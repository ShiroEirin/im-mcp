"""绿泡泡 4.x (xwechat) 数据库解析同步模块

绿泡泡 4.x 数据特征：
- 数据库根目录通常包含 db_storage/ 或直接由 contact, session, message 分类目录组成
- contact.db:
    - contact 表: username, alias, remark, nick_name, local_type
    - chat_room 表: chat_room_name, user_name, display_name
- session.db:
    - SessionTable 表: username, summary, last_timestamp
- message_*.db:
    - 许多名为 Msg_<md5(username)> 的表存储各会话消息
    - Name2Id 表: rowid -> user_name (发送者/用户名映射)
    - 消息表字段: local_id, real_sender_id, local_type, create_time, message_content, source
"""

import sqlite3
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any

from ..parser import build_content

logger = logging.getLogger(__name__)


def is_v4_layout(decrypted_dir: Path) -> bool:
    """判断是否为绿泡泡 4.x 解密结构"""
    patterns = [
        decrypted_dir / "contact" / "contact.db",
        decrypted_dir / "db_storage" / "contact" / "contact.db",
        decrypted_dir / "session" / "session.db",
        decrypted_dir / "db_storage" / "session" / "session.db",
        decrypted_dir / "message",
        decrypted_dir / "db_storage" / "message",
    ]
    return any(p.exists() for p in patterns)


def sync_v4(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """执行绿泡泡 4.x 数据库同步"""
    # 1. 联系人与群成员解析
    contact_dir = decrypted_dir / "contact"
    if not contact_dir.exists():
        contact_dir = decrypted_dir / "db_storage" / "contact"

    if contact_dir.exists():
        for db_file in contact_dir.glob("*.db"):
            try:
                c_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                c_conn.row_factory = sqlite3.Row
                tables = [r[0] for r in c_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                if "contact" in tables:
                    # 动态检测 contact 列名 (4.x 为 local_type，兼容防御 type)
                    cols = [r[1] for r in c_conn.execute("PRAGMA table_info(contact)").fetchall()]
                    type_col = "local_type" if "local_type" in cols else ("type" if "type" in cols else "0")
                    rows = c_conn.execute(f"SELECT username, alias, remark, nick_name, {type_col} AS local_type FROM contact").fetchall()
                    for r in rows:
                        uname = r["username"]
                        disp = r["remark"] or r["nick_name"] or r["alias"] or uname
                        cur.execute("""
                            INSERT OR REPLACE INTO contacts (username, alias, display_name, type)
                            VALUES (?, ?, ?, ?)
                        """, (uname, r["alias"], disp, r["local_type"] or 0))

                # 群成员解析 (4.x 使用 chatroom_member + chat_room + name2id)
                if "chatroom_member" in tables and "chat_room" in tables and "name2id" in tables:
                    try:
                        sql = """
                            SELECT cr.username AS room_name, n.username AS member_name
                            FROM chatroom_member cm
                            JOIN chat_room cr ON cm.room_id = cr.id
                            JOIN name2id n ON cm.member_id = n.rowid
                        """
                        for r in c_conn.execute(sql):
                            if r["room_name"] and r["member_name"]:
                                cur.execute("""
                                    INSERT OR REPLACE INTO chatroom_members (chatroom_username, member_username, display_name)
                                    VALUES (?, ?, ?)
                                """, (r["room_name"], r["member_name"], ""))
                    except Exception as err:
                        logger.debug(f"Error parsing 4.x chatroom_member: {err}")
                elif "chat_room" in tables:
                    cols = [r[1] for r in c_conn.execute("PRAGMA table_info(chat_room)").fetchall()]
                    if "chat_room_name" in cols and "user_name" in cols:
                        for r in c_conn.execute("SELECT chat_room_name, user_name, display_name FROM chat_room"):
                            if r["chat_room_name"] and r["user_name"]:
                                cur.execute("""
                                    INSERT OR REPLACE INTO chatroom_members (chatroom_username, member_username, display_name)
                                    VALUES (?, ?, ?)
                                """, (r["chat_room_name"], r["user_name"], r["display_name"]))
                c_conn.close()
            except Exception as e:
                logger.warning(f"Error parsing contact db {db_file}: {e}")

    # 2. 会话解析
    session_dir = decrypted_dir / "session"
    if not session_dir.exists():
        session_dir = decrypted_dir / "db_storage" / "session"

    if session_dir.exists():
        for db_file in session_dir.glob("*.db"):
            try:
                s_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                s_conn.row_factory = sqlite3.Row
                cols = [r[1] for r in s_conn.execute("PRAGMA table_info(SessionTable)").fetchall()]
                time_col = "last_timestamp" if "last_timestamp" in cols else ("last_time" if "last_time" in cols else "0")
                
                for r in s_conn.execute(f"SELECT username, summary, {time_col} AS last_time FROM SessionTable"):
                    uname = r["username"]
                    if not uname:
                        continue
                    stype = "room" if "@chatroom" in uname or "@openim" in uname else ("gh" if uname.startswith("gh_") else "c2c")
                    # 匹配联系人中的昵称
                    cur.execute("SELECT display_name FROM contacts WHERE username = ?", (uname,))
                    c_row = cur.fetchone()
                    dname = c_row[0] if c_row else uname

                    cur.execute("""
                        INSERT OR REPLACE INTO sessions (username, type, display_name, last_time, last_msg_preview)
                        VALUES (?, ?, ?, ?, ?)
                    """, (uname, stype, dname, r["last_time"], r["summary"]))
                s_conn.close()
            except Exception as e:
                logger.warning(f"Error parsing session db {db_file}: {e}")

    # 3. 消息解析
    msg_dir = decrypted_dir / "message"
    if not msg_dir.exists():
        msg_dir = decrypted_dir / "db_storage" / "message"

    if msg_dir.exists():
        # 微信 4.x 的消息库不止 message_*.db：biz_message_*.db（服务号/公众号等）
        # 同样存放 Msg_<md5> 消息表（本机实测 189 张表 / 5191 条消息）。原先只
        # glob message_*.db 会整库漏掉。改为遍历目录下全部 .db，由内部的 Msg_
        # 表探测决定是否真有消息，将来新增分片也能自动覆盖。
        for db_file in sorted(msg_dir.glob("*.db")):
            try:
                m_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                m_conn.row_factory = sqlite3.Row

                # 建立 Name2Id 映射与 MD5 反向字典
                name2id: Dict[int, str] = {}
                hash_to_user: Dict[str, str] = {}
                try:
                    for rowid, uname in m_conn.execute("SELECT rowid, user_name FROM Name2Id"):
                        if uname:
                            name2id[rowid] = uname
                            h = hashlib.md5(uname.encode("utf-8")).hexdigest()
                            hash_to_user[h] = uname
                except Exception:
                    pass

                # 遍历所有 Msg_<hash> 表
                # 用 GLOB 而非 LIKE：LIKE 的 '_' 是单字符通配符，会误匹配 MsgX… 等无关表
                tables = [r[0] for r in m_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'")]

                for tbl in tables:
                    tbl_hash = tbl[4:].strip().lower()  # 去掉 'Msg_' 前缀
                    session_name = hash_to_user.get(tbl_hash, "")

                    # 探测表字段
                    tbl_cols = [r[1] for r in m_conn.execute(f"PRAGMA table_info('{tbl}')").fetchall()]
                    type_col = "local_type" if "local_type" in tbl_cols else ("message_type" if "message_type" in tbl_cols else "1")
                    sender_col = "real_sender_id" if "real_sender_id" in tbl_cols else "NULL"
                    source_col = "source" if "source" in tbl_cols else "NULL"

                    query_sql = f"""
                        SELECT local_id, {sender_col} AS real_sender_id, {type_col} AS local_type,
                               create_time, message_content, {source_col} AS source
                        FROM '{tbl}'
                    """
                    for r in m_conn.execute(query_sql):
                        real_sid = r["real_sender_id"]
                        sender_name = name2id.get(real_sid, session_name) if real_sid is not None else session_name
                        if not sender_name:
                            sender_name = session_name or "unknown"
                        
                        target_session = session_name or f"chat_{tbl_hash}"
                        content = build_content(r["local_type"], r["message_content"], r["source"])

                        # 用 UPSERT 而非 INSERT OR REPLACE：REPLACE 是「先删后插」，
                        # 而 recursive_triggers 默认关闭，AFTER DELETE 触发器不会触发，
                        # messages_fts 会残留旧词条；且新行会拿到新的自增 id，导致主键
                        # 每次 ETL 都漂移、索引无界膨胀。UPSERT 原地更新，rowid 稳定。
                        cur.execute("""
                            INSERT INTO messages
                            (source_file, local_id, session_username, sender_username, create_time, local_type, content)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(source_file, session_username, local_id) DO UPDATE SET
                                sender_username = excluded.sender_username,
                                create_time     = excluded.create_time,
                                local_type      = excluded.local_type,
                                content         = excluded.content
                        """, (db_file.name, r["local_id"], target_session, sender_name, r["create_time"], r["local_type"], content))

                m_conn.close()
            except Exception as e:
                logger.warning(f"Error parsing message db {db_file}: {e}")
