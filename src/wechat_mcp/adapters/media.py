"""绿泡泡 4.x 媒体文件索引与收藏条目同步模块

覆盖两类此前"已解密但从未被使用"的数据：

- ``hardlink.db`` → ``media_files``
  图片 / 文件 / 视频的 md5 → 文件名 → 磁盘相对路径。
  微信 4.x 的实际布局（实测验证）：
    图片  msg/attach/{dir2id.username}/{YYYY-MM}/Img/{file_name}
    文件  msg/file/{YYYY-MM}/{file_name}
    视频  msg/video/{YYYY-MM}/{file_name}
- ``favorite.db`` → ``favorites``（content 为 XML，提取标题/描述/来源）
"""

import datetime
import logging
import re
import sqlite3
from pathlib import Path

from ..config import get_files_root
from ..parser import extract_tag_from_xml

logger = logging.getLogger(__name__)

# hardlink 三张表 → 媒体类型标识
_HARDLINK_TABLES = (
    ("image_hardlink_info_v4", "image"),
    ("file_hardlink_info_v4", "file"),
    ("video_hardlink_info_v4", "video"),
)


def _resolve_rel_path(kind: str, chat_hash: str | None, month: str, file_name: str) -> str:
    """按微信 4.x 磁盘布局拼出相对文件根目录的路径"""
    if not file_name or not month:
        return ""
    if kind == "image":
        if not chat_hash:
            return ""
        return f"msg/attach/{chat_hash}/{month}/Img/{file_name}"
    if kind == "video":
        return f"msg/video/{month}/{file_name}"
    return f"msg/file/{month}/{file_name}"


def sync_media_v4(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """从 hardlink.db 建立媒体文件索引（md5 → 文件名 / 磁盘路径）"""
    hardlink_db = decrypted_dir / "hardlink" / "hardlink.db"
    if not hardlink_db.exists():
        return

    files_root = get_files_root()

    try:
        conn = sqlite3.connect(f"file:{hardlink_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        # dir2id.rowid → 会话哈希目录名（图片按会话分目录存放）
        dir_map: dict[int, str] = {}
        if "dir2id" in tables:
            for row in conn.execute("SELECT rowid, username FROM dir2id"):
                if row[1]:
                    dir_map[row[0]] = row[1]

        count = 0
        for table, kind in _HARDLINK_TABLES:
            if table not in tables:
                continue
            try:
                rows = conn.execute(
                    f"SELECT md5, file_name, file_size, modify_time, dir1 FROM {table}"
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning(f"Error reading {table}: {exc}")
                continue

            for row in rows:
                md5 = row["md5"]
                if not md5:
                    continue
                month = ""
                if row["modify_time"]:
                    try:
                        month = datetime.datetime.fromtimestamp(row["modify_time"]).strftime("%Y-%m")
                    except (OverflowError, OSError, ValueError):
                        month = ""
                chat_hash = dir_map.get(row["dir1"]) if kind == "image" else None
                rel_path = _resolve_rel_path(kind, chat_hash, month, row["file_name"] or "")
                abs_path = str(Path(files_root) / rel_path) if (files_root and rel_path) else ""
                cur.execute(
                    """
                    INSERT OR REPLACE INTO media_files
                    (md5, kind, file_name, file_size, modify_time, chat_hash, month, rel_path, abs_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        md5,
                        kind,
                        row["file_name"],
                        row["file_size"],
                        row["modify_time"],
                        chat_hash,
                        month,
                        rel_path,
                        abs_path,
                    ),
                )
                count += 1
        conn.close()
        logger.info(f"Indexed {count} media files")
    except Exception as exc:
        logger.warning(f"Error building media index: {exc}")


def _extract_tag(xml_text: str, tag: str) -> str:
    """从收藏 XML 中提取首个非空标签文本（复用 parser 的统一实现，避免两处分叉）"""
    return extract_tag_from_xml(xml_text or "", tag)


def sync_favorites_v4(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """从 favorite.db 同步收藏条目（标题 / 描述 / 来源）"""
    fav_db = decrypted_dir / "favorite" / "favorite.db"
    if not fav_db.exists():
        return

    try:
        conn = sqlite3.connect(f"file:{fav_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "fav_db_item" not in tables:
            conn.close()
            return

        count = 0
        for row in conn.execute(
            "SELECT local_id, type, update_time, content, fromusr FROM fav_db_item"
        ):
            content = row["content"] or ""
            desc = _extract_tag(content, "desc") or _extract_tag(content, "pagedesc")
            title = _extract_tag(content, "title")
            if not title:
                # 图片 / 链接等类型的收藏没有 <title>，退化为描述摘要或类型占位
                title = desc[:40] if desc else f"[收藏类型 {row['type']}]"
            cur.execute(
                """
                INSERT OR REPLACE INTO favorites
                (local_id, type, update_time, title, description, from_user, raw_content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["local_id"],
                    row["type"],
                    row["update_time"],
                    title,
                    desc,
                    row["fromusr"] or _extract_tag(content, "fromusr"),
                    content,
                ),
            )
            count += 1
        conn.close()
        logger.info(f"Synced {count} favorites")
    except Exception as exc:
        logger.warning(f"Error syncing favorites: {exc}")


def sync_emoticons_v4(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """从 emoticon.db 建立表情包索引（md5 → 文字说明 / 所属表情包）"""
    emo_db = decrypted_dir / "emoticon" / "emoticon.db"
    if not emo_db.exists():
        return

    try:
        conn = sqlite3.connect(f"file:{emo_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        # 表情包 id → 名称
        packages: dict[str, str] = {}
        if "kStoreEmoticonPackageTable" in tables:
            for row in conn.execute(
                "SELECT package_id_, package_name_ FROM kStoreEmoticonPackageTable"
            ):
                if row[0]:
                    packages[row[0]] = row[1] or ""

        count = 0

        # 1) 非商店表情（自己添加的单张表情）
        if "kNonStoreEmoticonTable" in tables:
            for row in conn.execute(
                "SELECT md5, caption, product_id, thumb_url, cdn_url, designer_id "
                "FROM kNonStoreEmoticonTable"
            ):
                if not row["md5"]:
                    continue
                product_id = row["product_id"] or ""
                cur.execute(
                    """
                    INSERT OR REPLACE INTO emoticons
                    (md5, caption, package_id, package_name, kind, thumb_url, cdn_url, designer_id)
                    VALUES (?, ?, ?, ?, 'non_store', ?, ?, ?)
                    """,
                    (
                        row["md5"],
                        row["caption"] or "",
                        product_id,
                        packages.get(product_id, ""),
                        row["thumb_url"] or "",
                        row["cdn_url"] or "",
                        row["designer_id"] or "",
                    ),
                )
                count += 1

        # 2) 商店表情（带官方文字说明，可用于反查表情含义）
        if "kStoreEmoticonCaptionsTable" in tables:
            for row in conn.execute(
                "SELECT md5_, package_id_, caption_ FROM kStoreEmoticonCaptionsTable"
            ):
                if not row["md5_"]:
                    continue
                cur.execute(
                    """
                    INSERT OR REPLACE INTO emoticons
                    (md5, caption, package_id, package_name, kind, thumb_url, cdn_url, designer_id)
                    VALUES (?, ?, ?, ?, 'store', '', '', '')
                    """,
                    (
                        row["md5_"],
                        row["caption_"] or "",
                        row["package_id_"] or "",
                        packages.get(row["package_id_"], ""),
                    ),
                )
                count += 1

        conn.close()
        logger.info(f"Indexed {count} emoticons")
    except Exception as exc:
        logger.warning(f"Error building emoticon index: {exc}")
