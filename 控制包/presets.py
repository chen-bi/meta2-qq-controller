# -*- coding: utf-8 -*-
"""
============================================================
  设备控制预设库（元力2代 / TF-META2 蓝牙直控）
============================================================

【这是什么】
  一套可以直接跑在元力2代上的"节目单"。每个预设就是一段
  自动播放的节奏，跑起来不用人管，结束时自动归零断开。

【怎么用】
  python presets.py --list                 看所有预设和说明
  python presets.py --preset rush --minutes 5
  python presets.py --preset after --minutes 10
  python presets.py --dry-run --preset chaos --seconds 10
  python presets.py --preset rush --minutes 0     # 无限循环

  通用参数：
    --preset    预设名（见下方 PRESET_INFO）
    --minutes   跑多久，0 = 一直跑直到 Ctrl+C
    --intensity 整体强度倍数（默认 1.0，1.2 更狠）
    --seed      随机种子，同种子 = 同一段节奏
    --addr      手动指定设备地址（不填则自动找 TF-META2）

【预设清单 + 用途说明】（只开放这几个高频节奏预设）
  1. rush     快速上叠   —— 高频变化、持续上叠，适合快速进入状态
  2. edge     高位起伏   —— 高位来回，随机小幅回落后快速回升
  3. chaos    随机乱序   —— 所有参数 0.2-1 秒随机变一次，没有规律
  4. stopgo   停走       —— 短停后突然全速，完全不可预测
  5. reverse  正反交替   —— 旋转正反转交替
  6. pulse    脉冲       —— 三通道同步开关，打点器节奏
  7. random   随机       —— 各通道独立随机
  8. torture  长周期复合 —— 5 分钟一个循环，自动循环播放

【技术备注】
  - 设备：TF-META2（TryFun 元力2代），BLE 服务 0000ffac，写特征 0000ffb7
  - 命令字：振动 0x08 / 伸缩 0x0b / 旋转 0x0e（旋转 1-127 正转，256-v 反转）
  - 每 0.06 秒发一次，结束时必归零
  - 地址缓存：.meta2_addr（同目录）
============================================================
"""

import argparse
import asyncio
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "pylib"))

from bleak import BleakClient  # noqa: E402
from meta2 import Meta2, resolve_address  # noqa: E402

TICK = 0.06  # 变化间隔刻意做短，动作更碎、更抓不到

PRESET_INFO = {
    "rush":     ("快速上叠", "高频变化、持续上叠，强度缓慢提升"),
    "edge":     ("高位起伏", "高位起伏，随机小幅回落后快速回升"),
    "chaos":    ("随机乱序", "各参数 0.2-1 秒随机变一次"),
    "stopgo":   ("停走", "短停后突然全速，节奏不可预测"),
    "reverse":  ("正反交替", "旋转正反转交替"),
    "pulse":    ("脉冲", "三通道同步开关，脉冲节奏"),
    "random":   ("随机", "各通道独立随机"),
    "torture":  ("长周期复合", "5 分钟一循环：快速爬升→高位波动→爆发→全停→乱切→缓降→低强度，自动循环"),
}


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def rot_byte(speed, reverse):
    s = clamp(speed, 0, 99)
    return (256 - int(s)) & 0xFF if reverse else int(s)


class Runner:
    """统一引擎：每个 tick 根据预设生成 (vib, thrust, rot_raw)。"""

    def __init__(self, preset, seed=None, intensity=1.0):
        self.preset = preset
        self.rng = random.Random(seed)
        self.intensity = intensity
        # 随机变化的调度
        self.next_change = 0.0
        self.next_event = self.rng.uniform(2.0, 5.0)
        self.next_dir_flip = self.rng.uniform(1.0, 4.0)
        self.reverse = False
        self.pause_until = -1.0
        self.burst_until = -1.0
        # 当前值（平滑用）
        self.cur = {"vib": 0.0, "thrust": 0.0, "rot": 0.0}
        self.target = {"vib": 0.0, "thrust": 0.0, "rot": 0.0}
        # 通道独立暂停：可以只停旋转、只停震动，其他通道照常
        self.ch_pause = {"vib": -1.0, "thrust": -1.0, "rot": -1.0}
        self.next_ch_event = {
            "vib": self.rng.uniform(2.0, 6.0),
            "thrust": self.rng.uniform(3.0, 8.0),
            "rot": self.rng.uniform(2.0, 6.0),
        }
        # 磨人类预设：上升慢、下降很慢——要“慢慢降低”，不是瞬间停
        self.grind = preset in (
            "after", "slowgrind", "buzz", "flip", "tease",
            "drip", "nocturne", "aftershock",
        )
        self.alpha_up = {
            "vib": 0.10 if self.grind else 0.5,
            "thrust": 0.30 if self.grind else 0.5,
            "rot": 0.35 if self.grind else 0.5,
        }
        self.alpha_down = {"vib": 0.05, "thrust": 0.04, "rot": 0.07}
        # 顶点后：伸缩不常开，只偶尔出其不意点一两下
        self.next_jab = self.rng.uniform(5.0, 12.0)
        self.jab_until = -1.0
        # 预设内部状态
        self.st = {
            "phase": 0.0,
            "cycle": self.rng.uniform(40, 70),
            "vib_freq": self.rng.uniform(0.2, 0.9),
            "vib_freq_next": 0.0,
            "step_t": 0.0,
            "step_len": 0.2,
            "long_cycle": self.rng.uniform(180, 420),
            "stage": 1.0,
            "stage_next": self.rng.uniform(60, 150),
        }

    # ---------- 工具 ----------
    def _pick_targets(self, t):
        """按预设规则，随机刷新目标值。间隔很短，所以变化很碎。"""
        p = self.preset
        rng = self.rng
        I = self.intensity
        st = self.st
        if p == "rush":
            # 持续往上叠，几乎不回退
            boost = min(1.0, t / 60.0)
            st["step_len"] = rng.uniform(0.15, 0.55)
            self.target["thrust"] = clamp((45 + 55 * boost) * I + rng.uniform(-8, 8))
            self.target["vib"] = clamp((40 + 50 * boost) * I + rng.uniform(-12, 12))
            self.target["rot"] = clamp((50 + 45 * boost) * I + rng.uniform(-10, 10))
        elif p == "edge":
            # 高位徘徊，随机小降，偶尔大降
            st["step_len"] = rng.uniform(0.2, 0.8)
            base = 85 * I
            if rng.random() < 0.25:
                base = 35 * I
            self.target["thrust"] = clamp(base + rng.uniform(-10, 8))
            self.target["vib"] = clamp(base + rng.uniform(-20, 15))
            self.target["rot"] = clamp(base + rng.uniform(-10, 10))
        elif p == "after":
            # 旋转为主，震动精准，伸缩偶发
            st["step_len"] = rng.uniform(0.2, 0.7)
            self.target["rot"] = clamp(rng.uniform(75, 99) * I)
            self.target["vib"] = clamp(rng.uniform(55, 90) * I + rng.uniform(-8, 8))
            self.target["thrust"] = 0  # 顶点后伸缩靠 jab 偶尔点一下
        elif p == "chaos":
            st["step_len"] = rng.uniform(0.1, 0.5)
            self.target["thrust"] = clamp(rng.uniform(0, 100) * I)
            self.target["vib"] = clamp(rng.uniform(0, 100) * I)
            self.target["rot"] = clamp(rng.uniform(0, 99) * I)
        # 磨人预设：叠加几分钟尺度的慢包络，避免短时间重复
        if p in ("slowgrind", "buzz", "flip", "tease", "drip", "nocturne", "aftershock", "after"):
            env = (0.72 + 0.28 * math.sin((t / st["long_cycle"]) * math.tau)) * st["stage"]
            for k in ("vib", "thrust", "rot"):
                self.target[k] = clamp(self.target[k] * env)
        elif p == "stopgo":
            st["step_len"] = rng.uniform(0.1, 0.3)
            self.target["thrust"] = clamp(100 * I + rng.uniform(-5, 5))
            self.target["vib"] = clamp(rng.uniform(60, 100) * I)
            self.target["rot"] = clamp(rng.uniform(70, 99) * I)
        elif p == "tide":
            st["step_len"] = rng.uniform(0.4, 1.0)
            x = ((t % st["cycle"]) / st["cycle"])
            tri = 1 - abs(2 * x - 1)
            self.target["thrust"] = clamp(tri * 95 * I + rng.uniform(-6, 6))
            self.target["vib"] = clamp(tri * 70 * I + rng.uniform(-8, 8))
            self.target["rot"] = clamp(tri * 80 * I + rng.uniform(-8, 8))
        elif p == "sink":
            st["step_len"] = rng.uniform(0.3, 0.9)
            x = ((t % st["cycle"]) / st["cycle"])
            down = 1 - x
            self.target["thrust"] = clamp(down * 90 * I + rng.uniform(-6, 6))
            self.target["vib"] = clamp(down * 75 * I + rng.uniform(-8, 8))
            self.target["rot"] = clamp(down * 85 * I + rng.uniform(-8, 8))
        elif p == "reverse":
            st["step_len"] = rng.uniform(0.15, 0.6)
            self.target["rot"] = clamp(rng.uniform(80, 99) * I)
            self.target["vib"] = clamp(rng.uniform(55, 95) * I + rng.uniform(-10, 10))
            self.target["thrust"] = clamp(rng.uniform(80, 100) * I)
        elif p == "pulse":
            st["step_len"] = rng.uniform(0.1, 0.25)
            on = rng.random() < 0.5
            self.target["thrust"] = clamp((100 if on else 15) * I)
            self.target["vib"] = clamp((90 if on else 10) * I)
            self.target["rot"] = clamp((95 if on else 25) * I)
        elif p == "slowgrind":
            # 慢磨：三个维度都在低中位慢慢晃，不让人到，但一直有事发生
            st["step_len"] = rng.uniform(0.8, 2.0)
            self.target["thrust"] = 0  # 慢磨阶段伸缩基本不上，靠 jab 偶尔点
            self.target["vib"] = clamp(rng.uniform(40, 75) * I)
            self.target["rot"] = clamp(rng.uniform(40, 70) * I)
        elif p == "buzz":
            # 震动磨：旋转低档陪跑，震动在高位台阶慢移
            st["step_len"] = rng.uniform(0.6, 1.5)
            self.target["vib"] = clamp(rng.uniform(65, 95) * I)
            self.target["rot"] = clamp(rng.uniform(25, 55) * I)
            self.target["thrust"] = 0
        elif p == "flip":
            # 反向折磨：旋转高速但方向毫无预兆地翻
            st["step_len"] = rng.uniform(0.3, 1.0)
            self.target["rot"] = clamp(rng.uniform(80, 99) * I)
            self.target["vib"] = clamp(rng.uniform(50, 85) * I)
            self.target["thrust"] = 0  # 伸缩只靠 jab 偶尔来一下
        elif p == "tease":
            # 逗弄：随机掉到低位吊一下，再整片拉回去
            st["step_len"] = rng.uniform(0.2, 0.8)
            if rng.random() < 0.35:
                self.target["thrust"] = 0
                self.target["vib"] = clamp(rng.uniform(0, 20))
                self.target["rot"] = clamp(rng.uniform(0, 20))
            else:
                self.target["thrust"] = 0  # 逗弄不靠伸缩
                self.target["vib"] = clamp(rng.uniform(65, 95) * I)
                self.target["rot"] = clamp(rng.uniform(70, 99) * I)
        elif p == "drip":
            # 滴答：短促给一点又收回，间隔不固定
            st["step_len"] = rng.uniform(0.15, 0.5)
            on = rng.random() < 0.5
            self.target["thrust"] = clamp(rng.uniform(70, 95) if on else 0)
            self.target["vib"] = clamp(rng.uniform(75, 95) if on else 0)
            self.target["rot"] = clamp(rng.uniform(60, 90) if on else 0)
        elif p == "nocturne":
            # 夜曲：极慢极稳，长时间低强度
            st["step_len"] = rng.uniform(1.5, 3.0)
            self.target["thrust"] = clamp(rng.uniform(0, 15))
            self.target["vib"] = clamp(rng.uniform(20, 45) * I)
            self.target["rot"] = clamp(rng.uniform(20, 50) * I)
        elif p == "aftershock":
            # 余波：高位缓慢波动，含随机小爆发
            st["step_len"] = rng.uniform(0.3, 0.8)
            self.target["thrust"] = 0  # 余波不碰伸缩，靠 jab 点
            self.target["vib"] = clamp(rng.uniform(55, 85) * I)
            self.target["rot"] = clamp(rng.uniform(55, 90) * I)
        elif p == "torture":
            # 折磨循环：5 分钟一周期，50 秒快速爬坡后长时间高位，不让人冷却
            st["step_len"] = rng.uniform(1.2, 2.5)
            ph = t % 300.0
            if ph < 50:       # 50 秒快速爬坡
                k = ph / 50.0
                self.target["thrust"] = clamp(45 + 50 * k)
                self.target["vib"] = clamp(50 + 45 * k)
                self.target["rot"] = clamp(55 + 42 * k)
            elif ph < 200:    # 2.5 分钟高位磨：85-100 波动，偶尔掉下去再拉回
                base = 92 + 6 * math.sin(t * 0.18)
                if rng.random() < 0.12:
                    base = 70
                self.target["thrust"] = clamp(base + rng.uniform(-4, 6))
                self.target["vib"] = clamp(base * 0.95 + rng.uniform(-8, 6))
                self.target["rot"] = clamp(base + rng.uniform(-2, 2))
            elif ph < 205:    # 爆发
                self.target["thrust"] = 100
                self.target["vib"] = 100
                self.target["rot"] = 99
            elif ph < 208:    # 全停吊 3 秒
                self.target["thrust"] = 0
                self.target["vib"] = 0
                self.target["rot"] = 0
            elif ph < 260:    # 高位乱切
                on = rng.random() < 0.75
                self.target["thrust"] = clamp(100 if on else 60)
                self.target["vib"] = clamp(95 if on else 55)
                self.target["rot"] = clamp(99 if on else 65)
            elif ph < 290:    # 高位维持
                self.target["thrust"] = clamp(88 + 6 * math.sin(t * 0.25))
                self.target["vib"] = clamp(85 + 8 * math.sin(t * 0.3 + 1))
                self.target["rot"] = clamp(92 + 2 * math.sin(t * 0.2 + 2))
            else:             # 最后 10 秒缓和但不冷场
                self.target["thrust"] = 60
                self.target["vib"] = 55
                self.target["rot"] = 65
        else:  # random
            st["step_len"] = rng.uniform(0.1, 0.6)
            self.target["thrust"] = clamp(rng.uniform(0, 100) * I)
            self.target["vib"] = clamp(rng.uniform(0, 100) * I)
            self.target["rot"] = clamp(rng.uniform(0, 99) * I)

    def _schedule_events(self, t):
        """停顿 / 爆发 / 换向，各预设频率不同。"""
        p = self.preset
        rng = self.rng
        # 换向
        if t > self.next_dir_flip:
            self.reverse = not self.reverse
            if p in ("reverse", "chaos", "flip", "torture"):
                gap = rng.uniform(0.25, 1.2)
            elif p in ("after", "aftershock", "tease"):
                gap = rng.uniform(0.8, 2.5)
            elif p == "torture":
                gap = rng.uniform(15.0, 40.0)
            elif p in ("nocturne", "slowgrind"):
                gap = rng.uniform(6.0, 15.0)
            else:
                gap = rng.uniform(1.5, 5.0)
            self.next_dir_flip = t + gap
        # 停顿/爆发（rush 和 pulse 几乎没有停顿，保持压迫感）
        if t > self.next_event:
            if p in ("rush", "drip", "tease", "torture"):
                self.next_event = t + rng.uniform(4, 9)
            elif p in ("pulse",):
                self.next_event = t + rng.uniform(3, 7)
            elif p in ("slowgrind", "nocturne"):
                self.next_event = t + rng.uniform(6, 14)
            else:
                r = rng.random()
                if p in ("stopgo", "edge", "chaos"):
                    if r < 0.55:
                        self.pause_until = t + rng.uniform(0.4, 2.0)
                    else:
                        self.burst_until = t + rng.uniform(0.3, 1.2)
                else:
                    if r < 0.35:
                        self.pause_until = t + rng.uniform(1.2, 3.0)
                    elif r < 0.6:
                        self.burst_until = t + rng.uniform(0.3, 0.9)
                self.next_event = t + rng.uniform(2.0, 6.0)

    def step(self, t):
        rng = self.rng
        # 变频抖动（振动一直在抖）
        if t > self.st["vib_freq_next"]:
            self.st["vib_freq"] = rng.uniform(0.2, 1.6)
            self.st["vib_freq_next"] = t + rng.uniform(0.8, 2.5)

        # 阶段基调：每 1-2.5 分钟换一次整体强度档
        if t > self.st["stage_next"]:
            self.st["stage"] = rng.choice([0.8, 0.9, 1.0, 1.1, 1.2])
            self.st["stage_next"] = t + rng.uniform(60, 150)

        if t > self.next_change:
            self._pick_targets(t)
            self.next_change = t + self.st["step_len"]

        self._schedule_events(t)

        # 通道独立暂停：每次只挑一个通道关掉，其他照常
        for ch in ("vib", "thrust", "rot"):
            if self.ch_pause[ch] > 0 and t >= self.ch_pause[ch]:
                self.ch_pause[ch] = -1.0
            if self.preset == "torture":
                continue
            if t > self.next_ch_event[ch]:
                chance = 0.45 if self.grind else 0.2
                if rng.random() < chance:
                    self.ch_pause[ch] = t + rng.uniform(0.5, 2.5)
                self.next_ch_event[ch] = t + rng.uniform(2.5, 7.0)

        # 平滑追值：上升有坡度、下降很慢（要“慢慢降低”，不是瞬间停）
        out = {}
        for ch in ("vib", "thrust", "rot"):
            target = self.target[ch]
            if t < self.ch_pause[ch] or t < self.pause_until:
                target = 0.0
            alpha = self.alpha_up[ch] if target >= self.cur[ch] else self.alpha_down[ch]
            self.cur[ch] += (target - self.cur[ch]) * alpha
            out[ch] = self.cur[ch]

        # 振动：慢漂移 + 内部抖动，避免单一频率麻木
        vib = out["vib"] * (0.82 + 0.18 * math.sin(t * 0.33))
        vib += math.sin(t * math.tau * self.st["vib_freq"]) * 5.0 + rng.uniform(-3, 3)
        thrust = out["thrust"] + (rng.uniform(-2, 2) if out["thrust"] > 3 else 0)
        rot = out["rot"]

        # 顶点后：伸缩只在"出其不意"的时候点一两下
        if self.grind:
            if t > self.next_jab:
                self.jab_until = t + rng.uniform(0.25, 0.7)
                self.next_jab = t + rng.uniform(6, 18)
            if t < self.jab_until:
                thrust = max(thrust, 70 + rng.uniform(0, 25))

        if t < self.burst_until:
            return 95, 100, rot_byte(99, self.reverse)

        return int(clamp(vib)), int(clamp(thrust)), rot_byte(rot, self.reverse)


async def dry_run(preset, seconds, seed, intensity):
    r = Runner(preset, seed, intensity)
    t0 = time.monotonic()
    while True:
        t = time.monotonic() - t0
        if t > seconds:
            break
        vib, thrust, rot = r.step(t)
        rev = "反" if rot >= 128 else "正"
        print(f"{t:5.2f}s vib={vib:3d} thrust={thrust:3d} rot={rot:3d}({rev})")
        await asyncio.sleep(TICK)


async def play(preset, minutes, seed, intensity, addr=None):
    r = Runner(preset, seed, intensity)
    addr = addr or await resolve_address()
    print(f"连接 {addr}")
    async with BleakClient(addr, timeout=25) as client:
        dev = Meta2(client)
        print(f"已连接，预设：{preset}（{PRESET_INFO.get(preset, ('', ''))[1]}）")
        t0 = time.monotonic()
        try:
            while True:
                t = time.monotonic() - t0
                if minutes > 0 and t > minutes * 60:
                    break
                vib, thrust, rot = r.step(t)
                await dev.vibrate(vib)
                await dev.oscillate(thrust)
                await dev._write(0x0E, rot)
                await asyncio.sleep(TICK)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await dev.stop()
            print("归零，断开")


def main():
    p = argparse.ArgumentParser(description="设备控制预设库（元力2代）")
    p.add_argument("--list", action="store_true", help="列出所有预设")
    p.add_argument("--preset", default="chaos", help="预设名")
    p.add_argument("--minutes", type=float, default=5, help="跑多久，0=无限")
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--addr", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--seconds", type=float, default=10, help="dry-run 时长")
    args = p.parse_args()

    if args.list:
        print("预设清单：")
        for k, (name, desc) in PRESET_INFO.items():
            print(f"  {k:8s} {name:6s} {desc}")
        return

    if args.preset not in PRESET_INFO:
        print(f"没有这个预设：{args.preset}")
        return

    if args.dry_run:
        asyncio.run(dry_run(args.preset, args.seconds, args.seed, args.intensity))
    else:
        asyncio.run(play(args.preset, args.minutes, args.seed, args.intensity, args.addr))


if __name__ == "__main__":
    main()
