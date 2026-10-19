"""聊天记录（recorditem）解析的单元测试。

解析部分是纯函数，不依赖本地真实数据 / 数据库，任何环境都能跑。
需要数据库的用例在库不可用时自动跳过。
"""

import unittest

from wechat_mcp.records import find_raw_content, parse_chat_record

SAMPLE = (
    "<msg><appmsg><title>群聊的聊天记录</title><des>A:&#x20;hi</des><type>19</type>"
    "<recorditem><![CDATA[<recordinfo><title>群聊的聊天记录</title>"
    '<desc>A:&#x20;hi&#x0A;B:&#x20;yo</desc><datalist count="2">'
    '<dataitem datatype="1" dataid="x1"><datadesc>hi&#x20;there</datadesc>'
    "<sourcename>A</sourcename><sourcetime>2026-06-07 20:34:04</sourcetime>"
    "</dataitem>"
    '<dataitem datatype="2" dataid="x2"><datadesc>[图片]</datadesc>'
    "<sourcename>B</sourcename><fullmd5>abc123</fullmd5><datafmt>jpg</datafmt>"
    "<datasize>1234</datasize></dataitem>"
    "</datalist></recordinfo>]]></recorditem></appmsg></msg>"
)

# 少数客户端版本不带 CDATA，正文直接是转义后的 XML。
NO_CDATA = (
    "<msg><appmsg><type>19</type><recorditem>"
    '<recordinfo><title>T</title><datalist count="1">'
    '<dataitem datatype="4" dataid="v1"><datadesc>[视频]</datadesc>'
    "<sourcename>C</sourcename><duration>43</duration></dataitem>"
    "</datalist></recordinfo></recorditem></appmsg></msg>"
)


class TestParseChatRecord(unittest.TestCase):
    def test_header_fields(self):
        r = parse_chat_record(SAMPLE)
        self.assertEqual(r["title"], "群聊的聊天记录")
        # &#x20; / &#x0A; 要还原成空格与换行
        self.assertEqual(r["desc"], "A: hi\nB: yo")
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["declared_count"], 2)

    def test_items(self):
        items = parse_chat_record(SAMPLE)["items"]
        self.assertEqual(len(items), 2)

        self.assertEqual(items[0]["type"], "text")
        self.assertEqual(items[0]["datatype"], 1)
        self.assertEqual(items[0]["desc"], "hi there")
        self.assertEqual(items[0]["sender"], "A")
        self.assertEqual(items[0]["time"], "2026-06-07 20:34:04")
        self.assertIsNone(items[0]["md5"])

        self.assertEqual(items[1]["type"], "image")
        self.assertEqual(items[1]["desc"], "[图片]")
        self.assertEqual(items[1]["md5"], "abc123")
        self.assertEqual(items[1]["format"], "jpg")
        self.assertEqual(items[1]["size"], "1234")

    def test_without_cdata(self):
        r = parse_chat_record(NO_CDATA)
        self.assertEqual(r["title"], "T")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["type"], "video")
        self.assertEqual(r["items"][0]["duration"], "43")

    def test_unknown_datatype_falls_back(self):
        xml = (
            "<msg><recorditem><![CDATA[<recordinfo>"
            '<datalist count="1"><dataitem datatype="99" dataid="z">'
            "<datadesc>x</datadesc></dataitem></datalist>"
            "</recordinfo>]]></recorditem></msg>"
        )
        self.assertEqual(parse_chat_record(xml)["items"][0]["type"], "type_99")

    def test_not_a_chat_record(self):
        self.assertIn("error", parse_chat_record("<msg><appmsg><title>x</title></appmsg></msg>"))

    def test_empty_input(self):
        self.assertIn("error", parse_chat_record(""))
        self.assertIn("error", parse_chat_record(None))


class TestFindRawContent(unittest.TestCase):
    def test_missing_source(self):
        # 源库不存在 / 文件名为空时返回 None，而不是抛错
        self.assertIsNone(find_raw_content("definitely_not_a_real_db.db", 1, 1))
        self.assertIsNone(find_raw_content("", 1, 1))


class TestGetChatRecordErrors(unittest.TestCase):
    def _require_db(self):
        try:
            from wechat_mcp.database import get_connection

            conn = get_connection()
            conn.execute("SELECT 1 FROM messages LIMIT 1")
            conn.close()
        except Exception:
            self.skipTest("本地没有 wechat 索引库，跳过")

    def test_unknown_message_id(self):
        self._require_db()
        from wechat_mcp.records import get_chat_record

        r = get_chat_record(-1)
        self.assertIn("error", r)

    def test_non_chat_record_message(self):
        self._require_db()
        import sqlite3

        from wechat_mcp.database import get_connection
        from wechat_mcp.records import get_chat_record

        conn = get_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM messages WHERE local_type = 1 LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            self.skipTest("索引里没有纯文本消息，跳过")
        r = get_chat_record(row["id"])
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
