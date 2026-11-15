"""rag.py 核心逻辑单元测试。

覆盖：文本切块、页码提取、按页去重、检索去重、文档缓存命中/未命中。
用标准库 unittest，不依赖网络与模型 Key（向量化用 mock 打桩），可离线跑。

运行：
    .venv/Scripts/python.exe -m unittest tests.test_rag -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 让测试能 import backend/rag.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import rag  # noqa: E402


class TestSplitText(unittest.TestCase):
    """文本切块：段落切分 + 长段落重叠切分。"""

    def test_empty(self):
        self.assertEqual(rag.split_text(""), [])
        self.assertEqual(rag.split_text(None), [])

    def test_short_para_kept(self):
        chunks = rag.split_text("这是一段短文本。")
        self.assertEqual(chunks, ["这是一段短文本。"])

    def test_multi_para(self):
        text = "第一段。\n\n第二段。\n\n第三段。"
        self.assertEqual(rag.split_text(text), ["第一段。", "第二段。", "第三段。"])

    def test_long_para_overlap(self):
        text = "字" * 1200  # 超长单段
        chunks = rag.split_text(text, chunk_size=500, overlap=50)
        self.assertGreaterEqual(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), 500)
        # 重叠切分：相邻块有公共部分
        self.assertEqual(chunks[0][-50:], chunks[1][:50])


class TestExtractPage(unittest.TestCase):
    """从块文本提取 [[PAGE N]] 页码。"""

    def test_with_marker(self):
        self.assertEqual(rag._extract_page("[[PAGE 5]]\n内容"), 5)

    def test_no_marker(self):
        self.assertEqual(rag._extract_page("没有标记的内容"), 0)

    def test_first_marker_wins(self):
        # 跨页块取第一个标记
        self.assertEqual(rag._extract_page("[[PAGE 3]]\n...[[PAGE 4]]..."), 3)


class TestDedupeByPage(unittest.TestCase):
    """按页去重纯函数：同页只留高分块，page=0 不参与去重。"""

    def _block(self, page, score):
        return (score, {"text": "t", "doc_name": "d", "page": page})

    def test_dedupe_same_page(self):
        scored = [self._block(3, 0.9), self._block(3, 0.8), self._block(5, 0.7)]
        deduped = rag._dedupe_by_page(scored)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0][1]["page"], 3)  # 高分块
        self.assertEqual(deduped[1][1]["page"], 5)

    def test_page_zero_not_deduped(self):
        scored = [self._block(0, 0.9), self._block(0, 0.8), self._block(2, 0.7)]
        deduped = rag._dedupe_by_page(scored)
        self.assertEqual(len(deduped), 3)  # 两个 page=0 + 一个 page=2

    def test_no_duplicate_pages(self):
        scored = [self._block(1, 0.9), self._block(2, 0.8), self._block(3, 0.7)]
        self.assertEqual(len(rag._dedupe_by_page(scored)), 3)


class _RagPathsMixin:
    """隔离 rag 的向量库/缓存/上传目录到临时目录，避免污染真实 data/rag。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        rag.VECTORS_PATH = tmp / "vectors.json"
        rag.RAG_DIR = tmp / "rag"
        rag.CACHE_DIR = tmp / "cache"
        rag.UPLOADS_DIR = tmp / "uploads"


class TestRetrieveDedupe(_RagPathsMixin, unittest.TestCase):
    """检索按页去重：mock 向量化，验证覆盖不同页、后面页能被检索到。"""

    def test_dedupe_covers_multiple_pages(self):
        blocks = [
            {"id": "a1", "doc_id": "d", "doc_name": "教材", "text": "页1块1", "embedding": [1, 0], "page": 1},
            {"id": "a2", "doc_id": "d", "doc_name": "教材", "text": "页1块2", "embedding": [0.9, 0.1], "page": 1},
            {"id": "a6", "doc_id": "d", "doc_name": "教材", "text": "页6块", "embedding": [0.8, 0.2], "page": 6},
            {"id": "b1", "doc_id": "d", "doc_name": "教材", "text": "无页1", "embedding": [0.7, 0.3]},  # page=0
            {"id": "b2", "doc_id": "d", "doc_name": "教材", "text": "无页2", "embedding": [0.6, 0.4]},  # page=0
        ]
        rag._save_store(blocks)
        with mock.patch.object(rag, "embed_texts", return_value=[[1, 0]]):
            docs = rag.retrieve("query", k=10, dedupe_page=True)
        pages = [d["page"] for d in docs]
        # 页1去重留1块 + 页6 1块 + page=0 两块 = 4块
        self.assertEqual(len(docs), 4)
        self.assertEqual(pages.count(1), 1)  # 同页只一块
        self.assertIn(6, pages)  # 后面页能被检索到

    def test_no_dedupe_returns_all(self):
        blocks = [
            {"id": "a1", "doc_id": "d", "doc_name": "教材", "text": "页1块1", "embedding": [1, 0], "page": 1},
            {"id": "a2", "doc_id": "d", "doc_name": "教材", "text": "页1块2", "embedding": [0.9, 0.1], "page": 1},
        ]
        rag._save_store(blocks)
        with mock.patch.object(rag, "embed_texts", return_value=[[1, 0]]):
            docs = rag.retrieve("query", k=10, dedupe_page=False)
        self.assertEqual(len(docs), 2)  # 不去重，两块都返回

    def test_empty_store(self):
        with mock.patch.object(rag, "embed_texts", return_value=[[1, 0]]):
            self.assertEqual(rag.retrieve("query", k=5), [])


class TestCacheHit(_RagPathsMixin, unittest.TestCase):
    """文档缓存：命中跳过 OCR+Embedding，未命中才调模型。"""

    def test_cache_hit_skips_parse_and_embed(self):
        content = b"some cached doc content"
        h = rag._file_hash(content)
        cached_chunks = [{"text": "缓存块", "embedding": [0.1, 0.2], "page": 1}]
        rag._save_cache(h, {"hash": h, "filename": "test.txt", "chunks": cached_chunks, "ocr_used": False})
        # 命中缓存时不应调用解析与向量化
        with mock.patch.object(rag, "parse_document", side_effect=AssertionError("不应解析")) as m_parse, \
             mock.patch.object(rag, "embed_texts", side_effect=AssertionError("不应嵌入")) as m_embed:
            result = rag.add_document("test.txt", content)
        self.assertTrue(result["cached"])
        self.assertEqual(result["chunks"], 1)
        m_parse.assert_not_called()
        m_embed.assert_not_called()
        # 缓存块写入向量库
        store = rag._load_store()
        self.assertEqual(len(store), 1)
        self.assertEqual(store[0]["text"], "缓存块")
        self.assertEqual(store[0]["page"], 1)

    def test_cache_miss_calls_parse_and_embed(self):
        content = b"new doc content not cached"
        h = rag._file_hash(content)
        with mock.patch.object(rag, "parse_document", return_value=("一段文本内容", False)) as m_parse, \
             mock.patch.object(rag, "embed_texts", return_value=[[0.1, 0.2]]) as m_embed:
            result = rag.add_document("test.txt", content)
        self.assertFalse(result["cached"])
        m_parse.assert_called_once()
        m_embed.assert_called_once()
        self.assertIsNotNone(rag._load_cache(h))  # 缓存已写

    def test_file_hash_stable(self):
        # 内容相同 hash 相同，内容变 hash 变
        self.assertEqual(rag._file_hash(b"abc"), rag._file_hash(b"abc"))
        self.assertNotEqual(rag._file_hash(b"abc"), rag._file_hash(b"abd"))


if __name__ == "__main__":
    unittest.main()
