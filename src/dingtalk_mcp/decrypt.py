"""DingTalk local database decryptor for V2 and V3."""

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_V3_PBKDF2_SALT = b"666DingTalk888"[:8]  # "666DingT"
_V3_PBKDF2_ITERATIONS = 1000
_V3_PBKDF2_DKLEN = 32
_AES_PAGE_SIZE = 4096


def detect_dingtalk_user() -> Tuple[Optional[str], Optional[str], int, Optional[str]]:
    """Auto-detect DingTalk user data directory, UID, version, and salt.

    Returns:
        (data_dir, uid, version, salt)
    """
    env_uid = os.environ.get("DINGTALK_UID", "").strip()
    env_dir = os.environ.get("DINGTALK_DATA_DIR", "").strip()

    if env_uid and env_dir:
        # normpath 去掉尾部分隔符：否则 basename(".../acct_v3/") == ""，
        # 会被误判成 V2，用错误的 key 解密（表现为 SQLite 头部校验失败）。
        env_dir = os.path.normpath(env_dir)
        ver = 3 if "_v3" in os.path.basename(env_dir) else 2
        salt = read_v3_salt(env_dir) if ver == 3 else None
        return env_dir, env_uid, ver, salt

    search_bases = []
    appdata = os.environ.get("APPDATA", "")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")

    if appdata:
        search_bases.append(os.path.join(appdata, "DingTalk"))
    if local_appdata and local_appdata != appdata:
        search_bases.append(os.path.join(local_appdata, "DingTalk"))
    if userprofile:
        search_bases.append(os.path.join(userprofile, "AppData", "Roaming", "DingTalk"))
        search_bases.append(os.path.join(userprofile, "AppData", "Local", "DingTalk"))

    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        search_bases.append(
            os.path.join(
                home,
                "Library",
                "Containers",
                "5ZSL2CJU2T.com.dingtalk.mac",
                "Data",
                "Library",
                "Application Support",
                "DingTalkMac",
            )
        )
        search_bases.append(os.path.join(home, "Library", "Application Support", "DingTalk"))
    elif sys.platform.startswith("linux"):
        search_bases.append(os.path.join(home, ".config", "DingTalk"))
        search_bases.append(os.path.join(home, ".local", "share", "DingTalk"))

    found_dirs = []
    for base in search_bases:
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            if entry.endswith(("_v2", "_v3")):
                full_path = os.path.join(base, entry)
                if os.path.isdir(full_path):
                    db_file = os.path.join(full_path, "DBFiles", "dingtalk.db")
                    if os.path.exists(db_file):
                        folder_id = entry.rsplit("_v", 1)[0]
                        version = 3 if entry.endswith("_v3") else 2
                        mtime = os.path.getmtime(db_file)
                        found_dirs.append((folder_id, full_path, mtime, version))

    if not found_dirs:
        return None, None, 2, None

    # Prefer most recently updated directory
    found_dirs.sort(key=lambda x: (x[3], x[2]), reverse=True)
    folder_id, path, _, version = found_dirs[0]

    uid = folder_id
    salt = None
    if version == 3:
        salt = read_v3_salt(path)
        # Check if environment provided uid
        if env_uid:
            uid = env_uid

    return path, uid, version, salt


def read_v3_salt(data_dir: str) -> Optional[str]:
    """Read the salt from V3's user_config file."""
    config_path = os.path.join(data_dir, "user_config")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        decoded = base64.b64decode(raw).decode("utf-8")
        data = json.loads(decoded)
        return data.get("salt")
    except Exception as e:
        logger.warning(f"Failed to read V3 salt: {e}")
        return None


def generate_key(uid: str, version: int = 2, salt_hex: Optional[str] = None) -> bytes:
    """Generate AES-128-ECB decryption key for DingTalk."""
    if version == 3:
        if not salt_hex:
            raise ValueError("V3 salt is required for key derivation")
        pwd = (uid + salt_hex).encode("utf-8")
        dk = hashlib.pbkdf2_hmac("sha1", pwd, _V3_PBKDF2_SALT, _V3_PBKDF2_ITERATIONS, dklen=_V3_PBKDF2_DKLEN)
        md5_hex = hashlib.md5(dk).hexdigest()
        return md5_hex[:16].encode("ascii")
    else:
        md5_hex = hashlib.md5(uid.encode("utf-8")).hexdigest()
        return md5_hex[:16].encode("ascii")


def decrypt_db_file(src_path: str, dst_path: str, key: bytes, is_wal: bool = False) -> None:
    """Decrypt a DingTalk SQLite or WAL file page by page using AES-128-ECB."""
    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_ECB)

    with open(src_path, "rb") as f:
        data = f.read()

    result = bytearray()
    if not is_wal:
        # Standard SQLite DB file: 4096-byte pages
        for offset in range(0, len(data), _AES_PAGE_SIZE):
            page = data[offset : offset + _AES_PAGE_SIZE]
            if len(page) == _AES_PAGE_SIZE:
                for i in range(0, _AES_PAGE_SIZE, 16):
                    result.extend(cipher.decrypt(page[i : i + 16]))
            else:
                result.extend(page)
    else:
        # SQLite WAL file: 32-byte plaintext header, then frames (24-byte plaintext header + 4096-byte page)
        if len(data) >= 32:
            result.extend(data[:32])
            frame_size = 24 + _AES_PAGE_SIZE
            for offset in range(32, len(data), frame_size):
                frame = data[offset : offset + frame_size]
                if len(frame) == frame_size:
                    frame_header = frame[:24]
                    page_data = frame[24:]
                    result.extend(frame_header)
                    for i in range(0, _AES_PAGE_SIZE, 16):
                        result.extend(cipher.decrypt(page_data[i : i + 16]))
                else:
                    result.extend(frame)
        else:
            result.extend(data)

    with open(dst_path, "wb") as f:
        f.write(result)


def decrypt_dingtalk(
    data_dir: str,
    uid: str,
    version: int,
    salt: Optional[str],
    output_dir: str,
) -> str:
    """Safely copy, decrypt and checkpoint DingTalk database into output_dir.

    Returns the path to the decrypted dingtalk.db.
    """
    key = generate_key(uid, version, salt)
    os.makedirs(output_dir, exist_ok=True)

    db_dir = os.path.join(data_dir, "DBFiles")
    src_db = os.path.join(db_dir, "dingtalk.db")
    src_wal = os.path.join(db_dir, "dingtalk.db-wal")

    if not os.path.isfile(src_db):
        raise FileNotFoundError(f"dingtalk.db not found in {db_dir}")

    # Copy to temp first to avoid locking
    temp_dir = tempfile.mkdtemp(prefix="dt_decrypt_")
    try:
        temp_enc_db = os.path.join(temp_dir, "enc.db")
        temp_enc_wal = os.path.join(temp_dir, "enc.db-wal")
        shutil.copy2(src_db, temp_enc_db)
        has_wal = False
        if os.path.isfile(src_wal) and os.path.getsize(src_wal) > 0:
            shutil.copy2(src_wal, temp_enc_wal)
            has_wal = True

        out_db = os.path.join(output_dir, "dingtalk.db")
        out_wal = os.path.join(output_dir, "dingtalk.db-wal")
        out_shm = os.path.join(output_dir, "dingtalk.db-shm")

        # 上一次运行残留的 WAL/SHM 会被 SQLite 叠加到刚解密出来的库上，
        # 直接把库读成 "database disk image is malformed"，必须先清掉。
        for stale in (out_wal, out_shm):
            try:
                os.remove(stale)
            except OSError:
                pass

        decrypt_db_file(temp_enc_db, out_db, key, is_wal=False)

        # Check valid header
        with open(out_db, "rb") as f:
            header = f.read(16)
        if header != b"SQLite format 3\x00":
            raise RuntimeError(
                f"Decryption failed: invalid SQLite header. Please check DingTalk UID ({uid})."
            )

        if has_wal:
            decrypt_db_file(temp_enc_wal, out_wal, key, is_wal=True)
            # Checkpoint WAL into DB
            try:
                conn = sqlite3.connect(out_db)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.close()
            except Exception as e:
                logger.warning(f"WAL checkpoint warning: {e}")
            # -shm 只是可重建的索引缓存，留下旧文件只会干扰后续读取
            try:
                os.remove(out_shm)
            except OSError:
                pass

        return out_db
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
