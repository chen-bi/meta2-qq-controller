# -*- coding: utf-8 -*-
"""元力2代（TF-META2）远程控制插件 —— 让 QQ 机器人在聊天里控制设备。

定位：不是"遥控器"，是"会根据聊天气氛自己动手"。
插件不做蓝牙，只把参数写成一行 JSON 发给 meta2_tcp.py 的 127.0.0.1:5199。

两种用法：
  1. 聊天驱动（主）：主人在私聊里说「开始」，之后她按对话气氛自己调，
     调完说一句"我刚刚把你调高了一点"这种就行；说「停」收工。
  2. 手动兜底：震动 60 / 伸缩 80 / 旋转 90 反 / 预设 rush / 自动 / 状态。

安全阀：
  - allowed_qq 白名单，默认只有主人
  - max_level 上限
  - chat_gate：没「开始」之前，模型调不动（防止日常聊天误触发）
  - min_interval：两次调整至少隔几秒，免得一直戳蓝牙
  - 档位跳变会拆成几次小步（ramp），不会从 0 直接蹦到 100
"""

import asyncio
import json
import os
import re
import subprocess
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .meta2_client import Meta2Link, healthy, wait_healthy

PRESET_NAMES = {
    "rush": "快速上叠",
    "edge": "高位起伏",
    "chaos": "随机乱序",
    "stopgo": "停走",
    "reverse": "正反交替",
    "pulse": "脉冲",
    "random": "随机",
    "torture": "长周期复合",
    "tide": "潮汐起伏",
    "sink": "下沉收尾",
    "after": "事后余韵",
    "slowgrind": "慢磨",
    "buzz": "震动磨",
    "flip": "反向磨",
    "tease": "逗弄",
    "drip": "滴答",
    "nocturne": "夜曲",
    "aftershock": "余波",
}

_CMD_WORDS = {
    "震动", "振动", "震", "vib",
    "伸缩", "thrust",
    "旋转", "转", "rot",
    "预设", "preset", "节奏",
    "自动", "auto",
    "停", "停止", "全停", "stop",
    "状态", "status",
    "开始", "开始吧", "启动", "开机", "connect",
}

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


class Meta2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.conf = config
        self.host = str(self._cfg("host", "127.0.0.1"))
        self.port = int(self._cfg("port", 5199))
        self.max_level = max(1, min(100, int(self._cfg("max_level", 100))))
        self.auto_start = bool(self._cfg("auto_start", True))
        # 路径：留空就用默认安装位置 %USERPROFILE%\meta2-controller
        default_dir = os.path.join(os.path.expanduser("~"), "meta2-controller")
        self.install_dir = str(self._cfg("install_dir", default_dir))
        self.python_path = str(self._cfg("python_path", "")) or os.path.join(
            self.install_dir, ".venv", "Scripts", "python.exe"
        )
        self.daemon_script = str(self._cfg("daemon_script", "")) or os.path.join(
            self.install_dir, "meta2_tcp.py"
        )
        self.start_timeout = float(self._cfg("start_timeout", 30))
        self.llm_enabled = bool(self._cfg("llm_enabled", False))
        self.chat_gate = bool(self._cfg("chat_gate", True))
        self.min_interval = float(self._cfg("min_interval", 6))
        # 语气跟随：不依赖模型判断，直接按关键词调档（模型不调工具时的兜底）
        self.auto_follow = bool(self._cfg("auto_follow", True))
        self.auto_step = max(1, int(self._cfg("auto_step", 10)))
        self.auto_drift = int(self._cfg("auto_drift", 3))
        self.auto_interval = float(self._cfg("auto_interval", 3))
        self.allowed = self._parse_allowed(self._cfg("allowed_qq", ""))
        self._last_spawn = 0.0
        self._active: dict[str, bool] = {}   # 会话 -> 是否已「开始」
        self._last_call: dict[str, float] = {}
        self._last_auto: dict[str, float] = {}
        self._level: dict[str, int] = {}     # "umo|channel" -> 当前档位
        # 自动爬坡（参考那种"一点一点推"的节奏，默认关）
        self.creep_enabled = bool(self._cfg("creep_enabled", False))
        self.creep_step = max(1, int(self._cfg("creep_step", 5)))
        self.creep_interval = max(5, int(self._cfg("creep_interval", 25)))
        self.creep_ceiling = max(1, min(100, int(self._cfg("creep_ceiling", 70))))
        self._creep: dict[str, bool] = {}
        self._creep_tasks: dict[str, asyncio.Task] = {}

    # ---------------- 配置小工具 ----------------

    def _cfg(self, key, default):
        try:
            value = self.conf.get(key, default)
        except Exception:
            value = default
        if value is None or value == "":
            return default
        return value

    @staticmethod
    def _parse_allowed(raw):
        if isinstance(raw, (list, tuple, set)):
            items = [str(x) for x in raw]
        else:
            items = re.split(r"[,，\s]+", str(raw or ""))
        return {x.strip() for x in items if x.strip()}

    @staticmethod
    def _session(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value)
        return "default"

    # ---------------- 权限 ----------------

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if self.allowed:
            try:
                return str(event.get_sender_id()) in self.allowed
            except Exception:
                return False
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ---------------- 通信 ----------------

    async def _send(self, payload: dict):
        """返回 (成功?, 回复文本)。阻塞 IO 全丢线程，别卡住她。"""
        if not await asyncio.to_thread(healthy, self.host, self.port):
            spawned = await asyncio.to_thread(self._spawn_daemon)
            if not spawned:
                return False, "设备服务没在跑。先确认设备开机、手机断开连接，然后发「开始」。"
            ok = await asyncio.to_thread(
                wait_healthy, self.host, self.port, self.start_timeout
            )
            if not ok:
                return False, (
                    f"等了 {int(self.start_timeout)} 秒也没连上设备。"
                    "检查：设备开机了吗、手机 App 断开没、离电脑近不近。"
                )
        try:
            reply = await asyncio.to_thread(
                Meta2Link(self.host, self.port, 3.0).send, payload
            )
        except OSError as exc:
            return False, f"发不出去：{exc}"
        logger.info(f"[meta2] 发出 {payload} -> {reply}")
        return True, reply

    async def _send_ramped(self, key: str, payload_fn, target: int):
        """档位变化拆成几步，别让电机猛地一跳。返回 (成功?, 消息, 实际走过的台阶)。"""
        last = self._level.get(key)
        if last is None or abs(target - last) <= 15:
            steps = [target]
        else:
            count = max(2, min(5, int(abs(target - last) // 15) + 1))
            steps = [int(last + (target - last) * i / count) for i in range(1, count + 1)]
        ok, msg = True, ""
        for step in steps:
            ok, msg = await self._send(payload_fn(step))
            if not ok:
                return False, msg, steps
            await asyncio.sleep(0.3)
        self._level[key] = target
        return True, "ok", steps

    def _spawn_daemon(self) -> bool:
        if not self.auto_start:
            return False
        now = time.monotonic()
        if now - self._last_spawn < 60:
            return False
        self._last_spawn = now
        if not os.path.isfile(self.python_path):
            logger.error(f"[meta2] python 不存在：{self.python_path}")
            return False
        if not os.path.isfile(self.daemon_script):
            logger.error(f"[meta2] 守护脚本不存在：{self.daemon_script}")
            return False
        try:
            subprocess.Popen(
                [self.python_path, self.daemon_script],
                cwd=os.path.dirname(self.daemon_script),
                creationflags=_CREATE_NO_WINDOW | _DETACHED_PROCESS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as exc:
            logger.error(f"[meta2] 拉起守护进程失败：{exc}")
            return False
        logger.info("[meta2] 已拉起 meta2_tcp.py")
        return True

    # ---------------- 指令解析 ----------------

    @staticmethod
    def _args(event: AstrMessageEvent):
        parts = (getattr(event, "message_str", "") or "").strip().split()
        if parts and parts[0] in _CMD_WORDS:
            parts = parts[1:]
        return parts

    @staticmethod
    def _first_int(parts):
        for item in parts:
            m = re.search(r"-?\d+", item)
            if m:
                return int(m.group())
        return None

    def _clamp(self, value, hi=None):
        hi = self.max_level if hi is None else hi
        return max(0, min(hi, int(value)))

    # ---------------- 语气跟随 ----------------

    @staticmethod
    def _tone_delta(text: str, step: int, drift: int) -> int:
        """粗粒度地把一句话的语气翻译成档位变化。"""
        t = text or ""
        if "别停" in t or "不要停" in t:
            return step
        if any(k in t for k in ("停", "不要了", "结束", "算了")):
            return -999  # 归零
        up = ("快", "用力", "继续", "还要", "再来", "别松", "重", "狠", "深")
        down = ("慢", "轻", "缓", "轻点", "等一下", "受不了", "疼", "等等")
        if any(k in t for k in up):
            return step
        if any(k in t for k in down):
            return -step
        return drift

    # ---------------- 手动指令（兜底） ----------------

    @filter.command("震动", alias={"振动", "震"})
    async def cmd_vib(self, event: AstrMessageEvent):
        """把震动调到 0-100。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        level = self._first_int(self._args(event))
        if level is None:
            yield event.plain_result("用法：震动 60")
            return
        level = self._clamp(level)
        ok, msg, _steps = await self._send_ramped(
            f"{self._session(event)}|vib", lambda v: {"vib": v}, level
        )
        yield event.plain_result(f"好啦，震动 {level}。" if ok else msg)

    @filter.command("伸缩", alias={"thrust"})
    async def cmd_thrust(self, event: AstrMessageEvent):
        """把伸缩调到 0-100。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        level = self._first_int(self._args(event))
        if level is None:
            yield event.plain_result("用法：伸缩 80")
            return
        level = self._clamp(level)
        ok, msg, _steps = await self._send_ramped(
            f"{self._session(event)}|thrust", lambda v: {"thrust": v}, level
        )
        yield event.plain_result(f"伸缩 {level}。" if ok else msg)

    @filter.command("旋转", alias={"转", "rot"})
    async def cmd_rot(self, event: AstrMessageEvent):
        """把旋转调到 0-99，可加「反」反转。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        parts = self._args(event)
        level = self._first_int(parts)
        if level is None:
            yield event.plain_result("用法：旋转 90 或 旋转 90 反")
            return
        reverse = any(("反" in p) or p.lower() in ("rev", "reverse") for p in parts)
        level = self._clamp(level, 99)
        raw = (256 - level) & 0xFF if reverse else level
        ok, msg = await self._send({"rot_raw": raw})
        yield event.plain_result(
            (f"旋转 {level}，{'反' if reverse else '正'}向。" if ok else msg)
        )

    @filter.command("预设", alias={"preset", "节奏"})
    async def cmd_preset(self, event: AstrMessageEvent):
        """切到某个自动节奏并一直循环。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        parts = self._args(event)
        if not parts:
            yield event.plain_result("用法：预设 rush。发「预设 列表」看全部。")
            return
        if parts[0] in ("列表", "list", "ls"):
            yield event.plain_result("\n".join(f"{k}：{v}" for k, v in PRESET_NAMES.items()))
            return
        name = parts[0].lower()
        if name not in PRESET_NAMES:
            yield event.plain_result(f"没有「{name}」这个节奏。发「预设 列表」看能用的。")
            return
        ok, msg = await self._send({"preset": name})
        yield event.plain_result(
            f"切到 {name}（{PRESET_NAMES[name]}），会一直循环。" if ok else msg
        )

    @filter.command("自动", alias={"auto"})
    async def cmd_auto(self, event: AstrMessageEvent):
        """每 25-45 秒自动换一个节奏。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        ok, msg = await self._send({"auto": True})
        yield event.plain_result("自动模式开了，每 25-45 秒换一个节奏。" if ok else msg)

    # ---------------- 会话开关 ----------------

    @filter.command("开始", alias={"开始吧", "启动", "开机", "connect"})
    async def cmd_start(self, event: AstrMessageEvent):
        """开始一局：连设备，允许她按聊天气氛自己调。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        session = self._session(event)
        self._last_spawn = 0.0  # 手动启动不受 60 秒限流
        if not await asyncio.to_thread(healthy, self.host, self.port):
            if not await asyncio.to_thread(self._spawn_daemon):
                yield event.plain_result("拉不起来，守护脚本或 Python 路径配错了？")
                return
            if not await asyncio.to_thread(
                wait_healthy, self.host, self.port, self.start_timeout
            ):
                yield event.plain_result(
                    "没连上设备。检查：开机了吗、手机 App 断开没、离电脑近不近。"
                )
                return
        self._active[session] = True
        self._level.clear()
        yield event.plain_result("连上了。这局我看着你的话自己调，说「停」就收。")

    @filter.command("停", alias={"停止", "全停", "stop"})
    async def cmd_stop(self, event: AstrMessageEvent):
        """归零并断开，顺便结束这一局。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        session = self._session(event)
        self._active[session] = False
        self._stop_creep(session)
        if not await asyncio.to_thread(healthy, self.host, self.port):
            yield event.plain_result("本来就没在跑呀，什么都没做。")
            return
        ok, msg = await self._send({"stop": True})
        yield event.plain_result("停了，归零，断开。" if ok else msg)

    # ---------------- 自动爬坡 ----------------

    @filter.command("爬坡", alias={"推上去", "慢推"})
    async def cmd_creep(self, event: AstrMessageEvent):
        """让她自己每隔一会儿往上加一点（不用你催）。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        if not self.creep_enabled:
            yield event.plain_result("自动爬坡没开（配置里 creep_enabled）。")
            return
        session = self._session(event)
        if not self._active.get(session):
            yield event.plain_result("先发「开始」呀。")
            return
        if self._creep.get(session):
            self._stop_creep(session)
            yield event.plain_result("好，不推了，停在现在。")
            return
        self._creep[session] = True
        self._creep_tasks[session] = asyncio.create_task(self._creep_loop(session))
        yield event.plain_result(
            f"行，我每 {self.creep_interval} 秒往上加 {self.creep_step}，"
            f"最多到 {self.creep_ceiling}。说「爬坡」停。"
        )

    def _stop_creep(self, session: str):
        self._creep[session] = False
        task = self._creep_tasks.pop(session, None)
        if task and not task.done():
            task.cancel()

    async def _creep_loop(self, session: str):
        try:
            while self._creep.get(session):
                await asyncio.sleep(self.creep_interval)
                if not self._creep.get(session):
                    break
                if not await asyncio.to_thread(healthy, self.host, self.port):
                    break
                for ch in ("vib", "thrust", "rot"):
                    key = f"{session}|{ch}"
                    current = self._level.get(key, 0)
                    target = min(self.creep_ceiling, current + self.creep_step)
                    if target <= current:
                        continue
                    payload = {"rot_raw": target} if ch == "rot" else {ch: target}
                    ok, _ = await self._send(payload)
                    if ok:
                        self._level[key] = target
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"[meta2] 爬坡循环出错：{exc}")

    @filter.command("状态", alias={"status"})
    async def cmd_status(self, event: AstrMessageEvent):
        """看看现在在跑什么。"""
        if not self._allowed(event):
            yield event.plain_result("……这个不是你该碰的东西哦。")
            return
        if not await asyncio.to_thread(healthy, self.host, self.port):
            yield event.plain_result("设备服务没在跑（=设备没连上）。")
            return
        ok, msg = await self._send({"status": True})
        if not ok:
            yield event.plain_result(msg)
            return
        try:
            st = json.loads(msg)
        except json.JSONDecodeError:
            yield event.plain_result(f"服务回了看不懂的东西：{msg}")
            return
        preset = st.get("preset")
        tail = f"节奏 {preset}（{PRESET_NAMES.get(preset, '?')}）" if preset else "手动模式"
        if st.get("auto"):
            tail += "，自动换节奏中"
        rot_raw = int(st.get("rot") or 0)
        rot_txt = f"{256 - rot_raw}（反）" if rot_raw >= 128 else f"{rot_raw}（正）"
        active = "，这一局开着" if self._active.get(self._session(event)) else ""
        yield event.plain_result(
            f"{tail}{active}。振动={st.get('vib')} 伸缩={st.get('thrust')} 旋转={rot_txt}"
        )

    # ---------------- 每句话自动跟随（不依赖模型判断） ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_follow_message(self, event: AstrMessageEvent):
        """这一局开着的时候，按你说话的语气直接调档，不用模型发指令。"""
        if not self.auto_follow or not self._allowed(event):
            return
        session = self._session(event)
        if not self._active.get(session):
            return
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        now = time.monotonic()
        if now - self._last_auto.get(session, 0.0) < self.auto_interval:
            return
        delta = self._tone_delta(text, self.auto_step, self.auto_drift)
        if delta == 0:
            return
        self._last_auto[session] = now

        if delta == -999:
            self._stop_creep(session)
            self._active[session] = False
            for ch in ("vib", "thrust", "rot"):
                self._level[f"{session}|{ch}"] = 0
            await self._send({"vib": 0, "thrust": 0, "rot_raw": 0})
            return

        for ch in ("vib", "thrust", "rot"):
            key = f"{session}|{ch}"
            cur = self._level.get(key, 0)
            target = self._clamp(cur + delta, 99 if ch == "rot" else None)
            if target == cur:
                continue
            if ch == "vib":
                payload_fn = lambda v: {"vib": v}
            elif ch == "thrust":
                payload_fn = lambda v: {"thrust": v}
            else:
                payload_fn = lambda v: {"rot_raw": v}
            ok, _msg, _steps = await self._send_ramped(key, payload_fn, target)
            if not ok:
                return

    # ---------------- 聊天驱动（主用法） ----------------

    @filter.llm_tool(name="meta2_control")
    async def tool_meta2(
        self,
        event: AstrMessageEvent,
        vib: int = -1,
        thrust: int = -1,
        rot: int = -1,
        reverse: bool = False,
        preset: str = "",
        stop: bool = False,
        creep: bool = False,
    ):
        """调节连着电脑的那台设备的强度（振动/伸缩/旋转）。

        You drive this yourself: after the owner starts a session with 开始, judge the
        mood of the conversation every turn and adjust without being asked. Raise when
        the owner's messages get more intense, demanding or impatient; lower when they
        calm down, hesitate, or say it is too much. Never wait for the owner to give
        numbers, and never ask permission before adjusting.

        IMPORTANT: fill in all three values (vib, thrust, rot) on every call, and make
        them DIFFERENT from each other — decide each channel separately according to
        what the scene is like right now (one channel can lead while another stays
        low). Only use -1 when you deliberately want to keep that channel untouched.
        Change by about 10-20 at a time, not in one jump. One short line afterwards is
        enough. Call with stop=true when the owner wants to end.

        Args:
            vib(int): 振动 0-100，-1 表示不改
            thrust(int): 伸缩 0-100，-1 表示不改
            rot(int): 旋转 0-99，-1 表示不改
            reverse(bool): 旋转方向是否反转
            preset(string): 切到某个自动节奏（rush/edge/chaos/torture 等），空表示不切
            stop(bool): 是否归零收工
            creep(bool): 是否开自动爬坡（她每隔一会儿自己加一点）
        """
        if not self._allowed(event):
            return "拒绝：不是允许的人。"
        if not self.llm_enabled:
            return "聊天驱动没开（llm_enabled=false）。"

        logger.info(
            f"[meta2] 工具被调用 vib={vib} thrust={thrust} rot={rot} "
            f"reverse={reverse} preset={preset!r} stop={stop} creep={creep}"
        )

        session = self._session(event)
        if self.chat_gate and not self._active.get(session):
            return "这一局还没开始，先等主人说「开始」。"

        if stop:
            self._active[session] = False
            self._stop_creep(session)
            ok, msg = await self._send({"stop": True})
            return "已归零断开" if ok else msg

        if creep:
            if not self.creep_enabled:
                return "自动爬坡没开。"
            if self._creep.get(session):
                return "已经在爬了。"
            self._creep[session] = True
            self._creep_tasks[session] = asyncio.create_task(self._creep_loop(session))
            return (
                f"开始自动爬坡：每 {self.creep_interval} 秒加 {self.creep_step}，"
                f"上限 {self.creep_ceiling}。"
            )

        if preset:
            name = preset.strip().lower()
            if name not in PRESET_NAMES:
                return f"没有这个预设：{name}"
            ok, msg = await self._send({"preset": name})
            return f"切到 {name}" if ok else msg

        plan = []
        if vib >= 0:
            plan.append(("振动", "vib", self._clamp(vib), lambda v: {"vib": v}))
        if thrust >= 0:
            plan.append(("伸缩", "thrust", self._clamp(thrust), lambda v: {"thrust": v}))
        if rot >= 0:
            plan.append(
                (
                    "旋转",
                    "rot",
                    self._clamp(rot, 99),
                    lambda v: {"rot_raw": (256 - v) & 0xFF if reverse else v},
                )
            )
        if not plan:
            return "没有给档位。"

        now = time.monotonic()
        last = self._last_call.get(session, 0.0)
        if now - last < self.min_interval:
            return f"刚调过，{self.min_interval - (now - last):.0f} 秒后再动。"
        self._last_call[session] = now

        parts = []
        for label, chan, target, payload_fn in plan:
            ok, msg, steps = await self._send_ramped(
                f"{session}|{chan}", payload_fn, target
            )
            if not ok:
                return msg
            parts.append(f"{label} " + "→".join(str(x) for x in steps))
        return "已调：" + "；".join(parts) + "。照这个报一句就好，别念整段。"
