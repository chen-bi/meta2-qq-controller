# -*- coding: utf-8 -*-
"""冲刺波形引擎 —— 仿「挑战自己（瑶瑶爽爆了）」并加码的不规则节奏。

思路（按毛球描述还原，再故意做得更坏）：
  1. 伸缩：缓慢递增 → 到顶后缓慢回落，来回摆动（三角波 + 随机拐点）
  2. 旋转：方向不定时切换，速度跟着整体强度走
  3. 振动：频率一直在变（不是平稳线，是一直在抖），叠随机噪声
  4. 随机停一小会（0.3~1.5 秒），然后突然出其不意地满上
  5. 偶尔"爆发"：短促拉满再掉下来

用法：
  python sprint_wave.py --minutes 10          # 跑十分钟
  python sprint_wave.py --minutes 0            # 无限循环，Ctrl+C 停
  python sprint_wave.py --intensity 1.2 --minutes 15
  python sprint_wave.py --dry-run --seconds 20 # 只打印波形，不连设备

结束后自动全部归零并断开。
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

TICK = 0.06  # 变化间隔做短，动作更碎


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def rot_byte(speed, reverse):
    """设备字节约定：正向 = speed，反向 = 256-speed。"""
    s = clamp(speed, 0, 99)
    return (256 - int(s)) & 0xFF if reverse else int(s)


class WaveEngine:
    def __init__(self, seed=None, intensity=1.0):
        self.rng = random.Random(seed)
        self.intensity = intensity
        # 爬坡周期：40~70 秒一个来回
        self.cycle = self.rng.uniform(40, 70)
        self.phase_offset = 0.0  # 从最低处起步，慢慢爬
        # 振动调制
        self.vib_freq = self.rng.uniform(0.2, 0.8)
        self.vib_freq_next = 0.0
        # 旋转方向
        self.reverse = False
        self.next_dir_flip = self.rng.uniform(3, 8)
        # 事件状态
        self.pause_until = -1.0
        self.burst_until = -1.0
        self.next_event = self.rng.uniform(4, 9)

    def _climb(self, t):
        """缓慢递增再回落：三角波 + 轻微随机拐点，范围 0~1。"""
        x = ((t + self.phase_offset) % self.cycle) / self.cycle
        tri = 1 - abs(2 * x - 1)          # 0→1→0
        tri = tri ** 1.15                  # 顶端多停一点
        return tri

    def step(self, t):
        """返回 (vib, thrust, rot_raw)。"""
        base = self._climb(t) * 100 * self.intensity

        # 振动：变频 + 持续抖动
        if t > self.vib_freq_next:
            self.vib_freq = self.rng.uniform(0.15, 1.2)
            self.vib_freq_next = t + self.rng.uniform(1.5, 4.0)
        vib_center = 18 + base * 0.55
        vib_wave = math.sin(t * math.tau * self.vib_freq) * (8 + base * 0.10)
        vib_noise = self.rng.uniform(-8, 8)
        vib = clamp(vib_center + vib_wave + vib_noise)

        # 伸缩：坡道上加一点不规则的快抖，别让它平滑
        thrust = clamp(base + self.rng.uniform(-9, 9) + math.sin(t * 2.3) * 4.0)

        # 旋转方向
        if t > self.next_dir_flip:
            self.reverse = not self.reverse
            self.next_dir_flip = t + self.rng.uniform(2.5, 8.0)
        rot_speed = clamp(30 + base * 0.65 + self.rng.uniform(-6, 6), 0, 99)
        rot_raw = rot_byte(rot_speed, self.reverse)

        # 随机停顿
        if t > self.next_event:
            if self.rng.random() < 0.45:
                self.pause_until = t + self.rng.uniform(0.3, 1.5)
            else:
                self.burst_until = t + self.rng.uniform(0.35, 0.9)
            self.next_event = t + self.rng.uniform(5, 11)

        if t < self.pause_until:
            return 0, 0, rot_byte(0, False)
        if t < self.burst_until:
            return 95, 100, rot_byte(99, self.reverse)

        return int(vib), int(thrust), rot_raw


async def dry_run(seconds, intensity, seed):
    eng = WaveEngine(seed=seed, intensity=intensity)
    t0 = time.monotonic()
    n = 0
    while True:
        t = time.monotonic() - t0
        if t > seconds:
            break
        vib, thrust, rot = eng.step(t)
        rev = "反" if rot >= 128 else "正"
        print(f"{t:5.2f}s  vib={vib:3d}  thrust={thrust:3d}  rot={rot:3d}({rev})")
        n += 1
        await asyncio.sleep(TICK)
    print(f"共 {n} 帧")


async def play(minutes, intensity, seed, addr=None):
    eng = WaveEngine(seed=seed, intensity=intensity)
    addr = addr or await resolve_address()
    print(f"连接 {addr}")
    async with BleakClient(addr, timeout=25) as client:
        dev = Meta2(client)
        print("已连接，开始跑波形（Ctrl+C 停止）")
        t0 = time.monotonic()
        try:
            while True:
                t = time.monotonic() - t0
                if minutes > 0 and t > minutes * 60:
                    break
                vib, thrust, rot = eng.step(t)
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
    p = argparse.ArgumentParser(description="冲刺波形引擎（不规律节奏）")
    p.add_argument("--minutes", type=float, default=10, help="跑多久，0 = 无限循环")
    p.add_argument("--intensity", type=float, default=1.0, help="整体强度倍数")
    p.add_argument("--seed", type=int, default=None, help="随机种子（同种子=同节奏）")
    p.add_argument("--addr", default=None, help="指定设备地址")
    p.add_argument("--dry-run", action="store_true", help="只打印波形不连设备")
    p.add_argument("--seconds", type=float, default=15, help="dry-run 时长")
    args = p.parse_args()

    if args.dry_run:
        asyncio.run(dry_run(args.seconds, args.intensity, args.seed))
    else:
        asyncio.run(play(args.minutes, args.intensity, args.seed, args.addr))


if __name__ == "__main__":
    main()
