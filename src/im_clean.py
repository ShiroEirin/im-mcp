"""Cleanup tools for WeChat & DingTalk exported and cached databases."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 获取项目根目录 (src/ 的上一级)
BASE_DIR = Path(__file__).resolve().parent.parent

# 相关的守护/工作进程关键词
RELATED_PROCESS_KEYWORDS = [
    "wechat-mcp",
    "dingtalk-mcp",
    "wechat_mcp.server",
    "dingtalk_mcp.server",
]


def find_blocking_processes() -> list[tuple[int, str]]:
    """扫描是否有活跃的 wechat-mcp / dingtalk-mcp 进程持有数据库锁。"""
    blocking = []
    if sys.platform == "win32":
        try:
            # 使用 wmic 或 powershell 查询命令行
            cmd = "Get-CimInstance Win32_Process | Select-Object ProcessId, CommandLine"
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in proc.stdout.splitlines():
                for kw in RELATED_PROCESS_KEYWORDS:
                    if kw in line:
                        parts = line.strip().split(None, 1)
                        if parts and parts[0].isdigit():
                            pid = int(parts[0])
                            cmdline = parts[1] if len(parts) > 1 else kw
                            # 排除当前脚本自身的 PID
                            if pid != os.getpid():
                                blocking.append((pid, cmdline))
                        break
        except Exception:
            pass
    return blocking


def terminate_processes(pids: list[int]) -> None:
    """终止阻碍文件删除的活跃 MCP 进程。"""
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=3)
            else:
                os.kill(pid, 9)
        except Exception:
            pass
    time.sleep(0.5)


def get_cleanup_targets() -> dict[str, list[Path]]:
    """Collect all generated/exported files and directories for cleanup."""
    targets: dict[str, list[Path]] = {
        "wechat": [],
        "dingtalk": [],
        "shared": [],
    }

    # 1. 绿泡泡相关产物
    # 解密目录（项目下 decrypted/、.decrypted/ 或 环境变量 WECHAT_DECRYPTED_DIR）
    env_wx_dec = os.environ.get("WECHAT_DECRYPTED_DIR")
    if env_wx_dec:
        targets["wechat"].append(Path(env_wx_dec))

    targets["wechat"].extend([
        BASE_DIR / "decrypted",
        BASE_DIR / ".decrypted",
        BASE_DIR / "all_keys.json",
        BASE_DIR / ".cache",
        Path.home() / ".wechat_mcp",
        # key_tool 手动/实验性写入的 32 字节 passphrase 明文文件；此前不在
        # 清理范围内，im-clean 跑完密钥仍留在磁盘上
        Path.home() / ".wcdb-key-tool",
    ])

    # 2. DuangDuang相关产物
    env_dt_dec = os.environ.get("DINGTALK_DECRYPTED_DIR")
    if env_dt_dec:
        targets["dingtalk"].append(Path(env_dt_dec))

    targets["dingtalk"].extend([
        Path.home() / ".dingtalk_mcp",
        BASE_DIR / ".dingtalk_decrypted",
    ])

    # 去重并过滤掉不存在的
    cleaned_targets: dict[str, list[Path]] = {}
    for cat, paths in targets.items():
        seen = set()
        cleaned = []
        for p in paths:
            resolved = p.resolve()
            if resolved not in seen and resolved.exists():
                seen.add(resolved)
                cleaned.append(resolved)
        cleaned_targets[cat] = cleaned

    return cleaned_targets


def get_size(path: Path) -> int:
    """Calculate directory or file size in bytes."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def format_size(bytes_size: int) -> str:
    """Format bytes to human readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="im-clean",
        description="一键清理绿泡泡与DuangDuang导出的解密数据库、中间缓存与密钥文件 (IM Cache & Export Cleaner)",
    )
    parser.add_argument(
        "--scope",
        choices=["all", "wechat", "dingtalk"],
        default="all",
        help="清理范围: all (全部), wechat (仅绿泡泡), dingtalk (仅DuangDuang) (默认: all)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="跳过二次确认直接删除",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制模式：若遇正在运行的 MCP 服务进程自动尝试结束，并在常规删除受阻时强制重试",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅列出将要被清理的目标和文件大小，不实际删除",
    )

    args = parser.parse_args()

    targets_by_cat = get_cleanup_targets()
    to_delete: list[tuple[Path, int]] = []

    categories = ["wechat", "dingtalk"] if args.scope == "all" else [args.scope]
    for cat in categories:
        for p in targets_by_cat.get(cat, []):
            size = get_size(p)
            to_delete.append((p, size))

    print("=" * 65)
    print("🧹 绿泡泡 & DuangDuang数据与缓存清理工具 (im-clean)")
    print("=" * 65)

    if not to_delete:
        print("[*] 没有发现任何需要清理的导出文件或缓存。当前环境非常干净！")
        return

    total_bytes = sum(s for _, s in to_delete)
    print(f"[*] 扫描到以下导出数据 / 缓存文件（共计 {len(to_delete)} 处，占用 {format_size(total_bytes)}）:\n")
    for p, s in to_delete:
        type_str = "目录" if p.is_dir() else "文件"
        print(f"  • [{type_str}] {p} ({format_size(s)})")

    print("-" * 65)

    # 检查是否存在占用数据库句柄的活动 MCP 进程
    blocking = find_blocking_processes()
    if blocking:
        print("⚠️  检测到以下正在运行的 MCP 服务进程可能占有数据库文件:")
        for pid, cmd in blocking:
            print(f"  • PID: {pid} -> {cmd[:70]}")
        
        if args.force:
            print("[*] [--force] 正在自动结束上述占用进程...")
            terminate_processes([pid for pid, _ in blocking])
        elif not args.dry_run:
            if not args.yes:
                try:
                    ans = input("❓ 是否自动结束这些进程以解除文件占用？[y/N]: ").strip().lower()
                    if ans in ("y", "yes"):
                        terminate_processes([pid for pid, _ in blocking])
                    else:
                        print("[!] 提示: 若不结束进程，部分处于打开状态的 SQLite/WAL 文件可能会删除失败。")
                except (KeyboardInterrupt, EOFError):
                    print("\n[!] 操作取消。")
                    sys.exit(130)
            else:
                print("[*] 正在尝试结束上述占用进程以解除文件占用...")
                terminate_processes([pid for pid, _ in blocking])

    if args.dry_run:
        print("[*] [Dry Run] 模拟运行结束，未删除任何内容。")
        return

    if not args.yes:
        try:
            confirm = input("⚠️  确认彻底删除上述所有导出的数据和缓存？[y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("[!] 用户取消，未进行任何清理。")
                sys.exit(0)
        except (KeyboardInterrupt, EOFError):
            print("\n[!] 操作取消。")
            sys.exit(130)

    print("\n[*] 正在执行清理...")
    failed_items: list[tuple[Path, str]] = []
    freed_bytes = 0

    for p, original_size in to_delete:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            else:
                p.unlink(missing_ok=True)
            
            # 二次确认物理磁盘上是否已被完全移除
            if p.exists():
                raise OSError("文件或目录在执行删除后依然物理残留（可能被特定进程加锁）")
            
            print(f"  [+] 已彻底删除: {p}")
            freed_bytes += original_size
        except Exception as e:
            # 如果常规删除失败，且开启了 force，尝试 Windows 原生 rd /s /q 降级
            deleted_by_force = False
            if args.force and sys.platform == "win32":
                try:
                    if p.is_dir():
                        subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)], capture_output=True, timeout=5)
                    else:
                        subprocess.run(["cmd", "/c", "del", "/f", "/q", str(p)], capture_output=True, timeout=5)
                    if not p.exists():
                        deleted_by_force = True
                        print(f"  [+] [FORCE] 已强制删除: {p}")
                        freed_bytes += original_size
                except Exception:
                    pass

            if not deleted_by_force:
                print(f"  [-] 删除失败 {p}: {e}")
                failed_items.append((p, str(e)))

    print("=" * 65)
    if failed_items:
        print("❌ 清理未能完全成功！存在部分文件/目录残留：")
        for p, err in failed_items:
            print(f"  ❌ {p}")
        print("\n[*] 失败排查建议：")
        print("  1. 当前文件可能正被 AI 客户端（如 WorkBuddy, Claude Desktop, Pi）或后台 MCP 服务进程占用；")
        print("  2. 请先完全退出 AI 客户端应用，或追加 --force 参数重新运行：im-clean -y --force")
        print("=" * 65)
        sys.exit(1)
    else:
        print(f"🎉 全部数据清理成功！实际释放约 {format_size(freed_bytes)} 空间。")
        print("=" * 65)


if __name__ == "__main__":
    main()
