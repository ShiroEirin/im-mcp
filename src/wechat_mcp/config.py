import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
import os

# 基础路径
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 用户家目录全局持久化路径（与DuangDuang保持一致的设计，杜绝工作目录变动与 uv 缓存清理丢失）
USER_WECHAT_DIR = Path.home() / ".wechat_mcp"
DEFAULT_DECRYPTED_DIR = USER_WECHAT_DIR / "decrypted"
DB_PATH = USER_WECHAT_DIR / "app.db"

# 确保家目录存在
USER_WECHAT_DIR.mkdir(parents=True, exist_ok=True)

# 解密数据库目录（优先环境变量，其次全局家目录 ~/.wechat_mcp/decrypted，最后兼容当前项目相对路径）
def get_decrypted_dir() -> Path:
    env_dir = os.environ.get("WECHAT_DECRYPTED_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
    # 全局家目录为首选默认路径
    if DEFAULT_DECRYPTED_DIR.exists() and any(DEFAULT_DECRYPTED_DIR.iterdir()):
        return DEFAULT_DECRYPTED_DIR
    # 兼容当前开发目录 / 项目源码下的 decrypted
    plain_dir = BASE_DIR / "decrypted"
    if plain_dir.exists() and any(plain_dir.iterdir()):
        return plain_dir
    dot_dir = BASE_DIR / ".decrypted"
    if dot_dir.exists() and any(dot_dir.iterdir()):
        return dot_dir
    # 默认返回全局家目录
    return DEFAULT_DECRYPTED_DIR


def get_files_root() -> Path | None:
    """定位绿泡泡文件根目录（db_storage 的上一级），供媒体文件路径解析使用。

    优先读环境变量 WECHAT_FILES_ROOT；否则复用 wechat-init 同源的探测逻辑
    （读取 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 指向的数据根目录）。
    """
    env_root = os.environ.get("WECHAT_FILES_ROOT")
    if env_root:
        p = Path(env_root)
        if p.exists():
            return p

    appdata = os.environ.get("APPDATA", "")
    config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
    if not config_dir.is_dir():
        return None

    for ini_file in config_dir.glob("*.ini"):
        content = ""
        for enc in ("utf-8", "gbk"):
            try:
                content = ini_file.read_text(encoding=enc).strip()[:1024]
                break
            except (UnicodeDecodeError, OSError):
                continue
        if not content or not Path(content).is_dir():
            continue
        for match in Path(content).glob("xwechat_files/*/db_storage"):
            if match.is_dir():
                return match.parent
    return None
