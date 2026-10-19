import os
import sys
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest import mock

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from im_clean import (
    RELATED_PROCESS_KEYWORDS,
    find_blocking_processes,
    format_size,
    get_cleanup_targets,
)


class TestImClean(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(500), "500.0 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 1024), "1.0 GB")

    def test_cleanup_targets_structure(self):
        targets = get_cleanup_targets()
        self.assertIn("wechat", targets)
        self.assertIn("dingtalk", targets)
        self.assertIsInstance(targets["wechat"], list)
        self.assertIsInstance(targets["dingtalk"], list)


class TestImCleanProcessScan(unittest.TestCase):
    """进程扫描必须覆盖 start_sync 拉起的独立同步进程。

    该进程刻意脱离 MCP 进程树，宿主回收 MCP 后仍会继续跑并锁住 decrypted/ 与
    app.db；若关键词表漏了它，`im-clean -y --force` 会检测不到占用进程，删除阶段
    失败并误导用户去「退出客户端」（而退出客户端根本没用）。
    """

    def test_keywords_cover_detached_sync_worker(self):
        self.assertIn("wechat_mcp.sync_worker", RELATED_PROCESS_KEYWORDS)

    @unittest.skipUnless(sys.platform == "win32", "进程扫描仅在 Windows 生效")
    def test_find_blocking_processes_matches_sync_worker(self):
        fake_stdout = (
            "12345 C:\\Python\\python.exe -m wechat_mcp.sync_worker --force\n"
            "67890 C:\\Python\\python.exe -m wechat_mcp.server\n"
            "11111 C:\\Windows\\explorer.exe\n"
        )
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout=fake_stdout)
            blocking = find_blocking_processes()

        pids = [pid for pid, _ in blocking]
        self.assertIn(12345, pids)  # 独立同步进程
        self.assertIn(67890, pids)  # MCP 服务本体
        self.assertNotIn(11111, pids)  # 无关进程不得误伤


if __name__ == "__main__":
    unittest.main()
