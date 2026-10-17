"""DingTalk SQLite database reader and query layer."""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def format_timestamp(ts: Optional[int]) -> str:
    """Format millisecond timestamp to ISO datetime string."""
    if not ts:
        return ""
    try:
        # DingTalk timestamps are usually in milliseconds
        if ts > 100000000000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def parse_message_content(content_raw: Any, content_type: int) -> Dict[str, Any]:
    """Parse DingTalk message content JSON into a readable text and meta."""
    if not content_raw:
        return {"text": "", "meta": {}}

    data = {}
    if isinstance(content_raw, str):
        try:
            data = json.loads(content_raw)
        except Exception:
            return {"text": content_raw, "meta": {}}
    elif isinstance(content_raw, dict):
        data = content_raw

    text = ""
    # Extract common fields
    if "text" in data and data["text"]:
        text = str(data["text"])
    elif "content" in data and isinstance(data["content"], str):
        text = data["content"]
    elif "title" in data and data["title"]:
        text = str(data["title"])

    # Attachments
    attachments = data.get("attachments", [])
    files = []
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict):
                # Check markdown in extension (very common for rich messages / quoted messages in DingTalk)
                ext = att.get("extension")
                if ext:
                    if isinstance(ext, str):
                        try:
                            ext_obj = json.loads(ext)
                            if "markdown" in ext_obj and ext_obj["markdown"]:
                                text = (text + "\n" if text else "") + ext_obj["markdown"]
                            elif "title" in ext_obj and ext_obj["title"] and not text:
                                text = ext_obj["title"]
                        except Exception:
                            pass
                    elif isinstance(ext, dict):
                        if "markdown" in ext and ext["markdown"]:
                            text = (text + "\n" if text else "") + ext["markdown"]
                        elif "title" in ext and ext["title"] and not text:
                            text = ext["title"]

                fname = att.get("fileName") or att.get("title") or att.get("name")
                fsize = att.get("size")
                if fname:
                    files.append(f"{fname} ({fsize}B)" if fsize else fname)

    if files and not text:
        text = f"[文件/附件] {', '.join(files)}"
    elif files:
        text += f" [附件: {', '.join(files)}]"

    if not text:
        if content_type == 2:
            text = "[图片]"
        elif content_type in (3, 203):
            text = "[语音]"
        elif content_type in (102, 103, 104, 501):
            text = "[文件]"
        elif content_type == 1200:
            text = data.get("templateTitle") or "[卡片/富文本]"
        elif content_type == 2950:
            text = "[协同卡片/动态通知]"
        elif content_type == 3100:
            text = "[待办/日程]"
        else:
            text = f"[消息类型 {content_type}]"

    return {"text": text.strip(), "meta": data}


class DingTalkDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._user_cache: Dict[str, Dict[str, Any]] = {}

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_user_profiles(self) -> Dict[str, Dict[str, Any]]:
        if self._user_cache:
            return self._user_cache

        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    "SELECT uid, nick, realName, mobile, email FROM tbuser_profile_v2"
                ).fetchall()
                for r in rows:
                    uid = str(r["uid"])
                    self._user_cache[uid] = {
                        "uid": uid,
                        "nick": r["nick"] or "",
                        "real_name": r["realName"] or "",
                        "mobile": r["mobile"] or "",
                        "email": r["email"] or "",
                    }
        except Exception as e:
            logger.warning(f"Failed to fetch user profiles: {e}")

        return self._user_cache

    def resolve_sender(self, sender_id: Any) -> str:
        profiles = self.get_user_profiles()
        sid = str(sender_id)
        if sid in profiles:
            p = profiles[sid]
            return p["real_name"] or p["nick"] or sid
        return sid

    def list_conversations(self, limit: int = 50, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        profiles = self.get_user_profiles()
        sql = """
            SELECT cid, title, lastModify, unreadCount, status
            FROM tbconversation
        """
        params = []
        if keyword:
            sql += " WHERE title LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(keyword)}%")
        sql += " ORDER BY lastModify DESC LIMIT ?"
        params.append(limit)

        results = []
        with self.get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                cid = str(r["cid"])
                title = r["title"] or ""
                # If title is empty or numeric/id-like, try to resolve to user real name
                if not title or title.isdigit() or ":" in title:
                    parts = cid.split(":")
                    if len(parts) == 2:
                        # Private chat: format uid1:uid2
                        other_uid = parts[1] if parts[0] in profiles else parts[0]
                        if other_uid in profiles:
                            p = profiles[other_uid]
                            title = p["real_name"] or p["nick"] or title

                results.append({
                    "cid": cid,
                    "title": title or cid,
                    "last_modify": format_timestamp(r["lastModify"]),
                    "unread_count": r["unreadCount"] or 0,
                })
        return results

    def get_messages(
        self,
        cid: str,
        limit: int = 50,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get messages for a conversation across all sharded tables."""
        messages = []
        with self.get_connection() as conn:
            # First find which table(s) contain this cid
            target_tables = []
            for i in range(128):
                t = f"tbmsg_{i:03d}"
                try:
                    exists = conn.execute(
                        f"SELECT 1 FROM \"{t}\" WHERE cid = ? LIMIT 1", (cid,)
                    ).fetchone()
                    if exists:
                        target_tables.append(t)
                except sqlite3.OperationalError:
                    pass

            for t in target_tables:
                sql = f"""
                    SELECT primaryKey, cid, mid, senderId, createdAt, contentType, content
                    FROM "{t}"
                    WHERE cid = ? AND recallStatus = 0
                """
                params = [cid]
                if before_time:
                    sql += " AND createdAt < ?"
                    params.append(before_time)
                if after_time:
                    sql += " AND createdAt > ?"
                    params.append(after_time)

                sql += " ORDER BY createdAt DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(sql, params).fetchall()
                for r in rows:
                    parsed = parse_message_content(r["content"], r["contentType"])
                    sender_name = self.resolve_sender(r["senderId"])
                    messages.append({
                        "mid": str(r["mid"]),
                        "cid": str(r["cid"]),
                        "sender_id": str(r["senderId"]),
                        "sender_name": sender_name,
                        "created_at": format_timestamp(r["createdAt"]),
                        "content_type": r["contentType"],
                        "content": parsed["text"],
                        "raw_created_at": r["createdAt"],
                    })

        # Sort by created_at ascending
        messages.sort(key=lambda x: x.get("raw_created_at") or 0)
        # Remove raw field
        for m in messages:
            m.pop("raw_created_at", None)
        return messages[-limit:]

    def search_messages(
        self,
        keyword: str,
        cid: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """Search messages by keyword across all sharded tables.

        Scans **every** shard and then sorts globally by createdAt DESC, so the
        result really is the most recent ``limit`` matches. The previous version
        stopped as soon as the quota was filled while iterating shards
        tbmsg_000..127 in order, so the output was dominated by whatever the
        low-numbered shards happened to contain instead of the newest hits.
        """
        results: List[Dict[str, Any]] = []
        with self.get_connection() as conn:
            for i in range(128):
                t = f"tbmsg_{i:03d}"
                try:
                    sql = f"""
                        SELECT primaryKey, cid, mid, senderId, createdAt, contentType, content
                        FROM "{t}"
                        WHERE content LIKE ? ESCAPE '\\' AND recallStatus = 0
                    """
                    params = [f"%{_escape_like(keyword)}%"]
                    if cid:
                        sql += " AND cid = ?"
                        params.append(cid)

                    sql += " ORDER BY createdAt DESC LIMIT ?"
                    params.append(limit)
                    rows = conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    continue

                for r in rows:
                    parsed = parse_message_content(r["content"], r["contentType"])
                    if keyword.lower() in parsed["text"].lower() or keyword in str(r["content"]):
                        results.append({
                            "mid": str(r["mid"]),
                            "cid": str(r["cid"]),
                            "sender_id": str(r["senderId"]),
                            "sender_name": self.resolve_sender(r["senderId"]),
                            "created_at": format_timestamp(r["createdAt"]),
                            "content_type": r["contentType"],
                            "content": parsed["text"],
                            "_raw_ts": r["createdAt"] or 0,
                        })

        results.sort(key=lambda m: m["_raw_ts"], reverse=True)
        for m in results:
            m.pop("_raw_ts", None)
        return results[:limit]

    def list_contacts(self, limit: int = 100, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        profiles = self.get_user_profiles()
        results = []
        for p in profiles.values():
            if keyword:
                kw = keyword.lower()
                if kw not in p["real_name"].lower() and kw not in p["nick"].lower() and kw not in p["uid"]:
                    continue
            results.append(p)
            if len(results) >= limit:
                break
        return results
