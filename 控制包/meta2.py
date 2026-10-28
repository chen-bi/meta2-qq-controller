# -*- coding: utf-8 -*-
"""TryFun 元力2代（TF-META2）直接蓝牙控制脚本。

协议来源：buttplug-rs 的 tryfun_meta2 协议实现（BSD-3-Clause, Nonpolynomial Labs LLC）
数据包：[packet_id, 0x02, 0x00, 0x05, 0x21, 0x05, cmd, speed, checksum]
  cmd: 0x08 振动 / 0x05 伸缩 / 0x0e 旋转
  checksum = (0xff - sum(data[1:]) + 8) & 0xff

用法示例：
  python meta2.py --vib 40 --sec 5            # 振动 40 档 5 秒
  python meta2.py --thrust 30 --rot 50 --sec 8
  python meta2.py --preset wave --sec 20      # 振动波浪
  python meta2.py --preset ramp --sec 15 --target thrust
  python meta2.py --scan                      # 只扫描附近 BLE 设备
"""

import argparse
import asyncio
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "pylib"))

from bleak import BleakClient, BleakScanner  # noqa: E402

DEVICE_NAME = "TF-META2"
ADDR_FILE = os.path.join(HERE, ".meta2_addr")

TX_CHAR = "0000ffb7-0000-1000-8000-00805f9b34fb"
RX_CHAR = "0000ffb8-0000-1000-8000-00805f9b34fb"

CMD_VIBRATE = 0x08
CMD_OSCILLATE = 0x0B
CMD_ROTATE = 0x0E


class Meta2:
    def __init__(self, client):
        self.client = client
        self.pid = 0

    def _packet(self, cmd, value):
        data = [self.pid & 0xFF, 0x02, 0x00, 0x05, 0x21, 0x05, cmd, value & 0xFF]
        checksum = 0xFF
        for b in data[1:]:
            checksum = (checksum - b) & 0xFF
        checksum = (checksum + (len(data[1:]) + 1)) & 0xFF
        self.pid = (self.pid + 1) & 0xFF
        return bytes(data + [checksum])

    async def _write(self, cmd, value):
        await self.client.write_gatt_char(TX_CHAR, self._packet(cmd, value), response=False)

    async def vibrate(self, speed):
        await self._write(CMD_VIBRATE, max(0, min(100, int(speed))))

    async def oscillate(self, speed):
        await self._write(CMD_OSCILLATE, max(0, min(100, int(speed))))

    async def rotate(self, speed, reverse=False):
        # 设备字节约定：正向 = 1..127（直接写 speed）；反向 = 256-speed
        val = min(99, max(0, int(speed)))
        await self._write(CMD_ROTATE, (256 - val) & 0xFF if reverse else val & 0xFF)

    async def stop(self):
        await self.vibrate(0)
        await self.oscillate(0)
        await self.rotate(0)


def find_saved_addr():
    if os.path.exists(ADDR_FILE):
        with open(ADDR_FILE, "r", encoding="utf-8") as f:
            addr = f.read().strip()
            if addr:
                return addr
    return None


async def resolve_address(addr=None):
    if addr:
        return addr
    saved = find_saved_addr()
    if saved:
        return saved
    print("扫描设备中（10 秒）...")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)
    # 名字匹配放宽：不同批次/固件可能叫 TF-META2、META2-PRO、TryFun Meta 之类
    keys = ("META2", "META", "TRYFUN", "元力")
    hits = []
    for address, (dev, adv) in sorted(devices.items(), key=lambda kv: -(kv[1][1].rssi or -999)):
        name = dev.name or ""
        print(f"  {address}\tRSSI={adv.rssi}\t{name or '(无名)'}")
        if name == DEVICE_NAME or any(k in name.upper() for k in keys):
            hits.append(address)
    if hits:
        with open(ADDR_FILE, "w", encoding="utf-8") as f:
            f.write(hits[0])
        return hits[0]
    raise SystemExit(
        "没找到 " + DEVICE_NAME + "（或名字带 META 的设备）。\n"
        "确认：设备已开机、手机 App 已断开连接、离电脑 1 米内。\n"
        "如果上面扫出的名字跟预期不一样，把名字记下来改 DEVICE_NAME。"
    )


async def do_scan():
    devices = await BleakScanner.discover(timeout=10, return_adv=True)
    for address, (dev, adv) in sorted(devices.items(), key=lambda kv: -(kv[1][1].rssi or -999)):
        print(f"{address}\tRSSI={adv.rssi}\t{dev.name}")


async def preset_loop(dev, preset, seconds, target, base, amp):
    """按预设持续驱动，直到 seconds 秒结束。"""
    steps = max(1, int(seconds / 0.1))
    t = 0
    level = {"vibrate": "vibrate", "thrust": "oscillate", "rot": "rotate"}[target]
    for i in range(steps):
        if preset == "wave":
            v = base + amp * (math.sin(t * 1.2) + 1) / 2
        elif preset == "ramp":
            v = base + amp * (i / steps)
        elif preset == "rampdown":
            v = base + amp * (1 - i / steps)
        elif preset == "heartbeat":
            beat = (t * 1.4) % 1.0
            v = base + amp * (1 if beat < 0.12 else (0.45 if beat < 0.4 else 0))
        elif preset == "pulse":
            v = base + amp * (0.5 + 0.5 * math.sin(t * 2.5))
        elif preset == "random":
            if i % 8 == 0:
                current = random.uniform(base, base + amp)
            v = current
        else:
            v = base
        v = max(0, min(100, int(v)))
        if level == "vibrate":
            await dev.vibrate(v)
        elif level == "oscillate":
            await dev.oscillate(v)
        else:
            await dev.rotate(v)
        await asyncio.sleep(0.1)
        t += 0.1


async def run(args):
    addr = await resolve_address(args.addr)
    print(f"连接 {addr} ...")
    async with BleakClient(addr, timeout=25) as client:
        print("已连接，开始控制")
        dev = Meta2(client)
        try:
            if args.preset:
                await preset_loop(dev, args.preset, args.sec, args.target, args.base, args.amp)
            else:
                await dev.vibrate(args.vib)
                await dev.oscillate(args.thrust)
                await dev.rotate(args.rot)
                await asyncio.sleep(args.sec)
        except asyncio.CancelledError:
            pass
        finally:
            await dev.stop()
            print("已全部归零，断开")


def main():
    p = argparse.ArgumentParser(description="TryFun 元力2代蓝牙直控")
    p.add_argument("--scan", action="store_true", help="扫描附近 BLE 设备")
    p.add_argument("--addr", help="指定设备蓝牙地址")
    p.add_argument("--vib", type=int, default=0, help="振动档位 0-100")
    p.add_argument("--thrust", type=int, default=0, help="伸缩档位 0-100")
    p.add_argument("--rot", type=int, default=0, help="旋转档位 0-99")
    p.add_argument("--sec", type=float, default=5, help="持续秒数")
    p.add_argument("--preset", choices=["wave", "ramp", "rampdown", "heartbeat", "pulse", "random"],
                   help="预设效果")
    p.add_argument("--target", choices=["vibrate", "thrust", "rot"], default="vibrate",
                   help="预设作用的功能")
    p.add_argument("--base", type=int, default=10, help="预设基础档位")
    p.add_argument("--amp", type=int, default=80, help="预设幅度")
    args = p.parse_args()
    if args.scan:
        asyncio.run(do_scan())
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
