# -*- coding: utf-8 -*-
"""常驻守护进程（TCP 版，支持预设循环）：保持与 TF-META2 的连接，监听 127.0.0.1:5199。

命令（一行 JSON）：
  {"vib":0-100,"thrust":0-100,"rot_raw":0-255}   手动档位（rot_raw: 1-127 正转 / 129-255 反转）
  {"preset":"after"}                             切到某个预设，一直循环跑
  {"auto":true}                                  自动模式：每隔 25-45 秒随机换一个磨人预设
  {"stop":true}                                  归零并断开退出（保证停得下来）

auto 模式下会自动在 slowgrind / buzz / flip / tease / drip / nocturne / aftershock / after
之间随机切换，不用人管，节奏一直在变。
"""

import asyncio
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "pylib"))

from meta2 import Meta2, resolve_address  # noqa: E402
from bleak import BleakClient  # noqa: E402
from presets import Runner  # noqa: E402

HOST = "127.0.0.1"
PORT = 5199
LOG_FILE = os.path.join(HERE, "daemon.log")
TICK = 0.10

GRIND_PRESETS = ["slowgrind", "buzz", "flip", "tease", "drip", "nocturne", "aftershock", "after"]

CTX = {
    "mode": "manual",       # manual | preset
    "auto": False,          # 自动换预设
    "vib": 0,
    "thrust": 0,
    "rot_raw": None,
    "runner": None,
    "preset": None,
    "t0": time.monotonic(),
    "next_switch": 0.0,
}


def log(msg):
    line = f"[daemon] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def start_preset(name, seed=None):
    CTX["mode"] = "preset"
    CTX["preset"] = name
    CTX["runner"] = Runner(name, seed=seed, intensity=1.0)
    CTX["t0"] = time.monotonic()
    CTX["next_switch"] = time.monotonic() + random.uniform(25, 45)
    log(f"preset -> {name}")


async def control_loop(dev):
    last = {"vib": -999, "thrust": -999, "rot": -999}
    while True:
        now = time.monotonic()
        if CTX["mode"] == "preset" and CTX["runner"]:
            if CTX["auto"] and now > CTX["next_switch"]:
                start_preset(random.choice(GRIND_PRESETS))
            vib, thrust, rot = CTX["runner"].step(now - CTX["t0"])
        else:
            vib, thrust, rot = CTX["vib"], CTX["thrust"], CTX["rot_raw"]
        if rot is None:
            rot = 0
        # 死区：变化太小就不发，避免电机被频繁微调
        if abs(vib - last["vib"]) >= 2:
            await dev.vibrate(vib)
            last["vib"] = vib
        if abs(thrust - last["thrust"]) >= 2:
            await dev.oscillate(thrust)
            last["thrust"] = thrust
        if abs(rot - last["rot"]) >= 4:
            await dev._write(0x0E, rot)
            last["rot"] = rot
        await asyncio.sleep(TICK)


async def run():
    addr = await resolve_address()
    log(f"connect {addr}")
    async with BleakClient(addr, timeout=25) as client:
        dev = Meta2(client)
        log("connected")
        task = asyncio.create_task(control_loop(dev))

        async def handle(reader, writer):
            try:
                raw = await reader.readline()
                cmd = json.loads(raw.decode("utf-8", "replace"))
                # 探活：插件用它判断服务在不在，别让它真去动设备
                if cmd.get("ping"):
                    writer.write(b"pong\n")
                    await writer.drain()
                    return
                # 查状态：只回一行 JSON，不改任何参数
                if cmd.get("status"):
                    st = {
                        "mode": CTX["mode"],
                        "preset": CTX["preset"],
                        "auto": CTX["auto"],
                        "vib": CTX["vib"],
                        "thrust": CTX["thrust"],
                        "rot": 0 if CTX["rot_raw"] is None else CTX["rot_raw"],
                    }
                    writer.write((json.dumps(st) + "\n").encode("utf-8"))
                    await writer.drain()
                    return
                if cmd.get("stop"):
                    task.cancel()
                    await dev.stop()
                    log("STOP -> zeroed, disconnecting")
                    writer.write(b"bye\n")
                    await writer.drain()
                    await asyncio.sleep(0.4)
                    os._exit(0)
                if cmd.get("preset"):
                    CTX["auto"] = False
                    start_preset(cmd["preset"])
                elif cmd.get("auto"):
                    CTX["auto"] = True
                    CTX["mode"] = "preset"
                    start_preset(random.choice(GRIND_PRESETS))
                else:
                    CTX["mode"] = "manual"
                    CTX["auto"] = False
                    if "vib" in cmd:
                        CTX["vib"] = int(max(0, min(100, cmd.get("vib", 0))))
                    if "thrust" in cmd:
                        CTX["thrust"] = int(max(0, min(100, cmd.get("thrust", 0))))
                    if "rot_raw" in cmd:
                        CTX["rot_raw"] = int(cmd["rot_raw"]) & 0xFF
                writer.write(b"ok\n")
                await writer.drain()
            except Exception as e:
                log(f"handle error {type(e).__name__}: {e}")
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        server = await asyncio.start_server(handle, HOST, PORT)
        log(f"listening {HOST}:{PORT}")
        async with server:
            while True:
                await asyncio.sleep(1)
                if not client.is_connected:
                    task.cancel()
                    log("ble disconnected, exiting for reconnect")
                    return


async def main():
    while True:
        try:
            await run()
        except Exception as e:
            log(f"error {type(e).__name__}: {e}")
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
