"""FastMCP Server for local DingTalk chat history and search."""

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional
from mcp.server.fastmcp import FastMCP

from dingtalk_mcp.db import DingTalkDB
from dingtalk_mcp.decrypt import (
    decrypt_dingtalk,
    decrypt_dingtalk_with_report,
    detect_dingtalk_user,
)

mcp = FastMCP("dingtalk-mcp")

_db_instance: Optional[DingTalkDB] = None


def get_db() -> DingTalkDB:
    global _db_instance
    if _db_instance is not None:
        return _db_instance

    # Auto decrypt or use cached decrypted db
    decrypted_dir = os.environ.get(
        "DINGTALK_DECRYPTED_DIR",
        os.path.join(os.path.expanduser("~"), ".dingtalk_mcp", "decrypted"),
    )
    db_file = os.path.join(decrypted_dir, "dingtalk.db")

    # If doesn't exist, decrypt now
    if not os.path.isfile(db_file):
        data_dir, uid, ver, salt = detect_dingtalk_user()
        if not data_dir or not uid:
            raise RuntimeError(
                "Could not auto-detect DingTalk data directory or UID. "
                "Please set DINGTALK_DATA_DIR and DINGTALK_UID environment variables."
            )
        decrypt_dingtalk(data_dir, uid, ver, salt, decrypted_dir)

    _db_instance = DingTalkDB(db_file)
    return _db_instance


@mcp.tool()
def sync_dingtalk_data() -> str:
    """Manually sync and decrypt latest data from DingTalk local files with WAL validation."""
    global _db_instance
    decrypted_dir = os.environ.get(
        "DINGTALK_DECRYPTED_DIR",
        os.path.join(os.path.expanduser("~"), ".dingtalk_mcp", "decrypted"),
    )
    data_dir, uid, ver, salt = detect_dingtalk_user()
    if not data_dir or not uid:
        return "Error: Could not auto-detect DingTalk data directory or UID."

    out_db, report = decrypt_dingtalk_with_report(data_dir, uid, ver, salt, decrypted_dir)
    _db_instance = DingTalkDB(out_db)

    # 采样分表获取最新消息时间
    latest_msg_time = None
    try:
        with _db_instance.get_connection() as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tbmsg_%'"
                ).fetchall()
            ]
            max_ts = 0
            for t in tables[:25]:
                row = conn.execute(f"SELECT MAX(createdAt) FROM {t}").fetchone()
                if row and row[0] and row[0] > max_ts:
                    max_ts = row[0]
            if max_ts > 0:
                from datetime import datetime
                latest_msg_time = datetime.fromtimestamp(max_ts / 1000.0).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
    except Exception:
        pass

    method = report.get("checkpoint_method", "none")
    applied = report.get("wal_frames_applied", 0)
    total = report.get("wal_frames_total", 0)

    msg = f"DingTalk database successfully synced to {out_db} (checkpoint: {method}, wal_frames: {applied}/{total}"
    if latest_msg_time:
        msg += f", latest_msg: {latest_msg_time}"
    msg += ")"
    return msg


@mcp.tool()
def list_dingtalk_conversations(
    limit: int = 50,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List recent DingTalk conversations (group chats and private chats).

    Args:
        limit: Maximum number of conversations to return (default 50).
        keyword: Optional keyword to filter conversation title.
    """
    db = get_db()
    return db.list_conversations(limit=limit, keyword=keyword)


@mcp.tool()
def get_dingtalk_messages(
    cid: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Get chat messages for a specific conversation by cid.

    Args:
        cid: The conversation ID (from list_dingtalk_conversations).
        limit: Maximum number of messages to return (default 50).
    """
    db = get_db()
    return db.get_messages(cid=cid, limit=limit)


@mcp.tool()
def search_dingtalk_messages(
    keyword: str,
    cid: Optional[str] = None,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Search DingTalk messages by keyword across conversations.

    Args:
        keyword: Search query keyword.
        cid: Optional conversation ID to restrict search.
        limit: Maximum results (default 30).
    """
    db = get_db()
    return db.search_messages(keyword=keyword, cid=cid, limit=limit)


@mcp.tool()
def list_dingtalk_contacts(
    limit: int = 100,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List DingTalk contacts and team members.

    Args:
        limit: Max contacts to return (default 100).
        keyword: Filter by name, nick or uid.
    """
    db = get_db()
    return db.list_contacts(limit=limit, keyword=keyword)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
