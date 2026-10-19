"""定时同步调度器的单元测试。

全部用例都 mock 掉真实解密与 ETL：测试不得触发对微信数据目录的读写。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_mcp import auto_sync


class TestIntervalClamp(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(auto_sync._clamp_interval(1), auto_sync.MIN_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(99999), auto_sync.MAX_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(45), 45)
        self.assertEqual(auto_sync._clamp_interval("bad"), auto_sync.DEFAULT_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(None), auto_sync.DEFAULT_INTERVAL_MINUTES)


class TestRefreshDecrypted(unittest.TestCase):
    def test_missing_keys_file(self):
        with tempfile.TemporaryDirectory() as d:
            res = auto_sync.refresh_decrypted(keys_file=Path(d) / "nope.json")
        self.assertFalse(res["ok"])
        self.assertIn("密钥文件不存在", res["error"])

    def test_bad_db_dir(self):
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            res = auto_sync.refresh_decrypted(db_dir=str(Path(d) / "nope"), keys_file=keys)
        self.assertFalse(res["ok"])

    def test_db_dir_from_keys(self):
        """密钥文件里的 _db_dir 只在目录真实存在时才算可用。"""
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys.write_text(json.dumps({"_db_dir": str(dbdir)}), encoding="utf-8")
            self.assertEqual(auto_sync._db_dir_from_keys(keys), str(dbdir))

            keys.write_text(json.dumps({"_db_dir": str(Path(d) / "nope")}), encoding="utf-8")
            self.assertIsNone(auto_sync._db_dir_from_keys(keys))
            keys.write_text("{}", encoding="utf-8")
            self.assertIsNone(auto_sync._db_dir_from_keys(keys))
            self.assertIsNone(auto_sync._db_dir_from_keys(Path(d) / "missing.json"))

    def test_falls_back_to_db_dir_recorded_in_keys(self):
        """自动探测失败时必须回退到密钥文件记录的 _db_dir。

        真实故障：wechat-mcp 由 Snow App 启动，其环境与终端不同，
        auto_detect_db_dir() 返回 None → 同步永远以「未找到 db_storage」失败，
        而 all_keys.json 里一直记着当初成功解密用过的目录。
        """
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc()

        with tempfile.TemporaryDirectory() as d:
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys = Path(d) / "all_keys.json"
            keys.write_text(json.dumps({"_db_dir": str(dbdir)}), encoding="utf-8")
            with patch("wechat_mcp.tools.key_tool.auto_detect_db_dir", return_value=None), \
                 patch.object(auto_sync.subprocess, "run", side_effect=fake_run), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertTrue(res["ok"])
        self.assertEqual(res["db_dir"], str(dbdir))
        self.assertIn(str(dbdir), calls[0])

    def test_env_var_overrides_detection(self):
        """WECHAT_DB_DIR 显式指定时优先于自动探测（排障出路）。"""
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        with tempfile.TemporaryDirectory() as d:
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            with patch.dict(auto_sync.os.environ, {"WECHAT_DB_DIR": str(dbdir)}), \
                 patch("wechat_mcp.tools.key_tool.auto_detect_db_dir",
                       side_effect=AssertionError("有环境变量时不应再探测")), \
                 patch.object(auto_sync.subprocess, "run",
                              side_effect=lambda cmd, **kw: calls.append(cmd) or FakeProc()), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertTrue(res["ok"])
        self.assertEqual(res["db_dir"], str(dbdir))
        self.assertIn(str(dbdir), calls[0])

    def test_all_fallbacks_fail_gives_actionable_error(self):
        """探测与密钥记录都不可用时，错误信息必须给出可操作出路。"""
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            with patch.dict(auto_sync.os.environ, {"WECHAT_DB_DIR": ""}), \
                 patch("wechat_mcp.tools.key_tool.auto_detect_db_dir", return_value=None):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertFalse(res["ok"])
        self.assertIn("WECHAT_DB_DIR", res["error"])

    def test_uses_subprocess_and_stays_quiet(self):
        """刷新必须走子进程并捕获输出 —— 直接进程内调用会把进度打到 stdout，
        MCP 走 stdio 传 JSON-RPC，任何 stdout 输出都会破坏协议。"""
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "结果: 20 成功, 0 失败\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc()

        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            with patch.object(auto_sync.subprocess, "run", side_effect=fake_run), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(db_dir=str(dbdir), keys_file=keys)

        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertIn("key_tool", " ".join(cmd))
        self.assertIn("decrypt", cmd)
        self.assertTrue(kwargs.get("capture_output"))


class TestAutoSyncState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "auto_sync.json"
        self._patch = patch.object(auto_sync, "STATE_FILE", self.state)
        self._patch.start()
        auto_sync._stop_worker()

    def tearDown(self):
        auto_sync._stop_worker()
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_status_disabled(self):
        st = auto_sync.get_auto_sync_status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["running"])
        self.assertEqual(st["interval_minutes"], auto_sync.DEFAULT_INTERVAL_MINUTES)

    def test_enable_then_disable(self):
        st = auto_sync.set_auto_sync(True, 15)
        self.assertTrue(st["enabled"])
        self.assertTrue(st["running"])
        self.assertEqual(st["interval_minutes"], 15)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval_minutes"], 15)

        st2 = auto_sync.set_auto_sync(False)
        self.assertFalse(st2["enabled"])
        self.assertFalse(st2["running"])

    def test_restore_on_startup(self):
        self.state.write_text(json.dumps({"enabled": True, "interval_minutes": 7}), encoding="utf-8")
        auto_sync.restore_on_startup()
        st = auto_sync.get_auto_sync_status()
        self.assertTrue(st["running"])
        self.assertEqual(st["interval_minutes"], 7)

    def test_restore_skips_when_disabled(self):
        self.state.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        auto_sync.restore_on_startup()
        self.assertFalse(auto_sync.get_auto_sync_status()["running"])

    def test_sync_now_skips_etl_when_decrypt_fails(self):
        """解密失败就不该重建索引：在旧快照上重建只会「看起来成功」但内容没变"""
        with patch.object(auto_sync, "refresh_decrypted", return_value={"ok": False, "error": "x"}), \
             patch.object(auto_sync, "sync_data") as mock_etl:
            res = auto_sync.sync_now()
        self.assertFalse(res["ok"])
        mock_etl.assert_not_called()
        self.assertEqual(res["etl_step"]["status"], "skipped")

    def test_sync_now_runs_etl_when_decrypt_ok(self):
        with patch.object(auto_sync, "refresh_decrypted", return_value={"ok": True}), \
             patch.object(auto_sync, "sync_data", return_value={"status": "success"}) as mock_etl:
            res = auto_sync.sync_now(force=True)
        self.assertTrue(res["ok"])
        mock_etl.assert_called_once_with(force=True)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_count"], 1)
        self.assertIn("last_run", saved)


class _FakeAliveThread:
    """只用于冒充「仍在运行的同步线程」——不需要真的起线程。"""

    def is_alive(self):
        return True


class TestManualSyncWorker(unittest.TestCase):
    """手动后台同步（start_sync / _sync_worker）的两条关键不变量。

    真实故障（2026-10-19 实测）：面板点「同步微信数据」永远停在「同步中」。
    根因有二 —— (1) 后台线程抛异常时只 logger.warning 不写 state，调用方按
    run_count 增长判定完成的条件永远不成立；(2) start_sync 只检查 is_alive()，
    一次卡死之后每次点击都返回 already_running，永久卡死。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "auto_sync.json"
        self._patch = patch.object(auto_sync, "STATE_FILE", self.state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        with auto_sync._lock:
            auto_sync._sync_thread = None
            auto_sync._sync_started_at = None

    def test_worker_records_failure_when_sync_raises(self):
        """异常必须落盘：否则调用方永远等不到「跑完」的信号。"""
        with patch.object(auto_sync, "sync_now", side_effect=RuntimeError("解密快照被写坏")):
            auto_sync._sync_worker(False, True)

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_count"], 1)
        self.assertFalse(saved["last_run"]["ok"])
        self.assertIn("解密快照被写坏", saved["last_run"]["result"]["error"])
        # 线程引用与启动时间都要清干净，否则下一次 start_sync 会误判 already_running
        self.assertIsNone(auto_sync._sync_thread)
        self.assertIsNone(auto_sync._sync_started_at)

    def test_start_sync_reports_already_running_when_fresh(self):
        """未超时阈值的同步在跑：必须如实报告 already_running 与已跑时长。"""
        with auto_sync._lock:
            auto_sync._sync_thread = _FakeAliveThread()
            auto_sync._sync_started_at = time.time() - 5

        res = auto_sync.start_sync()
        self.assertFalse(res["started"])
        self.assertTrue(res["already_running"])
        self.assertGreaterEqual(res["elapsed_seconds"], 5)

        st = auto_sync.get_auto_sync_status()
        self.assertTrue(st["syncing"])
        self.assertIsNotNone(st["sync_started_at"])
        self.assertGreaterEqual(st["sync_elapsed_seconds"], 5)

    def test_start_sync_replaces_stale_thread(self):
        """超过 SYNC_STALE_SECONDS 仍活着 → 视为僵死，必须允许重新发起。"""
        with auto_sync._lock:
            auto_sync._sync_thread = _FakeAliveThread()
            auto_sync._sync_started_at = time.time() - (auto_sync.SYNC_STALE_SECONDS + 1)

        with patch.object(auto_sync, "_sync_worker"):
            res = auto_sync.start_sync()

        self.assertTrue(res["started"])
        self.assertNotIn("already_running", res)


if __name__ == "__main__":
    unittest.main()
