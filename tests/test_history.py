"""main.py 多会话历史逻辑单元测试。

覆盖：会话创建、老扁平格式迁移、会话隔离、当前会话切换、历史上限截断。
用标准库 unittest，隔离 HIST_FILE 到临时目录，不触碰真实 data/chat_history.json。

运行：
    .venv/Scripts/python.exe -m unittest tests.test_history -v
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import main  # noqa: E402


class _HistPathsMixin:
    """把 HIST_FILE 指向临时文件，隔离真实历史数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hist_file = Path(self.tmp.name) / "chat_history.json"
        main.HIST_FILE = self.hist_file

    def _write_raw(self, data):
        self.hist_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestNewSession(unittest.TestCase):
    def test_structure(self):
        s = main._new_session("测试会话")
        self.assertEqual(s["title"], "测试会话")
        self.assertEqual(s["messages"], [])
        self.assertTrue(s["id"])
        self.assertTrue(s["created_at"])
        self.assertTrue(s["updated_at"])


class TestHistLoad(_HistPathsMixin, unittest.TestCase):
    def test_empty_creates_default(self):
        state = main._hist_load()
        self.assertEqual(len(state["sessions"]), 1)
        self.assertEqual(state["sessions"][0]["title"], "默认会话")
        self.assertEqual(state["active_id"], state["sessions"][0]["id"])

    def test_legacy_flat_migration(self):
        # 老扁平 {items:[...]} 自动迁移成单会话，消息保留
        self._write_raw({"items": [{"role": "user", "text": "老消息1"}, {"role": "pet", "text": "老消息2"}]})
        state = main._hist_load()
        self.assertEqual(len(state["sessions"]), 1)
        msgs = state["sessions"][0]["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["text"], "老消息1")
        on_disk = json.loads(self.hist_file.read_text(encoding="utf-8"))
        self.assertIn("sessions", on_disk)  # 迁移后已写盘

    def test_new_format_loaded(self):
        sid = "abc12345"
        self._write_raw({
            "sessions": [{"id": sid, "title": "A", "messages": [{"role": "user", "text": "hi"}],
                          "created_at": "", "updated_at": ""}],
            "active_id": sid,
        })
        state = main._hist_load()
        self.assertEqual(state["active_id"], sid)
        self.assertEqual(len(state["sessions"]), 1)

    def test_invalid_active_id_fallback(self):
        sid = "abc12345"
        self._write_raw({
            "sessions": [{"id": sid, "title": "A", "messages": [], "created_at": "", "updated_at": ""}],
            "active_id": "不存在的id",
        })
        state = main._hist_load()
        self.assertEqual(state["active_id"], sid)  # 回退到第一个会话


class TestSessionIsolation(_HistPathsMixin, unittest.TestCase):
    def test_messages_isolated_between_sessions(self):
        state = main._hist_load()
        s1 = main._new_session("会话A")
        s2 = main._new_session("会话B")
        state["sessions"].extend([s1, s2])
        state["active_id"] = s1["id"]
        main._hist_save(state)

        st = main._hist_load()
        main._find_session(st, s1["id"])["messages"].append({"role": "user", "text": "A的消息", "ts": ""})
        main._hist_save(st)

        st2 = main._hist_load()
        main._find_session(st2, s2["id"])["messages"].append({"role": "user", "text": "B的消息", "ts": ""})
        main._hist_save(st2)

        st3 = main._hist_load()
        a = main._find_session(st3, s1["id"])
        b = main._find_session(st3, s2["id"])
        self.assertEqual([m["text"] for m in a["messages"]], ["A的消息"])
        self.assertEqual([m["text"] for m in b["messages"]], ["B的消息"])

    def test_active_session_switch(self):
        state = main._hist_load()
        s1 = main._new_session("A")
        s2 = main._new_session("B")
        state["sessions"].extend([s1, s2])
        state["active_id"] = s1["id"]
        main._hist_save(state)

        st = main._hist_load()
        self.assertEqual(main._active_session(st)["id"], s1["id"])
        st["active_id"] = s2["id"]
        main._hist_save(st)
        st2 = main._hist_load()
        self.assertEqual(main._active_session(st2)["id"], s2["id"])

    def test_find_session_missing(self):
        state = {"sessions": [main._new_session()], "active_id": None}
        self.assertIsNone(main._find_session(state, "不存在"))


class TestHistoryAdd(_HistPathsMixin, unittest.IsolatedAsyncioTestCase):
    """端点级：加历史 + 上限截断 + 指定会话隔离（mock publish_event 避免 SSE 副作用）。"""

    async def test_add_to_current_session(self):
        with mock.patch.object(main, "publish_event", return_value=None):
            await main.history_add(main.HistoryAddRequest(role="user", text="你好"))
        st = main._hist_load()
        msgs = main._active_session(st)["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "你好")
        self.assertEqual(msgs[0]["role"], "user")

    async def test_add_trim_to_max(self):
        with mock.patch.object(main, "publish_event", return_value=None):
            for i in range(main.HIST_MAX + 10):
                await main.history_add(main.HistoryAddRequest(role="user", text=f"m{i}"))
        st = main._hist_load()
        msgs = main._active_session(st)["messages"]
        self.assertEqual(len(msgs), main.HIST_MAX)
        self.assertEqual(msgs[-1]["text"], f"m{main.HIST_MAX + 9}")

    async def test_add_to_specific_session_isolated(self):
        state = main._hist_load()
        s1 = main._new_session("A")
        s2 = main._new_session("B")
        state["sessions"].extend([s1, s2])
        state["active_id"] = s1["id"]
        main._hist_save(state)
        with mock.patch.object(main, "publish_event", return_value=None):
            await main.history_add(main.HistoryAddRequest(role="user", text="进B", session_id=s2["id"]))
        st = main._hist_load()
        self.assertEqual(len(main._find_session(st, s1["id"])["messages"]), 0)
        self.assertEqual(len(main._find_session(st, s2["id"])["messages"]), 1)

    async def test_empty_text_noop(self):
        with mock.patch.object(main, "publish_event", return_value=None) as m:
            await main.history_add(main.HistoryAddRequest(role="user", text="   "))
        m.assert_not_called()  # 空文本不广播、不写入


if __name__ == "__main__":
    unittest.main()
