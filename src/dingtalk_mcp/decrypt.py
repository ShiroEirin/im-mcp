"""DingTalk local database decryptor for V2 and V3."""

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import re
from typing import Any, Dict, List, Optional, Set, Tuple

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
        # If user explicitly supplied DINGTALK_UID, use it
        if env_uid:
            uid = env_uid
        else:
            # V3 folder name is a hash, whereas PBKDF2 requires the real integer UID.
            # Try auto-detecting the real UID from DingTalk log files.
            detected_uid = find_v3_uid_from_logs(path, salt)
            if detected_uid:
                logger.info(f"Auto-detected DingTalk V3 real UID: {detected_uid}")
                uid = detected_uid

    return path, uid, version, salt


def find_v3_uid_from_logs(data_dir: str, salt: Optional[str]) -> Optional[str]:
    """Scan DingTalk log files for candidate UIDs and verify against the DB header."""
    if not salt:
        return None

    db_path = os.path.join(data_dir, "DBFiles", "dingtalk.db")
    if not os.path.isfile(db_path):
        return None

    try:
        with open(db_path, "rb") as f:
            sample_page = f.read(_AES_PAGE_SIZE)
        if len(sample_page) < _AES_PAGE_SIZE:
            return None
    except Exception as e:
        logger.warning(f"Failed to read sample page from {db_path}: {e}")
        return None

    # Search for log directories relative to data_dir or parent DingTalk root
    parent_dir = os.path.dirname(data_dir)
    possible_log_dirs = [
        os.path.join(parent_dir, "log"),
        os.path.join(data_dir, "log"),
        os.path.join(parent_dir, "holmeslogs"),
    ]

    candidates: Set[str] = set()
    uid_patterns = [
        re.compile(r'(?:real_uid|myOpenId|uid|userId|openId)[\"\'\s:=]+([0-9]{6,12})', re.IGNORECASE),
        re.compile(r'success\s+uid:([0-9]{6,12})', re.IGNORECASE),
    ]

    for log_dir in possible_log_dirs:
        if not os.path.isdir(log_dir):
            continue
        try:
            for root, _, files in os.walk(log_dir):
                for file_name in files:
                    if file_name.endswith((".log", ".txt")):
                        file_path = os.path.join(root, file_name)
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as lf:
                                for line in lf:
                                    for pattern in uid_patterns:
                                        for match in pattern.finditer(line):
                                            cand = match.group(1)
                                            if cand not in candidates:
                                                candidates.add(cand)
                                                if _verify_v3_uid(cand, salt, sample_page):
                                                    return cand
                        except Exception:
                            continue
        except Exception:
            continue

    # Try all gathered candidates if not returned early
    for cand in candidates:
        if _verify_v3_uid(cand, salt, sample_page):
            return cand

    return None


def _verify_v3_uid(uid: str, salt: str, sample_page: bytes) -> bool:
    """Check if the candidate UID successfully decrypts page 1 to a valid SQLite header."""
    try:
        from Crypto.Cipher import AES

        key = generate_key(uid, version=3, salt_hex=salt)
        cipher = AES.new(key, AES.MODE_ECB)
        dec = bytearray()
        for i in range(0, min(len(sample_page), _AES_PAGE_SIZE), 16):
            dec.extend(cipher.decrypt(sample_page[i : i + 16]))
        return bytes(dec[:16]) == b"SQLite format 3\x00"
    except Exception:
        return False


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


def _wal_cksum(data: bytes, s0: int = 0, s1: int = 0, is_big_endian: bool = False) -> Tuple[int, int]:
    """Compute SQLite WAL 32-bit checksum pair (s0, s1) over 8-byte steps."""
    fmt = ">II" if is_big_endian else "<II"
    for i in range(0, len(data), 8):
        x0, x1 = struct.unpack(fmt, data[i : i + 8])
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def decrypt_db_file(src_path: str, dst_path: str, key: bytes, is_wal: bool = False) -> int:
    """Decrypt a DingTalk SQLite or WAL file page by page using AES-128-ECB.

    Returns the number of frames decrypted if is_wal=True, else page count.
    """
    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_ECB)

    with open(src_path, "rb") as f:
        data = f.read()

    result = bytearray()
    count = 0
    if not is_wal:
        # Standard SQLite DB file: 4096-byte pages
        for offset in range(0, len(data), _AES_PAGE_SIZE):
            page = data[offset : offset + _AES_PAGE_SIZE]
            if len(page) == _AES_PAGE_SIZE:
                for i in range(0, _AES_PAGE_SIZE, 16):
                    result.extend(cipher.decrypt(page[i : i + 16]))
                count += 1
            else:
                result.extend(page)
    else:
        # SQLite WAL file: 32-byte header + frames (24-byte header + 4096-byte page)
        # Note: DingTalk WAL frames have their checksum calculated over CIPHERTEXT pages.
        # SQLite rejects the WAL if the checksum does not match decrypted pages.
        # We must recompute SQLite WAL checksums over the decrypted pages using the header magic endianness.
        if len(data) >= 32:
            hdr = bytearray(data[:32])
            magic = struct.unpack(">I", hdr[:4])[0]
            is_be = (magic & 1) == 1

            # Recompute WAL header checksum (first 24 bytes)
            s0, s1 = _wal_cksum(hdr[:24], 0, 0, is_big_endian=is_be)
            hdr[24:32] = struct.pack(">II", s0, s1)
            result.extend(hdr)

            frame_size = 24 + _AES_PAGE_SIZE
            for offset in range(32, len(data), frame_size):
                frame = data[offset : offset + frame_size]
                if len(frame) == frame_size:
                    fhdr = bytearray(frame[:24])
                    enc_page = frame[24:]
                    dec_page = bytearray()
                    for j in range(0, _AES_PAGE_SIZE, 16):
                        dec_page.extend(cipher.decrypt(enc_page[j : j + 16]))

                    # Recompute checksum over frame header (first 8 bytes) + decrypted page
                    a0, a1 = _wal_cksum(fhdr[:8], s0, s1, is_big_endian=is_be)
                    b0, b1 = _wal_cksum(dec_page, a0, a1, is_big_endian=is_be)
                    fhdr[16:24] = struct.pack(">II", b0, b1)
                    s0, s1 = b0, b1

                    result.extend(fhdr)
                    result.extend(dec_page)
                    count += 1
                else:
                    result.extend(frame)
        else:
            result.extend(data)

    with open(dst_path, "wb") as f:
        f.write(result)

    return count


def _merge_wal_frames_into_db(db_path: str, enc_wal_path: str, cipher) -> int:
    """Fallback merge: directly replay decrypted WAL frames into database pages.

    This handles cases where the host SQLite library refuses to checkpoint due to
    corrupted frames or unrecognized WAL state.
    Returns the number of frames merged.
    """
    pages = {}
    with open(db_path, "rb") as f:
        db_data = f.read()
    for i in range(0, len(db_data), _AES_PAGE_SIZE):
        pg = i // _AES_PAGE_SIZE + 1
        pages[pg] = db_data[i : i + _AES_PAGE_SIZE]

    frames_applied = 0
    with open(enc_wal_path, "rb") as f:
        hdr = f.read(32)
        if len(hdr) < 32:
            return 0

        while True:
            fhdr = f.read(24)
            if len(fhdr) < 24:
                break
            # SQLite WAL frame header pgno and dbsize are always big-endian (>II)
            pgno, dbsize = struct.unpack(">II", fhdr[:8])
            page_data = f.read(_AES_PAGE_SIZE)
            if len(page_data) < _AES_PAGE_SIZE:
                break
            dec_page = bytearray()
            for j in range(0, _AES_PAGE_SIZE, 16):
                dec_page.extend(cipher.decrypt(page_data[j : j + 16]))
            pages[pgno] = bytes(dec_page)
            frames_applied += 1

    if not pages:
        return 0

    max_p = max(pages.keys())
    with open(db_path, "wb") as f:
        for p in range(1, max_p + 1):
            if p in pages:
                f.write(pages[p])
            else:
                f.write(b"\x00" * _AES_PAGE_SIZE)

    return frames_applied


def decrypt_dingtalk_with_report(
    data_dir: str,
    uid: str,
    version: int,
    salt: Optional[str],
    output_dir: str,
) -> Tuple[str, Dict[str, Any]]:
    """Safely copy, decrypt and checkpoint DingTalk database into output_dir.

    Returns:
        (out_db_path, report_dict)
    """
    from Crypto.Cipher import AES

    key = generate_key(uid, version, salt)
    cipher = AES.new(key, AES.MODE_ECB)
    os.makedirs(output_dir, exist_ok=True)

    db_dir = os.path.join(data_dir, "DBFiles")
    src_db = os.path.join(db_dir, "dingtalk.db")
    src_wal = os.path.join(db_dir, "dingtalk.db-wal")

    if not os.path.isfile(src_db):
        raise FileNotFoundError(f"dingtalk.db not found in {db_dir}")

    report: Dict[str, Any] = {
        "db_path": "",
        "wal_frames_total": 0,
        "wal_frames_applied": 0,
        "checkpoint_method": "none",
        "stale": False,
    }

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
            total_frames = decrypt_db_file(temp_enc_wal, out_wal, key, is_wal=True)
            report["wal_frames_total"] = total_frames

            checkpoint_ok = False
            try:
                conn = sqlite3.connect(out_db)
                # Use FULL checkpoint to get accurate log_frames and ckpt_frames before truncating
                ckpt_res = conn.execute("PRAGMA wal_checkpoint(FULL);").fetchone()
                if ckpt_res and len(ckpt_res) >= 3:
                    busy, log_frames, ckpt_frames = ckpt_res[0], ckpt_res[1], ckpt_res[2]
                    if total_frames == 0:
                        checkpoint_ok = True
                        report["checkpoint_method"] = "none"
                        report["wal_frames_applied"] = 0
                    elif busy == 0 and ckpt_frames == log_frames and log_frames > 0:
                        checkpoint_ok = True
                        report["checkpoint_method"] = "native"
                        report["wal_frames_applied"] = ckpt_frames
                        # Now truncate cleanly
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    else:
                        logger.warning(
                            f"Native WAL checkpoint partial/busy/zero: busy={busy}, log={log_frames}, ckpt={ckpt_frames}, total={total_frames}"
                        )
                conn.close()
            except Exception as e:
                logger.warning(f"Native WAL checkpoint warning: {e}")

            # -shm 只是可重建的索引缓存，留下旧文件只会干扰后续读取
            try:
                os.remove(out_shm)
            except OSError:
                pass

            # If native checkpoint was not fully successful, fallback to manual frame reassembly
            if not checkpoint_ok:
                logger.info("Applying direct WAL frame reassembly fallback...")
                applied = _merge_wal_frames_into_db(out_db, temp_enc_wal, cipher)
                report["checkpoint_method"] = "fallback"
                report["wal_frames_applied"] = applied
                if os.path.exists(out_wal):
                    try:
                        os.remove(out_wal)
                    except Exception:
                        pass

        report["db_path"] = out_db
        return out_db, report
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    out_db, _ = decrypt_dingtalk_with_report(data_dir, uid, version, salt, output_dir)
    return out_db
