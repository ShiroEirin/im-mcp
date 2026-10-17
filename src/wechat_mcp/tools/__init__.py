"""WeChat tools and initialization CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .key_tool import cmd_extract
from ..database import sync_data
from ..config import DEFAULT_DECRYPTED_DIR, USER_WECHAT_DIR


def main() -> None:
    """Friendly one-click init CLI for WeChat database extraction & decryption."""
    parser = argparse.ArgumentParser(
        prog="wechat-init",
        description="绿泡泡数据库密钥提取与解密初始化工具 (One-click WeChat DB Initializer)",
    )
    parser.add_argument(
        "--db-dir",
        help="绿泡泡 db_storage 目录（默认自动检测，无需手动指定）",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_DECRYPTED_DIR),
        help=f"解密后 SQLite 数据库保存目录 (默认: {DEFAULT_DECRYPTED_DIR})",
    )
    parser.add_argument(
        "--keys",
        default=str(USER_WECHAT_DIR / "all_keys.json"),
        help=f"密钥保存文件名 (默认: {USER_WECHAT_DIR / 'all_keys.json'})",
    )
    parser.add_argument(
        "--only-extract",
        action="store_true",
        help="仅提取密钥，不立即解密数据库",
    )
    parser.add_argument(
        "--etl-only", "--skip-decrypt",
        dest="etl_only",
        action="store_true",
        help="仅执行 ETL 数据清洗与构建索引（跳过密钥提取与解密，基于已有解密数据库构建）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="打印调试详细日志",
    )

    args = parser.parse_args()

    # 优先使用环境变量 WECHAT_DECRYPTED_DIR，否则使用 args.output
    output_dir = Path(os.environ.get("WECHAT_DECRYPTED_DIR", args.output)).resolve()

    if args.etl_only:
        print("=" * 60)
        print("🚀 绿泡泡数据清洗与全文检索索引重建 (wechat-init --etl-only)")
        print("=" * 60)
        print(f"[*] 来源解密目录: {output_dir}")
        print("-" * 60)
        if not output_dir.exists():
            print(f"[ERROR] 解密目录不存在: {output_dir}")
            print("[*] 提示: 请先运行完整 wechat-init 进行提取与解密！")
            sys.exit(1)
        try:
            os.environ["WECHAT_DECRYPTED_DIR"] = str(output_dir)
            res = sync_data(force=True)
            if res.get("status") == "success":
                print(f"🎉 绿泡泡数据与全文检索索引重建就绪！(联系人: {res.get('contacts', 0)}, 会话: {res.get('sessions', 0)}, 消息: {res.get('messages', 0)})")
                print("=" * 60)
                print("[*] 绿泡泡 MCP 服务已完全就绪，您可以直接启动 wechat-mcp 了。")
                print("=" * 60)
            else:
                print(f"[ERROR] ETL 同步失败: {res.get('message')}")
                sys.exit(1)
        except Exception as e:
            print(f"\n[ERROR] 数据同步发生异常: {e}")
            sys.exit(1)
        return

    # 构造兼容底层 key_tool 的参数
    ns = argparse.Namespace(
        command="extract",
        db_dir=args.db_dir,
        output=args.keys,
        decrypted_dir=str(output_dir),
        decrypt=not args.only_extract,
        verbose=args.verbose,
    )

    print("=" * 60)
    print("🚀 绿泡泡数据库初始化向导 (wechat-init)")
    print("=" * 60)
    print(f"[*] 提示: 请确保 Windows 绿泡泡客户端已正常启动并完成登录。")
    print(f"[*] 解密输出目录: {output_dir}")
    print(f"[*] 密钥文件: {args.keys}")
    print("-" * 60)

    try:
        cmd_extract(ns)
        # 解密成功后，自动执行一次 ETL 同步，确保 app.db 建立并开箱即用
        if not args.only_extract:
            print("-" * 60)
            print("[*] 正在自动执行 ETL 数据清洗并构建全文检索索引...")
            # 显式将解密输出目录传入环境变量，确保无论何种工作目录下 ETL 均 100% 能读取
            os.environ["WECHAT_DECRYPTED_DIR"] = str(output_dir)
            res = sync_data(force=True)
            if res.get("status") == "success":
                print(f"🎉 绿泡泡数据与全文检索索引初始化就绪！(联系人: {res.get('contacts', 0)}, 会话: {res.get('sessions', 0)}, 消息: {res.get('messages', 0)})")
            else:
                print(f"[!] ETL 自动同步提示: {res.get('message')}")
            print("=" * 60)
            print("[*] 绿泡泡 MCP 服务已完全就绪，您可以直接启动 wechat-mcp 了。")
            print("=" * 60)
    except KeyboardInterrupt:
        print("\n[!] 操作已取消。")
        sys.exit(130)
    except Exception as e:
        err_msg = str(e)
        print(f"\n[ERROR] 提取或解密失败: {err_msg}")
        # 分场景精准归因，避免误导用户排查
        if "Weixin.exe" in err_msg or "进程" in err_msg:
            print("[*] 提示: 未能定位或连接到绿泡泡进程，请确认 Windows 绿泡泡客户端已登录运行。")
        elif "Access Denied" in err_msg or "权限" in err_msg or "privilege" in err_msg.lower():
            print("[*] 提示: 读取绿泡泡进程内存需要提权，请尝试以管理员身份运行当前终端！")
        elif "db_storage" in err_msg or "目录" in err_msg:
            print("[*] 提示: 未检测到绿泡泡数据库目录，请确认绿泡泡数据存储位置或通过 --db-dir 手动指定。")
        else:
            print("[*] 提示: 执行过程中遇到程序内部异常，请检查上方详细错误信息或向作者提交 issue。")
        sys.exit(1)


if __name__ == "__main__":
    main()
