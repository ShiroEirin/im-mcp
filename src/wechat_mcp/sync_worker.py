"""独立同步进程：脱离 MCP 进程树执行一次完整同步，并持续写心跳。

为什么需要它
------------
部分 MCP 宿主对 stdio 服务采用**短连接**：每次工具调用 spawn 一个进程、调用结束即退出
（实测 Snow App 的进程寿命 < 1 秒）。因此若只在 MCP 进程内起后台线程，线程会随进程被杀，
而一次完整同步（重新解密 + 重建索引）实测需要 53 秒 —— 永远跑不完，调用方也永远等不到
「跑完」的信号（表现为「同步中」一直转、run_count 永不增长）。

本模块由 ``auto_sync.start_sync()`` 以 detached + breakaway 方式拉起（Windows 用
CREATE_BREAKAWAY_FROM_JOB 脱离宿主 job，POSIX 用 start_new_session），因此：

- 它的生命周期与 MCP 进程**解耦**（MCP 被宿主回收也不会中断同步）；
- 它把心跳写进 ``~/.wechat_mcp/sync_state.json``、把结果写进 ``auto_sync.json``，
  调用方用 ``get_auto_sync_status`` 即可看到 ``syncing`` 与 ``last_run``。

用法::

    python -m wechat_mcp.sync_worker [--force] [--no-redecrypt]

退出码：0 = 同步成功，1 = 同步失败（失败原因同样已落盘，调用方查 ``last_run`` 可见）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import auto_sync


def _heartbeat_loop(stop_event: threading.Event, base: Dict[str, Any]) -> None:
    """按固定间隔刷新心跳时间戳 —— 查询方据此区分「还在跑」与「已僵死」。"""
    while not stop_event.wait(auto_sync.SYNC_HEARTBEAT_SECONDS):
        payload = dict(base)
        payload["heartbeat_at"] = time.time()
        auto_sync._save_sync_state(payload)


def run(force: bool = False, redecrypt: bool = True) -> int:
    """执行一次完整同步并落盘状态；返回进程退出码。"""
    started = time.time()
    payload: Dict[str, Any] = {
        "mode": "detached",
        "pid": os.getpid(),
        "started_at": started,
        "heartbeat_at": started,
        "force": bool(force),
        "redecrypt": bool(redecrypt),
    }
    auto_sync._save_sync_state(payload)

    stop_event = threading.Event()
    beat = threading.Thread(
        target=_heartbeat_loop, args=(stop_event, payload), name="sync-heartbeat", daemon=True
    )
    beat.start()

    try:
        result = auto_sync.refresh_and_sync(force=force, redecrypt=redecrypt)
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须落盘，否则调用方永远等不到结果
        result = {"ok": False, "error": f"独立同步进程异常：{exc}"}
    finally:
        stop_event.set()

    auto_sync.record_run(result, started)
    auto_sync._clear_sync_state()
    return 0 if result.get("ok") else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wechat_mcp.sync_worker",
        description="wechat-mcp 独立同步进程（由 start_sync 工具拉起，一般不手工调用）",
    )
    parser.add_argument("--force", action="store_true", help="强制重建索引（忽略指纹）")
    parser.add_argument("--no-redecrypt", action="store_true", help="跳过重新解密，只重建索引")
    args = parser.parse_args(argv)
    return run(force=args.force, redecrypt=not args.no_redecrypt)


if __name__ == "__main__":
    sys.exit(main())
