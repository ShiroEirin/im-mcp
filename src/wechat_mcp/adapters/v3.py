"""绿泡泡 3.x 数据库解析同步模块

绿泡泡 3.x 数据特征：
- 数据库位于 MicroMsg.db, MSG0.db, MSG1.db, MediaMSG0.db 等
- MicroMsg.db:
    - Contact 表: UserName, Alias, Remark, NickName, Type
    - ChatRoom 表: ChatRoomName, UserName, DisplayName
- MSG*.db:
    - MSG 表集中存储所有聊天记录: localId, StrTalker, IsSender, CreateTime, Type, SubType, StrContent, BytesExtra, CompressContent
"""

import sqlite3
import re
import logging
from pathlib import Path

from ..parser import build_content

logger = logging.getLogger(__name__)


def _extract_v3_group_sender(bytes_extra) -> str:
    """3.x 群聊的发言人不单独成列，而是编码在 BytesExtra 二进制里。

    尽力提取 wxid_/gh_ 形式的标识；提取不到就返回空串，由调用方回退为会话 ID
    （即修复前的行为），因此不会比现状更差。
    """
    if not bytes_extra:
        return ""
    if isinstance(bytes_extra, str):
        bytes_extra = bytes_extra.encode("utf-8", errors="ignore")
    if not isinstance(bytes_extra, (bytes, bytearray)):
        return ""
    match = re.search(rb"(wxid_[A-Za-z0-9_\-]{3,}|gh_[A-Za-z0-9_\-]{3,})", bytes(bytes_extra))
    return match.group(1).decode("ascii", errors="ignore") if match else ""


def is_v3_layout(decrypted_dir: Path) -> bool:
    """判断是否为绿泡泡 3.x 解密结构"""
    patterns = [
        decrypted_dir / "MicroMsg.db",
        decrypted_dir / "Msg" / "MicroMsg.db",
        decrypted_dir / "Multi" / "MSG0.db",
        decrypted_dir / "Msg" / "Multi" / "MSG0.db",
        decrypted_dir / "MSG0.db",
    ]
    return any(p.exists() for p in patterns)


def sync_v3(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """执行绿泡泡 3.x 数据库同步"""
    # 查找 MicroMsg.db
    micromsg_candidates = list(decrypted_dir.rglob("MicroMsg.db"))
    for db_file in micromsg_candidates:
        try:
            c_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            c_conn.row_factory = sqlite3.Row
            tables = [r[0] for r in c_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]

            # 联系人表 (Contact)
            if "Contact" in tables:
                for r in c_conn.execute("SELECT UserName, Alias, Remark, NickName, Type FROM Contact"):
                    uname = r["UserName"]
                    if not uname:
                        continue
                    disp = r["Remark"] or r["NickName"] or r["Alias"] or uname
                    cur.execute("""
                        INSERT OR REPLACE INTO contacts (username, alias, display_name, type)
                        VALUES (?, ?, ?, ?)
                    """, (uname, r["Alias"], disp, r["Type"] or 0))

            # 群成员表 (ChatRoom)
            if "ChatRoom" in tables:
                for r in c_conn.execute("SELECT ChatRoomName, UserName, DisplayName FROM ChatRoom"):
                    if r["ChatRoomName"] and r["UserName"]:
                        cur.execute("""
                            INSERT OR REPLACE INTO chatroom_members (chatroom_username, member_username, display_name)
                            VALUES (?, ?, ?)
                        """, (r["ChatRoomName"], r["UserName"], r["DisplayName"]))

            # 会话表 (Session)
            if "Session" in tables:
                cols = [r[1] for r in c_conn.execute("PRAGMA table_info(Session)").fetchall()]
                time_col = "CreateTime" if "CreateTime" in cols else "0"
                content_col = "Content" if "Content" in cols else "''"
                # 预载联系人昵称，避免逐会话回查（N+1）
                name_by_user = {
                    row[0]: row[1]
                    for row in cur.execute("SELECT username, display_name FROM contacts")
                    if row[0]
                }
                for r in c_conn.execute(f"SELECT strUsrName, {time_col} AS last_time, {content_col} AS summary FROM Session"):
                    uname = r["strUsrName"]
                    if not uname:
                        continue
                    stype = "room" if "@chatroom" in uname or "@openim" in uname else ("gh" if uname.startswith("gh_") else "c2c")
                    dname = name_by_user.get(uname) or uname

                    cur.execute("""
                        INSERT OR REPLACE INTO sessions (username, type, display_name, last_time, last_msg_preview)
                        VALUES (?, ?, ?, ?, ?)
                    """, (uname, stype, dname, r["last_time"], r["summary"]))

            c_conn.close()
        except Exception as e:
            logger.warning(f"Error parsing 3.x MicroMsg.db {db_file}: {e}")

    # 查找 MSG*.db
    # 3.x 的消息库名不止 MSG*.db：MediaMSG*.db（媒体消息）同样可能承载消息表。
    # 统一用 *MSG*.db 覆盖，由内部的 MSG/ChatCRMsg 表探测过滤，因此多匹配到的
    # MicroMsg.db（只有 Contact/ChatRoom/Session 表）会被安全跳过。
    msg_db_files = sorted(set(list(decrypted_dir.rglob("*MSG*.db")) + list(decrypted_dir.rglob("ChatMsg.db"))))
    for db_file in msg_db_files:
        try:
            m_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            m_conn.row_factory = sqlite3.Row
            tables = [r[0] for r in m_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            target_table = "MSG" if "MSG" in tables else ("ChatCRMsg" if "ChatCRMsg" in tables else None)

            if target_table:
                cols = [r[1] for r in m_conn.execute(f"PRAGMA table_info('{target_table}')").fetchall()]
                talker_col = "StrTalker" if "StrTalker" in cols else ("TalkerId" if "TalkerId" in cols else "''")
                content_col = "StrContent" if "StrContent" in cols else "''"
                type_col = "Type" if "Type" in cols else "1"
                time_col = "CreateTime" if "CreateTime" in cols else "0"
                extra_col = "BytesExtra" if "BytesExtra" in cols else "NULL"

                for r in m_conn.execute(
                    f"SELECT localId, {talker_col} AS StrTalker, IsSender, {type_col} AS Type, "
                    f"{time_col} AS CreateTime, {content_col} AS StrContent, {extra_col} AS BytesExtra "
                    f"FROM '{target_table}'"
                ):
                    session_name = r["StrTalker"]
                    if not session_name:
                        continue
                    is_sender = r["IsSender"]
                    if is_sender == 1:
                        sender_name = "me"
                    else:
                        # 群聊里 StrTalker 是群 ID，真实发言人在 BytesExtra 中；
                        # 提取失败则回退为群 ID（即修复前的行为）
                        sender_name = _extract_v3_group_sender(r["BytesExtra"]) or session_name
                    # 直接存 StrContent 会绕过 build_content，导致 [文件]/[图片] 等
                    # 标签缺失、search_shared_files 在 3.x 数据上恒为空。统一走同一入口。
                    content = build_content(r["Type"], r["StrContent"], None)

                    cur.execute("""
                        INSERT INTO messages
                        (source_file, local_id, session_username, sender_username, create_time, local_type, content)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_file, session_username, local_id) DO UPDATE SET
                            sender_username = excluded.sender_username,
                            create_time     = excluded.create_time,
                            local_type      = excluded.local_type,
                            content         = excluded.content
                    """, (db_file.name, r["localId"], session_name, sender_name, r["CreateTime"], r["Type"], content))

            m_conn.close()
        except Exception as e:
            logger.warning(f"Error parsing 3.x MSG db {db_file}: {e}")
