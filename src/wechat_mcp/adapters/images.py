"""绿泡泡 4.x 图片渲染适配层。

解决的问题：消息正文里只存 ``[图片]`` 占位符，既没有 md5 也没有路径，
因此「消息 → 真实图片」必须另找依据。本模块给出两条实测验证过的通路。

一、未加密的真实缩略图（首选，无需任何密钥）
--------------------------------------------------
磁盘上存在一批**未加密**的真实图片::

    cache/{YYYY-MM}/Message/{chat_hash}/Thumb/{seq}_{unix_ts}_thumb.jpg

本机实测（345 会话 / 1,606 张真实图片）：

* ``chat_hash == md5(username)`` —— 104/104 命中，可直接由会话推目录；
* Thumb 文件名里的 ``unix_ts`` **等于消息的 create_time** ——
  精确命中 5/5、4/4、1/1，零误差。

所以「会话 username + 消息时间」即可唯一定位一张可渲染的真实图片，
覆盖范围是磁盘缓存仍在的月份（本机为最近约 3 个月）。

二、``.dat`` 私有容器（覆盖全量，但 V2 需要账号密钥）
--------------------------------------------------
聊天图片落盘为 ``msg/attach/{chat_hash}/{YYYY-MM}/Img/{md5}[_t|_h].dat``。
结构（本机 30/30 精确吻合）::

    [15 字节头] + [AES-ECB 段] + [16 字节固定标记] + [单字节 XOR 段]

    头 = 签名(6) | aes_size(u32 LE) | xor_size(u32 LE) | 填充(1)
    签名 V1 = b"\\x07\\x08V1\\x08\\x07"   V2 = b"\\x07\\x08V2\\x08\\x07"

* V1 的 AES 密钥是硬编码常量 ``md5("0")[:16]``，可完全离线解码；
* V2 的 AES 密钥是**账号级密钥**，只能从运行中的绿泡泡进程提取；
* XOR 段密钥对 ``_t`` 缩略图可由文件尾 ``FF D9`` 反推（本机恒为 0x2E）。

本机 51,993 个 ``.dat`` 全部是 V2，因此在拿到账号 AES 密钥之前，
``.dat`` 只能解析出结构（长度自校验 + XOR 密钥），无法还原像素。
这也是本模块把「未加密缩略图」作为首选通路的原因。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 常量

SIG_V1 = b"\x07\x08V1\x08\x07"
SIG_V2 = b"\x07\x08V2\x08\x07"

#: V1 的 AES 密钥为硬编码常量 = md5("0") 的前 16 字节
V1_AES_KEY = b"cfcd208495d565ef"

#: AES 段与 XOR 段之间的固定 16 字节标记（非图片数据，必须跳过）
V4_RAW_MARKER = bytes.fromhex("e8f45b657f6d049de701465372f40b47")

AES_BLOCK = 16
_HEADER_SIZE = 15
_MARKER_SIZE = len(V4_RAW_MARKER)

#: 本机实测的账号默认 XOR 密钥（_t 缩略图恒为 0x2E）
DEFAULT_XOR_KEY = 0x2E

#: Thumb 目录文件名：{序号}_{unix秒}_thumb.{ext}
_THUMB_RE = re.compile(r"^(\d+)_(\d+)_thumb\.(jpg|jpeg|png|gif|bmp|webp)$", re.I)

#: 真实图片扩展名（宿主 readFileContent 也按扩展名判定 isImage）
REAL_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico", ".svg")

#: 磁盘缩略图索引缓存：(chat_hash, "YYYY-MM") -> {unix_ts: abs_path}
_THUMB_INDEX: Dict[tuple, Dict[int, str]] = {}


# ---------------------------------------------------------------- 会话 → 目录哈希


def chat_hash_of(username: str) -> str:
    """会话 username → 磁盘目录哈希（实测 chat_hash == md5(username)）"""
    return hashlib.md5(str(username or "").encode("utf-8")).hexdigest()


def _thumb_index(files_root: Path, chash: str, month: str) -> Dict[int, str]:
    """读取并缓存某会话某月的 Thumb 目录索引（时间戳 → 绝对路径）"""
    key = (chash, month)
    cached = _THUMB_INDEX.get(key)
    if cached is not None:
        return cached

    index: Dict[int, str] = {}
    thumb_dir = files_root / "cache" / month / "Message" / chash / "Thumb"
    if thumb_dir.is_dir():
        try:
            for name in os.listdir(thumb_dir):
                match = _THUMB_RE.match(name)
                if match:
                    index[int(match.group(2))] = str(thumb_dir / name)
        except OSError:
            pass

    _THUMB_INDEX[key] = index
    return index


def thumb_path_for(files_root, username: str, create_time) -> str:
    """由「会话 username + 消息时间」定位未加密的真实缩略图绝对路径。

    Args:
        files_root: 绿泡泡文件根目录（config.get_files_root()）
        username: 会话 username（群为 ``xxx@chatroom``）
        create_time: 消息时间戳，秒或毫秒均可

    Returns:
        命中则返回绝对路径，否则返回空字符串。
    """
    if not files_root or not username or not create_time:
        return ""
    try:
        ts = int(create_time)
    except (TypeError, ValueError):
        return ""
    if ts > 10 ** 11:  # 毫秒 → 秒
        ts //= 1000
    if ts <= 0:
        return ""

    chash = chat_hash_of(username)
    root = Path(files_root)
    base = datetime.datetime.fromtimestamp(ts)
    # 缓存按月分目录，跨月边界时相邻月份也查一遍
    for day_delta in (0, -31, 31):
        month = (base + datetime.timedelta(days=day_delta)).strftime("%Y-%m")
        hit = _thumb_index(root, chash, month).get(ts)
        if hit:
            return hit
    return ""


# ---------------------------------------------------------------- .dat 容器


def parse_dat_header(data: bytes) -> Dict[str, Any]:
    """解析绿泡泡 4.x ``.dat`` 头部，并用文件长度做自校验。

    Returns:
        ``{version, aes_size, xor_size, pad, size_ok, error}``；
        ``version`` 为 1/2，非 ``.dat`` 时为 0 且 ``error`` 非空。
    """
    out: Dict[str, Any] = {
        "version": 0,
        "aes_size": 0,
        "xor_size": 0,
        "pad": 0,
        "size_ok": False,
        "error": "",
    }
    if len(data) < _HEADER_SIZE:
        out["error"] = "文件过小，不足以容纳 .dat 头部"
        return out

    sig = data[:6]
    if sig == SIG_V2:
        out["version"] = 2
    elif sig == SIG_V1:
        out["version"] = 1
    else:
        out["error"] = "不是绿泡泡 4.x .dat（头部签名不符）"
        return out

    out["aes_size"] = int.from_bytes(data[6:10], "little")
    out["xor_size"] = int.from_bytes(data[10:14], "little")
    out["pad"] = data[14]
    out["size_ok"] = (
        _HEADER_SIZE + out["aes_size"] + _MARKER_SIZE + out["xor_size"] == len(data)
    )
    return out


def derive_xor_key(data: bytes) -> Optional[int]:
    """由文件尾 ``FF D9`` 反推单字节 XOR 密钥（对 ``_t`` 缩略图可靠）。

    两端同时成立才返回密钥，否则返回 ``None``（原图末尾带微信页脚，
    不能这样推导）。
    """
    if len(data) < 2:
        return None
    from_ff = data[-2] ^ 0xFF
    from_d9 = data[-1] ^ 0xD9
    return from_ff if from_ff == from_d9 else None


def guess_mime(data: bytes) -> str:
    """按 magic 判断图片类型，非图片返回空串。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "image/heic"
    return ""


def decode_dat(data: bytes, aes_key: Optional[str] = None) -> Dict[str, Any]:
    """解码绿泡泡 4.x ``.dat`` → 图片字节。

    Args:
        data: ``.dat`` 原始字节
        aes_key: V2 文件的账号 AES 密钥（16 字节 ASCII 字符串）；
            V1 文件忽略此参数（使用硬编码常量）。

    Returns:
        ``{ok, data, mime, header, xor_key}`` 成功；
        ``{ok: False, need_key: True, ...}`` 表示缺少账号密钥；
        ``{ok: False, error}`` 其他失败。
    """
    header = parse_dat_header(data)
    if header["error"]:
        return {"ok": False, "error": header["error"], "header": header}
    if not header["size_ok"]:
        return {
            "ok": False,
            "error": "头部声明的分段长度与文件实际大小不符",
            "header": header,
        }

    aes_size = header["aes_size"]
    xor_size = header["xor_size"]

    aes_blob = data[_HEADER_SIZE:_HEADER_SIZE + aes_size]
    raw_off = _HEADER_SIZE + aes_size
    raw_blob = data[raw_off:raw_off + _MARKER_SIZE]
    xor_blob = data[raw_off + _MARKER_SIZE:raw_off + _MARKER_SIZE + xor_size]

    # 16 字节标记不是图片数据：整体等于标记则全剥，长于标记则只剥前缀
    if raw_blob[:len(V4_RAW_MARKER)] == V4_RAW_MARKER:
        raw_blob = raw_blob[len(V4_RAW_MARKER):]

    key = V1_AES_KEY if header["version"] == 1 else aes_key
    if not key:
        return {
            "ok": False,
            "need_key": True,
            "header": header,
            "error": "V2 文件需要账号 AES 密钥（需从运行中的绿泡泡进程提取）",
        }

    try:
        key_bytes = key.encode("ascii")[:AES_BLOCK] if isinstance(key, str) else bytes(key)[:AES_BLOCK]
    except UnicodeEncodeError:
        return {"ok": False, "error": "AES 密钥必须是 ASCII 字符串", "header": header}
    if len(key_bytes) != AES_BLOCK:
        return {"ok": False, "error": "AES 密钥必须是 16 字节", "header": header}

    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding as AesPadding
    except ImportError:
        return {"ok": False, "error": "缺少 pycryptodome，无法解密 AES 段", "header": header}

    try:
        decrypted = AES.new(key_bytes, AES.MODE_ECB).decrypt(aes_blob)
        try:
            decrypted = AesPadding.unpad(decrypted, AES_BLOCK)
        except Exception:
            # 无 padding 时 unpad 会抛异常，属正常情况
            pass
    except Exception as exc:  # noqa: BLE001 - 任何解密失败都应转成可读结果
        return {"ok": False, "error": f"AES 段解密失败: {exc}", "header": header}

    xor_key = derive_xor_key(data)
    if xor_key is None:
        xor_key = DEFAULT_XOR_KEY
    xored = bytes(b ^ xor_key for b in xor_blob)

    plain = decrypted + raw_blob + xored
    mime = guess_mime(plain)
    if not mime:
        return {
            "ok": False,
            "need_key": header["version"] == 2,
            "header": header,
            "xor_key": xor_key,
            "error": "解密结果不是可识别的图片（密钥可能不正确）",
        }

    # 微信会在 JPEG 末尾追加约 24 字节元数据页脚，截到 FFD9 得到干净图片
    if mime == "image/jpeg":
        idx = plain.rfind(b"\xff\xd9")
        if idx > 0:
            plain = plain[: idx + 2]

    return {"ok": True, "data": plain, "mime": mime, "header": header, "xor_key": xor_key}


# ---------------------------------------------------------------- 统一读取入口


def is_dat_path(path: str) -> bool:
    return str(path or "").lower().endswith(".dat")


def is_real_image_path(path: str) -> bool:
    return str(path or "").lower().endswith(REAL_IMAGE_EXTS)


def read_media_data_url(
    path: str, aes_key: Optional[str] = None, allowed_root: Optional[str] = None
) -> Dict[str, Any]:
    """把磁盘上的媒体文件读成可直接给 ``<img src>`` 用的 data URL。

    真实图片文件直接 base64；``.dat`` 走 :func:`decode_dat`。
    任何失败都返回 ``{ok: False, error}``，绝不抛异常。

    Args:
        path: 目标文件路径。
        aes_key: 可选，V2 ``.dat`` 的账号级 AES 密钥。
        allowed_root: 可选，允许读取的根目录。给出时只接受其下的文件，
            避免本函数被当成「任意文件读取器」使用。
    """
    p = Path(str(path or ""))

    if allowed_root:
        try:
            root = Path(str(allowed_root)).resolve()
            target = p.resolve()
        except OSError as exc:
            return {"ok": False, "error": f"路径解析失败: {exc}", "path": str(p)}
        if target != root and not target.is_relative_to(root):
            return {
                "ok": False,
                "error": "路径不在允许的根目录内，已拒绝读取",
                "path": str(p),
                "allowed_root": str(root),
            }

    if not p.is_file():
        return {"ok": False, "error": f"文件不存在: {p}", "path": str(p)}

    try:
        data = p.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"读取失败: {exc}", "path": str(p)}

    source = "file"
    if is_dat_path(str(p)):
        source = "dat"
        result = decode_dat(data, aes_key=aes_key)
        if not result.get("ok"):
            return {
                "ok": False,
                "path": str(p),
                "source": "dat",
                "need_key": bool(result.get("need_key")),
                "header": result.get("header"),
                "error": result.get("error", "解码失败"),
            }
        payload = result["data"]
        mime = result["mime"]
        header = result.get("header")
        xor_key = result.get("xor_key")
    else:
        mime = guess_mime(data)
        if not mime:
            return {"ok": False, "path": str(p), "error": "不是可识别的图片格式", "source": source}
        payload = data
        header = None
        xor_key = None

    return {
        "ok": True,
        "path": str(p),
        "source": source,
        "mime": mime,
        "bytes": len(payload),
        "header": header,
        "xor_key": xor_key,
        "data_url": f"data:{mime};base64," + base64.b64encode(payload).decode("ascii"),
    }
