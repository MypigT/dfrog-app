"""agent.py 工具循环核心单测。

覆盖：路径解析、各工具执行（读/写/移动/改名/删/命令/提醒/速记）、确认闸门、
run_agent 的确认放行/拒绝流程。不依赖网络与模型 Key（run_agent 用 mock 客户端），
可离线跑。

运行：
    .venv/Scripts/python.exe -m unittest tests.test_agent -v
"""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import agent  # noqa: E402


class _RootMixin:
    """隔离工作目录到临时目录，避免污染真实数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write(self, rel, content=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p


class TestResolvePath(unittest.TestCase):
    """_resolve_path：相对路径挂到 root 下，绝对路径直接用。"""

    def test_relative_under_root(self):
        root = Path("C:/work")
        self.assertEqual(agent._resolve_path("sub/f.txt", root), (root / "sub/f.txt").resolve())

    def test_absolute_used_directly(self):
        root = Path("C:/work")
        self.assertEqual(agent._resolve_path("C:/other/f.txt", root), Path("C:/other/f.txt").resolve())

    def test_empty_resolves_to_root(self):
        root = Path("C:/work")
        self.assertEqual(agent._resolve_path("", root), root.resolve())


class TestExecuteTool(_RootMixin, unittest.TestCase):
    """_execute_tool 各工具的执行结果。"""

    def test_list_files(self):
        self._write("a.txt")
        self._write("sub/b.txt")
        out = agent._execute_tool("list_files", {"dir": ""}, self.root)
        self.assertIn("a.txt", out)
        self.assertIn("sub", out)

    def test_list_files_missing_dir(self):
        out = agent._execute_tool("list_files", {"dir": "no/such"}, self.root)
        self.assertIn("目录不存在", out)

    def test_read_file(self):
        self._write("a.txt", "hello world")
        self.assertEqual(agent._execute_tool("read_file", {"path": "a.txt"}, self.root), "hello world")

    def test_read_file_missing(self):
        out = agent._execute_tool("read_file", {"path": "nope.txt"}, self.root)
        self.assertIn("文件不存在", out)

    def test_search_files(self):
        self._write("report.txt")
        self._write("note.md")
        out = agent._execute_tool("search_files", {"keyword": "report"}, self.root)
        self.assertIn("report.txt", out)
        self.assertNotIn("note.md", out)

    def test_content_search(self):
        self._write("a.txt", "foo bar\nbaz")
        self._write("b.txt", "qux")
        out = agent._execute_tool("content_search", {"keyword": "foo"}, self.root)
        self.assertIn("a.txt", out)
        self.assertNotIn("b.txt", out)

    def test_get_current_time(self):
        out = agent._execute_tool("get_current_time", {}, self.root)
        self.assertIn("现在", out)

    def test_write_file(self):
        out = agent._execute_tool("write_file", {"path": "new.txt", "content": "data"}, self.root)
        self.assertIn("写好了", out)
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "data")

    def test_edit_file(self):
        self._write("a.txt", "hello world")
        out = agent._execute_tool("edit_file", {"path": "a.txt", "old_text": "hello", "new_text": "hi"}, self.root)
        self.assertIn("改好了", out)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "hi world")

    def test_edit_file_old_not_found(self):
        self._write("a.txt", "hello world")
        out = agent._execute_tool("edit_file", {"path": "a.txt", "old_text": "nope", "new_text": "x"}, self.root)
        self.assertIn("找不到", out)

    def test_move_file(self):
        self._write("a.txt")
        out = agent._execute_tool("move_file", {"src": "a.txt", "dst_dir": "sub"}, self.root)
        self.assertIn("移动", out)
        self.assertTrue((self.root / "sub" / "a.txt").exists())

    def test_rename_file(self):
        self._write("a.txt")
        out = agent._execute_tool("rename_file", {"old_path": "a.txt", "new_path": "b.txt"}, self.root)
        self.assertIn("改名", out)
        self.assertTrue((self.root / "b.txt").exists())

    def test_delete_file(self):
        self._write("a.txt")
        out = agent._execute_tool("delete_file", {"path": "a.txt"}, self.root)
        self.assertIn("删掉了", out)
        self.assertFalse((self.root / "a.txt").exists())

    def test_delete_nonempty_dir(self):
        self._write("sub/a.txt")
        out = agent._execute_tool("delete_file", {"path": "sub"}, self.root)
        self.assertIn("非空", out)

    def test_bash_empty(self):
        self.assertEqual(agent._execute_tool("bash", {"command": "  "}, self.root), "命令为空")

    def test_bash_dangerous_blocked(self):
        out = agent._execute_tool("bash", {"command": "rm -rf /"}, self.root)
        self.assertIn("拦截", out)

    def test_bash_normal(self):
        out = agent._execute_tool("bash", {"command": "echo hi"}, self.root)
        self.assertIn("exit 0", out)
        self.assertIn("hi", out)

    def test_unknown_tool(self):
        self.assertIn("工具不存在", agent._execute_tool("nope", {}, self.root))


class TestReminderNote(unittest.TestCase):
    """add_reminder / add_note 写 SQLite，隔离 DB_PATH 到临时库并建表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "naiwa.db"
        agent.DB_PATH = self.db
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, "
            "due_at TEXT NOT NULL, done INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, "
            "created_at TEXT DEFAULT (datetime('now')))"
        )
        conn.commit()
        conn.close()

    def test_add_reminder(self):
        out = agent._execute_tool("add_reminder", {"text": "喝水", "delay_seconds": 60}, Path("."))
        self.assertIn("已提醒", out)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT text FROM reminders").fetchone()[0], "喝水")
        conn.close()

    def test_add_note(self):
        out = agent._execute_tool("add_note", {"text": "面试"}, Path("."))
        self.assertIn("记下", out)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT text FROM notes").fetchone()[0], "面试")
        conn.close()


class TestConfirmGate(unittest.TestCase):
    """确认闸门：resolve_confirm 把主人的决定灌回挂起的 future。"""

    def test_resolve_approve(self):
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            agent._confirm_futures["t1"] = fut
            self.assertTrue(agent.resolve_confirm("t1", True))
            self.assertTrue(fut.done())
            self.assertTrue(fut.result())
        finally:
            agent._confirm_futures.pop("t1", None)
            loop.close()

    def test_resolve_deny(self):
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            agent._confirm_futures["t2"] = fut
            self.assertTrue(agent.resolve_confirm("t2", False))
            self.assertFalse(fut.result())
        finally:
            agent._confirm_futures.pop("t2", None)
            loop.close()

    def test_resolve_unknown_id(self):
        self.assertFalse(agent.resolve_confirm("no-such-id", True))


def _make_resp(tool_calls=None, content=""):
    """构造一个假的 chat.completions.create 返回值。"""
    resp = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    resp.choices = [mock.MagicMock()]
    resp.choices[0].message = msg
    return resp


def _make_tool_call(name, arguments):
    tc = mock.MagicMock()
    tc.id = "call_1"
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


class _FakeClient:
    """按顺序返回预设响应的假客户端。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.chat = mock.MagicMock()
        self.chat.completions = mock.MagicMock()

        async def create(**kwargs):
            return self._responses.pop(0)

        self.chat.completions.create = create


class TestRunAgentConfirm(_RootMixin, unittest.IsolatedAsyncioTestCase):
    """run_agent 确认流程：放行则执行写工具，拒绝则跳过。"""

    async def test_confirm_approve_executes(self):
        tc = _make_tool_call("write_file", '{"path": "out.txt", "content": "hello"}')
        client = _FakeClient([_make_resp(tool_calls=[tc]), _make_resp(content="写完了")])
        with mock.patch.object(agent, "_make_client", return_value=client):
            events = []
            async for ev in agent.run_agent("写个文件", root=str(self.root)):
                events.append(ev)
                if ev.get("type") == "agent_confirm":
                    agent.resolve_confirm(ev["confirm_id"], True)
        self.assertTrue((self.root / "out.txt").exists())
        self.assertEqual((self.root / "out.txt").read_text(encoding="utf-8"), "hello")
        # 确认事件与执行事件都出现过
        self.assertTrue(any(e.get("type") == "agent_confirm" for e in events))
        self.assertTrue(any(e.get("type") == "agent_action" for e in events))

    async def test_confirm_deny_skips(self):
        tc = _make_tool_call("write_file", '{"path": "out.txt", "content": "hello"}')
        client = _FakeClient([_make_resp(tool_calls=[tc]), _make_resp(content="没放行，跳过")])
        with mock.patch.object(agent, "_make_client", return_value=client):
            async for ev in agent.run_agent("写个文件", root=str(self.root)):
                if ev.get("type") == "agent_confirm":
                    agent.resolve_confirm(ev["confirm_id"], False)
        # 拒绝后不写文件
        self.assertFalse((self.root / "out.txt").exists())


if __name__ == "__main__":
    unittest.main()
