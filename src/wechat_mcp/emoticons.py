"""微信自定义表情（表情包）资源解析。

背景：微信 4.x 的表情消息在本地库里只存一个 ``md5`` 标识（原始 XML 的
``<emoji ... md5="...">``），真正的图片文件存在账号目录的
``business/emoticon/Persist/<两位>/<md5>:<ext>``，且被账号级密钥加密：

    key = MD5(f"{seed}{wxid}EMOTICON").digest()
    plaintext = AES-128-CBC(key, iv=key).decrypt(ciphertext)

其中 ``seed`` 落在 ``0..2^32`` 空间里，需要用 GPU/SIMD 暴力搜索（社区工具
wxemo 完成）。因此本模块**不在 MCP 进程里做解密**：由 ``prepare_emoticons()``
调用外部 wxemo 一次性把明文表情导出成扁平表情库
（``~/.wechat_mcp/emoticons/<md5>.<ext>``），之后 ``get_emoticon_image()``
只在这个库里按 md5 取文件 —— MCP 侧保持零加密依赖、零外部二进制调用。

未准备表情库时，工具返回可操作的提示而不是报错。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import USER_WECHAT_DIR

# 明文表情库：<md5>.<ext> 扁平存放（便于按 md5 直查）
EMOTICON_DIR = USER_WECHAT_DIR / "emoticons"

# wxgf 私有容器的待转码缓存（转码很慢，因此不批量做，改在首次取用时按需转一个）
WXGF_CACHE_DIR = EMOTICON_DIR / "_wxgf"

# wxemo 导出的可直接渲染格式（.wxgf 是微信私有容器，需解码后才能用）
_IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# wxemo 的 decode 步骤产物（wxgf → PNG/APNG）
_DECODED_EXTS = {".png"}


def _find_wxemo() -> Optional[str]:
    """定位 wxemo 可执行文件：环境变量 ``WXEMO_BIN`` → 约定目录 → PATH。"""
    env = (os.environ.get("WXEMO_BIN") or "").strip()
    if env and os.path.isfile(env):
        return env
    # 约定位置：与 all_keys.json 同级的 tools/ 下，用户拷进来即可，无需配 PATH
    bundled = USER_WECHAT_DIR / "tools" / "wxemo" / ("wxemo.exe" if os.name == "nt" else "wxemo")
    if bundled.is_file():
        return str(bundled)
    for name in ("wxemo", "wxemo.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _silent_wechat_pids() -> List[int]:
    """静默取 Weixin.exe 的 PID 列表。

    不能复用 ``key_tool._get_pids_windows()``：那个函数会 print 进程列表，
    而 MCP 走 stdio 传 JSON-RPC —— 任何 stdout 输出都会污染协议管道。
    """
    if sys.platform != "win32":
        return []
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 - 拿不到进程列表不影响原生格式表情
        return []
    pids: List[int] = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _wechat_install_dir() -> Optional[Path]:
    """从运行中的 Weixin.exe 取安装目录（VoipEngine.dll 就在这里）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        for pid in _silent_wechat_pids():
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return Path(buf.value).parent
            finally:
                kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 拿不到安装目录不影响原生格式表情
        return None
    return None


def _find_decoder_lib() -> Optional[str]:
    """定位微信原生 WXAM 解码库（VoipEngine.dll），用于把 wxgf 转成 PNG。"""
    env = (os.environ.get("WXEMO_DECODER_LIB") or "").strip()
    if env and os.path.isfile(env):
        return env
    install = _wechat_install_dir()
    if not install:
        return None
    try:
        hits = sorted(install.glob("**/VoipEngine.dll"), key=lambda p: p.stat().st_size, reverse=True)
        return str(hits[0]) if hits else None
    except OSError:
        return None


def find_emoticon_file(md5: str) -> Optional[Path]:
    """在明文表情库里按 md5 找文件（任意受支持的扩展名）。"""
    if not md5:
        return None
    target = str(md5).strip().lower()
    if not EMOTICON_DIR.is_dir():
        return None
    for path in EMOTICON_DIR.glob(f"{target}.*"):
        if path.is_file() and path.suffix.lower() in (_IMAGE_EXTS | _DECODED_EXTS):
            return path
    return None


def _decode_wxgf_single(md5: str, wxgf_path: Path, timeout_seconds: float = 120.0) -> Optional[Path]:
    """把单个 wxgf 转成 PNG 并入库，返回 PNG 路径（失败返回 None）。

    wxgf 是微信私有容器（内含 HEVC），转码需要微信自带的解码库、单张约 0.3 秒。
    批量转 1800+ 张要十分钟，所以改成首次取用时转一个、结果落库，后续直接命中。
    """
    wxemo = _find_wxemo()
    decoder = _find_decoder_lib()
    if not wxemo or not decoder:
        return None
    with tempfile.TemporaryDirectory(prefix="wxgf-") as tmp:
        src = Path(tmp) / "in"
        src.mkdir()
        shutil.copy2(wxgf_path, src / f"{md5}.wxgf")
        out = Path(tmp) / "out"
        result = _run_wxemo(
            wxemo,
            ["decode", str(src), "--out", str(out), "--decoder-lib", decoder],
            timeout_seconds,
        )
        if not result.get("ok"):
            return None
        for png in out.rglob("*.png"):
            dest = EMOTICON_DIR / f"{md5}.png"
            shutil.copy2(png, dest)
            return dest
    return None


def get_emoticon_image(md5: str) -> Dict[str, Any]:
    """按表情 md5 返回本地明文图片路径。

    返回 ``{"success": True, "md5", "file_path", "format", "size_bytes"}``；
    未命中时返回 ``error`` + 可操作提示（表情没缓存 / 表情库未准备）。
    """
    target = str(md5 or "").strip().lower()
    if not target:
        return {"error": "缺少表情 md5"}

    path = find_emoticon_file(target)
    if not path:
        # 明文库里没有：若之前缓存过 wxgf 原容器，现转一个（首次约 0.3s，之后命中）
        cached = WXGF_CACHE_DIR / f"{target}.wxgf"
        if cached.is_file():
            path = _decode_wxgf_single(target, cached)

    if path:
        return {
            "success": True,
            "md5": target,
            "file_path": str(path),
            "format": path.suffix.lstrip(".").lower(),
            "size_bytes": path.stat().st_size,
        }

    if not EMOTICON_DIR.is_dir():
        return {
            "error": "表情库尚未准备",
            "md5": target,
            "hint": "调用 prepare_emoticons 生成明文表情库（需要 wxemo，一次性 GPU 搜索）。",
        }
    return {
        "error": "本地表情库中没有这个表情",
        "md5": target,
        "hint": "该表情可能已被微信清理，或尚未在本地缓存过；重新在微信里收发一次即可。",
    }


def _wxemo_config_dir() -> Path:
    env = (os.environ.get("WXEMO_CONFIG_DIR") or "").strip()
    return Path(env) if env else Path.home() / ".config" / "wxemo"


def _read_emoticon_key() -> Optional[str]:
    """从 wxemo 的授权文件里取账号表情密钥（hex，32 字符）。

    wxemo 把搜索到的 seed 与派生 key 明文存在自己的配置目录，本模块直接复用，
    避免自己再实现一遍 seed 搜索。
    """
    auth = _wxemo_config_dir() / "auth.json"
    if not auth.is_file():
        return None
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置损坏按「还没搜过」处理
        return None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return None
    for info in accounts.values():
        value = (info or {}).get("key") if isinstance(info, dict) else None
        if isinstance(value, str) and len(value) == 32:
            return value.lower()
    return None


def _account_root() -> Optional[Path]:
    """定位当前账号目录（``xwechat_files/<wxid>_xxxx``）。"""
    try:
        from .tools.key_tool import auto_detect_db_dir
        db_dir = auto_detect_db_dir()
        return Path(db_dir).parent if db_dir else None
    except Exception:  # noqa: BLE001 - 探测失败返回 None，由调用方报错
        return None


# 明文表情的文件头 → 扩展名（wxgf 是微信私有容器，需转码后才能渲染）
_MAGIC_TO_FORMAT = (
    (b"\x89PNG", "png"),
    (b"GIF8", "gif"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),
    (b"wxgf", "wxgf"),
)


def _decrypt_emoticon(path: Path, key: bytes):
    """解一个表情文件，返回 ``(格式, 明文字节)``；不是表情或解不开返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 32 or len(data) % 16:
        return None
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        plain = unpad(AES.new(key, AES.MODE_CBC, iv=key).decrypt(data), 16)
    except Exception:  # noqa: BLE001 - 填充非法说明这不是该账号的表情文件
        return None
    for magic, fmt in _MAGIC_TO_FORMAT:
        if plain[: len(magic)] == magic:
            return fmt, plain
    return None


def _iter_cached_emoticons(root: Path):
    """遍历本机缓存的表情文件（``cache/<月>/Emoticon/<两位>/<md5>``）。

    business/emoticon 下的文件带 ``:wxgf`` 形式的 NTFS 备用数据流后缀，Python
    侧不处理；那里由 wxemo extract 覆盖，本函数专注 cache 里的大批量缓存。
    """
    cache = root / "cache"
    if not cache.is_dir():
        return
    for emoticon_dir in sorted(cache.glob("*/Emoticon")):
        for path in emoticon_dir.rglob("*"):
            if path.is_file() and len(path.name) == 32:
                yield path


def _run_wxemo(wxemo: str, args: List[str], timeout_seconds: float) -> Dict[str, Any]:
    """跑一次 wxemo 子命令，返回 ``{ok, exit_code, tail, stderr}``。"""
    try:
        proc = subprocess.run(
            [wxemo] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,  # MCP 走 stdio，绝不能把管道继承给子进程
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"wxemo 超时（>{timeout_seconds:.0f}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"wxemo 启动失败：{exc}"}
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-4:])
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "tail": tail, "stderr": (proc.stderr or "").strip()[-300:]}


def prepare_emoticons(timeout_seconds: float = 900.0) -> Dict[str, Any]:
    """生成 / 刷新本地明文表情库（``~/.wechat_mcp/emoticons/<md5>.<ext>``）。

    流程：取账号表情密钥（wxemo 搜索 seed 后派生并缓存的 key）→ 逐个解密本机
    缓存的表情文件 → 原生格式（png/gif/jpg/webp）直接入库 → wxgf 私有容器交给
    wxemo decode 转成 PNG/APNG 后入库。

    返回 ``{"ok": bool, ...}``；不抛异常、不向 stdout 输出（MCP stdio 安全）。
    """
    result: Dict[str, Any] = {"ok": False}
    wxemo = _find_wxemo()
    result["wxemo"] = wxemo

    key_hex = _read_emoticon_key()
    if not key_hex:
        if not wxemo:
            result["error"] = "未找到 wxemo，无法搜索表情密钥"
            result["hint"] = ("从 https://github.com/azazo1/wxemo/releases 下载后放到 "
                              "~/.wechat_mcp/tools/wxemo/ 下，或用环境变量 WXEMO_BIN 指定。")
            return result
        # 还没搜过 seed：现跑一次（GPU/SIMD 搜索，实测数秒）
        result["seed_search"] = _run_wxemo(wxemo, ["seed", "search"], timeout_seconds)
        key_hex = _read_emoticon_key()
    if not key_hex:
        result["error"] = "未取得表情密钥（seed 搜索未成功）"
        result["hint"] = "确认微信已登录、wxemo seed search 能正常跑完，再重试。"
        return result

    key = bytes.fromhex(key_hex)
    result["key_masked"] = f"{key_hex[:4]}...{key_hex[-4:]}"

    root = _account_root()
    if not root:
        result["error"] = "未找到微信账号目录"
        return result

    EMOTICON_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"native": 0, "wxgf": 0, "skipped": 0}
    for path in _iter_cached_emoticons(root):
        decrypted = _decrypt_emoticon(path, key)
        if not decrypted:
            stats["skipped"] += 1
            continue
        fmt, plain = decrypted
        md5 = path.name.lower()
        if fmt == "wxgf":
            # wxgf 只落缓存不转码：转码慢（约 0.3s/张、上千张要十分钟），
            # 交给 get_emoticon_image 在首次取用时按需转一个
            WXGF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (WXGF_CACHE_DIR / f"{md5}.wxgf").write_bytes(plain)
            stats["wxgf"] += 1
        else:
            (EMOTICON_DIR / f"{md5}.{fmt}").write_bytes(plain)
            stats["native"] += 1
    result["stats"] = stats

    result["ok"] = True
    result["library_dir"] = str(EMOTICON_DIR)
    result["total"] = sum(1 for p in EMOTICON_DIR.iterdir() if p.is_file())
    return result
