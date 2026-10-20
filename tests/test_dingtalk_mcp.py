import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Crypto.Cipher import AES
from dingtalk_mcp.db import parse_message_content
from dingtalk_mcp.decrypt import (
    _AES_PAGE_SIZE,
    _merge_wal_frames_into_db,
    _wal_cksum,
    decrypt_db_file,
    decrypt_dingtalk_with_report,
    generate_key,
)


class TestDingTalkDecrypt(unittest.TestCase):
    def test_generate_key_v2(self):
        # 钉钉 V2 AES 密钥必须是 16 字节
        dummy_uid = "12345678"
        key = generate_key(dummy_uid, version=2)
        self.assertEqual(len(key), 16)
        self.assertIsInstance(key, bytes)

    def test_generate_key_v3(self):
        # 钉钉 V3 结合 salt 生成 16 字节密钥
        dummy_uid = "12345678"
        dummy_salt = "abcd1234efgh5678"
        key = generate_key(dummy_uid, version=3, salt_hex=dummy_salt)
        self.assertEqual(len(key), 16)
        self.assertIsInstance(key, bytes)

    def test_generate_key_reproducibility(self):
        uid = "987654321"
        key1 = generate_key(uid, version=2)
        key2 = generate_key(uid, version=2)
        self.assertEqual(key1, key2)


class TestDingTalkWalChecksum(unittest.TestCase):
    def test_wal_cksum_reproducibility_and_endianness(self):
        # 验证 WAL checksum 算法小端和大端计算
        data = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        s0_le, s1_le = _wal_cksum(data, 0, 0, is_big_endian=False)
        self.assertIsInstance(s0_le, int)
        self.assertIsInstance(s1_le, int)
        self.assertTrue(0 <= s0_le <= 0xFFFFFFFF)
        self.assertTrue(0 <= s1_le <= 0xFFFFFFFF)

        s0_be, s1_be = _wal_cksum(data, 0, 0, is_big_endian=True)
        # 大小端解析出的值不同，累加结果也不同
        self.assertNotEqual((s0_le, s1_le), (s0_be, s1_be))

    def test_wal_cksum_chaining(self):
        # 验证累加调用与分步调用的等价性
        data1 = b"12345678"
        data2 = b"abcdefgh"
        s0_step1, s1_step1 = _wal_cksum(data1, 0, 0)
        s0_step2, s1_step2 = _wal_cksum(data2, s0_step1, s1_step1)

        s0_all, s1_all = _wal_cksum(data1 + data2, 0, 0)
        self.assertEqual((s0_step2, s1_step2), (s0_all, s1_all))


class TestDingTalkWalDecryptAndCheckpoint(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_dt_wal_")
        self.key = b"0123456789abcdef"
        self.cipher = AES.new(self.key, AES.MODE_ECB)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_encrypted_dingtalk_db(self):
        """Helper to create a realistic mock encrypted SQLite db and WAL."""
        db_path = os.path.join(self.test_dir, "raw.db")
        wal_path = db_path + "-wal"

        # 1. 用真实 SQLite 生成原始数据库与表
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE test_msgs (id INTEGER PRIMARY KEY, content TEXT);")
        conn.execute("INSERT INTO test_msgs (content) VALUES ('msg_in_base_db');")
        conn.commit()
        # 将基础数据落盘入主库
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

        # 2. 插入新消息，保留在 WAL 中不自动 checkpoint
        conn.execute("PRAGMA wal_autocheckpoint=0;")
        conn.execute("INSERT INTO test_msgs (content) VALUES ('wal_msg_1');")
        conn.execute("INSERT INTO test_msgs (content) VALUES ('wal_msg_2');")
        conn.commit()

        # 在连接保持打开时读取原始未加密的 db 和 wal 数据（防止 close 时自动 checkpoint 删空 WAL）
        with open(db_path, "rb") as f:
            raw_db_data = f.read()
        with open(wal_path, "rb") as f:
            raw_wal_data = f.read()
        conn.close()

        # 3. 模拟钉钉：加密 DB 文件（每 4096 字节 ECB 加密）
        enc_db_data = bytearray()
        for i in range(0, len(raw_db_data), _AES_PAGE_SIZE):
            page = raw_db_data[i : i + _AES_PAGE_SIZE]
            if len(page) == _AES_PAGE_SIZE:
                for j in range(0, _AES_PAGE_SIZE, 16):
                    enc_db_data.extend(self.cipher.encrypt(page[j : j + 16]))
            else:
                enc_db_data.extend(page)

        # 4. 模拟钉钉：加密 WAL 帧数据并计算密文校验和
        hdr = bytearray(raw_wal_data[:32])
        magic_be = struct.unpack(">I", hdr[:4])[0]
        is_be = (magic_be & 1) == 1

        enc_wal_data = bytearray()
        s0, s1 = _wal_cksum(hdr[:24], 0, 0, is_big_endian=is_be)
        hdr[24:32] = struct.pack(">II", s0, s1)
        enc_wal_data.extend(hdr)

        frame_size = 24 + _AES_PAGE_SIZE
        for offset in range(32, len(raw_wal_data), frame_size):
            frame = raw_wal_data[offset : offset + frame_size]
            if len(frame) == frame_size:
                fhdr = bytearray(frame[:24])
                page = frame[24:]
                enc_page = bytearray()
                for j in range(0, _AES_PAGE_SIZE, 16):
                    enc_page.extend(self.cipher.encrypt(page[j : j + 16]))

                # 密文校验和写入帧头（模拟钉钉真实行为）
                a0, a1 = _wal_cksum(fhdr[:8], s0, s1, is_big_endian=is_be)
                b0, b1 = _wal_cksum(enc_page, a0, a1, is_big_endian=is_be)
                fhdr[16:24] = struct.pack(">II", b0, b1)
                s0, s1 = b0, b1

                enc_wal_data.extend(fhdr)
                enc_wal_data.extend(enc_page)

        # 写入模拟的 DBFiles 目录
        dbfiles_dir = os.path.join(self.test_dir, "DBFiles")
        os.makedirs(dbfiles_dir, exist_ok=True)
        enc_db_path = os.path.join(dbfiles_dir, "dingtalk.db")
        enc_wal_path = os.path.join(dbfiles_dir, "dingtalk.db-wal")
        with open(enc_db_path, "wb") as f:
            f.write(enc_db_data)
        with open(enc_wal_path, "wb") as f:
            f.write(enc_wal_data)

        return self.test_dir, enc_db_path, enc_wal_path

    def test_wal_decryption_and_native_checkpoint(self):
        """测试解密 WAL 后校验和被成功重算，并且 SQLite 原生 checkpoint 能够 100% 成功合并。"""
        data_dir, enc_db, enc_wal = self._create_mock_encrypted_dingtalk_db()
        out_dir = os.path.join(self.test_dir, "decrypted")

        import dingtalk_mcp.decrypt as dt_dec

        orig_gen_key = dt_dec.generate_key
        try:
            dt_dec.generate_key = lambda uid, ver=2, salt=None: self.key
            out_db, report = decrypt_dingtalk_with_report(
                data_dir=data_dir,
                uid="mock_user",
                version=2,
                salt=None,
                output_dir=out_dir,
            )
        finally:
            dt_dec.generate_key = orig_gen_key

        self.assertTrue(os.path.isfile(out_db))
        self.assertEqual(report["checkpoint_method"], "native")
        self.assertGreater(report["wal_frames_applied"], 0)
        self.assertEqual(report["wal_frames_applied"], report["wal_frames_total"])

        # 验证 WAL 中的新增数据已完整合并入主库
        conn = sqlite3.connect(out_db)
        rows = conn.execute("SELECT content FROM test_msgs ORDER BY id ASC;").fetchall()
        conn.close()

        contents = [r[0] for r in rows]
        self.assertIn("msg_in_base_db", contents)
        self.assertIn("wal_msg_1", contents)
        self.assertIn("wal_msg_2", contents)
        self.assertEqual(len(contents), 3)

    def test_wal_fallback_merge_when_corrupted(self):
        """测试当原生 checkpoint 无法识别（校验和被刻意破坏）时，fallback 兜底机制生效。"""
        data_dir, enc_db, enc_wal = self._create_mock_encrypted_dingtalk_db()
        out_dir = os.path.join(self.test_dir, "decrypted_fallback")

        # 破坏 WAL 头部让原生 SQLite 拒绝识别
        with open(enc_wal, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPTED_MAGIC_HEADER_BYTES_XX")

        import dingtalk_mcp.decrypt as dt_dec

        orig_gen_key = dt_dec.generate_key
        try:
            dt_dec.generate_key = lambda uid, ver=2, salt=None: self.key
            out_db, report = decrypt_dingtalk_with_report(
                data_dir=data_dir,
                uid="mock_user",
                version=2,
                salt=None,
                output_dir=out_dir,
            )
        finally:
            dt_dec.generate_key = orig_gen_key

        # 验证回退到 fallback 并且数据仍然成功合并
        self.assertEqual(report["checkpoint_method"], "fallback")
        conn = sqlite3.connect(out_db)
        rows = conn.execute("SELECT content FROM test_msgs ORDER BY id ASC;").fetchall()
        conn.close()

        contents = [r[0] for r in rows]
        self.assertIn("msg_in_base_db", contents)
        self.assertIn("wal_msg_1", contents)
        self.assertIn("wal_msg_2", contents)


class TestDingTalkMessageContentParser(unittest.TestCase):
    def test_parse_content_type_501_file(self):
        # 测试 501 文件消息解析 (含 extension.f_name)
        raw_json = json.dumps({
            "attachments": [{
                "extension": json.dumps({"f_name": "quarterly_report.pdf", "f_size": 204800}),
            }]
        })
        res = parse_message_content(raw_json, content_type=501)
        self.assertIn("quarterly_report.pdf", res["text"])

    def test_parse_content_type_3100_todo_calendar(self):
        # 测试 3100 待办/日程卡片解析 (含 extension.desc)
        raw_json = json.dumps({
            "extension": {"desc": "每日项目同步会 10:00"}
        })
        res = parse_message_content(raw_json, content_type=3100)
        self.assertIn("每日项目同步会 10:00", res["text"])

    def test_parse_content_type_1200_rich_markdown(self):
        # 测试 1200 富文本卡片解析
        raw_json = json.dumps({
            "attachments": [{
                "extension": json.dumps({"markdown": "## 发布通知\n已完成发布"})
            }]
        })
        res = parse_message_content(raw_json, content_type=1200)
        self.assertIn("发布通知", res["text"])


if __name__ == "__main__":
    unittest.main()
