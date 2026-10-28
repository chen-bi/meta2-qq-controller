# -*- coding: utf-8 -*-
"""与 meta2_tcp.py 通信的最小 TCP 客户端。

协议就一句话：连 127.0.0.1:5199，发一行 JSON，收一行回复，断开。
守护进程每次只读一行（见 meta2_tcp.py 的 handle），所以不要复用连接。

可用指令：
  {"vib":0-100,"thrust":0-100,"rot_raw":0-255}   手动档位
  {"preset":"rush"}                              切预设，一直循环
  {"auto":true}                                  每 25-45 秒随机换预设
  {"stop":true}                                  归零并退出守护进程
  {"ping":true}                                  探活，回 pong
  {"status":true}                                查当前状态，回一行 JSON
"""

import json
import socket
import time


class Meta2Link:
    def __init__(self, host: str = "127.0.0.1", port: int = 5199, timeout: float = 3.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    def send(self, payload: dict) -> str:
        """发一行 JSON，返回一行回复。连接失败会抛 OSError。"""
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.create_connection((self.host, self.port), self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(raw)
            return sock.recv(4096).decode("utf-8", "replace").strip()


def healthy(host: str = "127.0.0.1", port: int = 5199, timeout: float = 0.8) -> bool:
    """守护进程活着且回声正常。"""
    try:
        return Meta2Link(host, port, timeout).send({"ping": True}) == "pong"
    except OSError:
        return False


def wait_healthy(host: str, port: int, timeout: float = 30.0, interval: float = 0.6) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if healthy(host, port):
            return True
        time.sleep(interval)
    return False
