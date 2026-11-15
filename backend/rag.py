"""
奶龙桌宠 · 轻量 RAG 文档问答模块

参考 ref-projects/awesome-llm-apps/rag_tutorials/rag_chain/app.py，但走轻量自实现：
- 不依赖 langchain / chromadb（避免依赖安装坑）。
- Embedding：千问 text-embedding-v3（DashScope，OpenAI 兼容协议，DASHSCOPE_API_KEY）。
- 聊天：DeepSeek 优先、千问回退（与 main.py 的 stream_naiwa_reply 同一套通道）。
- 向量存本地 JSON 文件，纯 Python 余弦相似度检索，无需 numpy。
- PDF 用 pypdf 解析；txt/md 直接解码。

全部使用用户自己的模型，不引入任何国外模型、不下载模型文件。
"""

import asyncio
import logging
import base64
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from openai import AsyncOpenAI, OpenAI
from PIL import Image

logger = logging.getLogger("dfrog")

# ---- 模型配置（env 已在 main.py 的 load_dotenv() 之后加载，本模块被其导入）----
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DASHSCOPE_CHAT_MODEL = os.environ.get("DASHSCOPE_CHAT_MODEL", "qwen3.8-max-0902")
EMBEDDING_MODEL = os.environ.get("DASHSCOPE_EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIM = 1024

# ---- 数据目录（与 main.py 同一套 frozen/非 frozen 逻辑，避免循环导入所以自算）----
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", Path(sys.executable).parent)) / "dfrog"
else:
    DATA_DIR = Path(__file__).parent.parent / "data"

RAG_DIR = DATA_DIR / "rag"
VECTORS_PATH = RAG_DIR / "vectors.json"
UPLOADS_DIR = RAG_DIR / "uploads"
CACHE_DIR = RAG_DIR / "cache"

# ---- 提示词 ----
RAG_ANSWER_SYSTEM = """你是一名严谨的文档问答助手。请**仅根据以下上下文**回答用户的问题。
要求：
- 只依据上下文作答，不要编造上下文中没有的信息；
- 上下文没覆盖的部分，直接说"文档里没有提到"；
- 回答准确、简洁，必要时分点。

【文档上下文】
{context}
"""

RAG_QUIZ_SYSTEM = """你是一名出题助手。请**根据以下文档内容**为用户出题。
要求：
- 题目必须能从文档内容中找到依据，不要凭空编造；
- 默认出 5 道题，用户指定数量则按其要求；
- 题型可多样（选择、填空、简答、判断等），每道题给出【答案】和简短【解析】；
- 用清晰的编号和格式输出。

【文档上下文】
{context}
"""


# ==================== 向量存储读写 ====================
def _load_store() -> list:
    if VECTORS_PATH.exists():
        try:
            return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.info(f"[Dfrog] RAG 向量库读取失败，重置: {e}")
    return []


def _save_store(store: list):
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    VECTORS_PATH.write_text(
        json.dumps(store, ensure_ascii=False), encoding="utf-8"
    )


# ==================== Embedding ====================
_embedding_client: OpenAI | None = None


def _get_embedding_client() -> OpenAI:
    global _embedding_client
    if _embedding_client is None:
        if not DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法使用千问 Embedding")
        _embedding_client = OpenAI(
            api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL
        )
    return _embedding_client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化（按 10 条一批——千问 text-embedding-v3 单次上限 10，超过报 400）"""
    client = _get_embedding_client()
    all_vecs: list[list[float]] = []
    batch = 10
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=chunk,
            dimensions=EMBEDDING_DIM,
        )
        # 按 index 排序，保证顺序与输入一致
        data = sorted(resp.data, key=lambda d: d.index)
        all_vecs.extend(d.embedding for d in data)
    return all_vecs


# ==================== 文本切分（纯 Python，不依赖模型）====================
def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """先按段落切，过长的段落再按字符切（带重叠）"""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            chunks.append(para)
        else:
            step = chunk_size - overlap
            for i in range(0, len(para), step):
                chunks.append(para[i : i + chunk_size])
                if i + chunk_size >= len(para):
                    break
    return chunks


_PAGE_RE = re.compile(r"\[\[PAGE (\d+)\]\]")


def _extract_page(chunk_text: str) -> int:
    """从块文本中提取 [[PAGE N]] 页码，无标记返回 0。"""
    m = _PAGE_RE.search(chunk_text)
    return int(m.group(1)) if m else 0


# ==================== 文档解析 ====================
# 扫描版 PDF 没有文字层，PyMuPDF 提取不到文字。这里把页面渲染成图片，
# 用你自己的千问视觉模型（qwen3.8-max-0902，多模态）把图里的文字认出来——
# 用的是你自己的模型、不引入本地 OCR 依赖、不联网下载模型、Python 3.15 可用。
OCR_PROMPT = """请仔细识别这张图片中的所有文字，按原文逐字输出，保持原有的段落和换行。
只输出识别到的文字本身，不要解释、不要加任何说明或前后缀。
如果图片中没有文字，就输出一个空行。"""


def _ocr_page(page) -> str:
    """把 PDF 页渲染成图片，用千问视觉模型识别文字。失败或没配 Key 返回空串。"""
    if not DASHSCOPE_API_KEY:
        return ""
    from openai import OpenAI

    pix = page.get_pixmap(dpi=200)
    img = pix.pil_image()  # PIL Image
    # 控制图片体积（API 计费看体积）：过宽则缩放
    MAX_W = 1800
    if img.width > MAX_W:
        new_h = int(img.height * MAX_W / img.width)
        img = img.resize((MAX_W, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
    # 并行 OCR 时可能触发限流/瞬时网络错误：重试 2 次（2s/4s 退避）
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=DASHSCOPE_CHAT_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }],
                max_tokens=2000,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            logger.info(f"[Dfrog] RAG 该页视觉 OCR 失败: {e}")
            return ""
    return ""


def _parse_pdf(content_bytes: bytes, progress_cb=None, hash_h: str | None = None) -> tuple[str, bool]:
    """PyMuPDF 提取文字；文字过少的页（扫描页）渲染成图片走千问视觉 OCR。
    多页**并行** OCR（4 路，大幅缩短大扫描 PDF 耗时）；progress_cb(done,total,page)
    可选，每识别完一页回调一次供前端显示进度。
    hash_h 非空时启用断点检查点：已识别的页（cache/{hash}.ocr.jsonl）直接复用、跳过 OCR，
    新识别的页逐页追加保存——中途中断（额度耗尽/断网/关窗）后重新上传自动续传，不重复消耗。
    返回 (全文, 是否用了 OCR)"""
    import pymupdf  # 即 fitz
    from concurrent.futures import ThreadPoolExecutor

    # 先探页数（开一个临时 doc 取长度即关）
    probe = pymupdf.open(stream=content_bytes, filetype="pdf")
    n = len(probe)
    probe.close()

    # 加载已有 OCR 检查点（断点续传：已识别的页直接复用，不再调模型）
    ckpt: dict = _load_ocr_ckpt(hash_h) if hash_h else {}
    resumed = len(ckpt)
    if resumed:
        logger.info(f"[Dfrog] RAG 续传：复用 {resumed}/{n} 页已识别数据，继续识别剩余 {n - resumed} 页")

    text_parts: list[str | None] = [None] * n
    ocr_used: list[bool] = [False] * n
    done = [0]
    lock = threading.Lock()        # done/进度计数
    ckpt_lock = threading.Lock()   # 检查点文件写入

    # 每路 worker 各开一份独立 doc（PyMuPDF 非线程安全），处理连续一段，结束即关
    def process_range(start: int, end: int):
        doc = pymupdf.open(stream=content_bytes, filetype="pdf")
        try:
            for i in range(start, end):
                # 检查点命中：直接复用已识别的文字，跳过 OCR（不消耗额度）
                if i in ckpt:
                    rec = ckpt[i]
                    text_parts[i] = rec["text"]
                    ocr_used[i] = rec.get("ocr_used", False)
                    with lock:
                        done[0] += 1
                        if progress_cb:
                            try:
                                progress_cb(done[0], n, i + 1)
                            except Exception:
                                pass
                    continue
                page = doc[i]
                page_text = page.get_text("text").strip()
                # 文字太少 → 大概率是扫描页，渲染成图片走千问视觉 OCR
                if len(page_text) < 20:
                    ocr_text = _ocr_page(page)
                    if ocr_text:
                        ocr_used[i] = True
                        page_text = ocr_text or page_text
                text_parts[i] = page_text
                # 成功识别（有文字层 或 OCR 成功）才写检查点；OCR 失败的页下次重试
                if hash_h and (len(page_text) >= 20 or ocr_used[i]):
                    _append_ocr_ckpt(hash_h, i, page_text, ocr_used[i], ckpt_lock)
                with lock:
                    done[0] += 1
                    if progress_cb:
                        try:
                            progress_cb(done[0], n, i + 1)
                        except Exception:
                            pass
        finally:
            doc.close()

    workers = 4
    chunk = max(1, (n + workers - 1) // workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for w in range(workers):
            s = w * chunk
            e = min(n, s + chunk)
            if s < e:
                futs.append(ex.submit(process_range, s, e))
        for f in futs:
            f.result()  # 等所有路完成，异常在此抛出

    # 兜底：万一有空位（理论上不会），补空串
    # 每页前加 [[PAGE N]] 标记：检索时按页去重、定位来源页，避免大文档前几页霸榜、后面页检索不到
    text = "\n\n".join(f"[[PAGE {i + 1}]]\n{t or ''}" for i, t in enumerate(text_parts))
    return text, any(ocr_used)


def parse_document(filename: str, content_bytes: bytes, progress_cb=None, hash_h: str | None = None) -> tuple[str, bool]:
    """PDF 用 PyMuPDF 提取（扫描页自动千问视觉 OCR，多页并行、断点续传）；txt/md/其他按文本解码。
    返回 (全文, 是否用了 OCR)；progress_cb 仅 PDF 生效；hash_h 仅 PDF 生效（启用 OCR 检查点）"""
    name = filename.lower()
    if name.endswith(".pdf"):
        try:
            return _parse_pdf(content_bytes, progress_cb, hash_h)
        except Exception as e:
            raise ValueError(f"PDF 解析失败：{e}")
    # 文本类
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return content_bytes.decode(enc), False
        except Exception:
            continue
    return content_bytes.decode("utf-8", errors="replace"), False


# ==================== 余弦相似度（纯 Python）====================
def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ==================== 文档缓存（按内容哈希，省 OCR + Embedding 消耗）====================
def _file_hash(content_bytes: bytes) -> str:
    """文档内容 sha256，作为身份；内容变了 hash 才变，保证同文件必命中。"""
    return hashlib.sha256(content_bytes).hexdigest()


def _cache_path(h: str) -> Path:
    return CACHE_DIR / f"{h}.json"


def _load_cache(h: str) -> dict | None:
    p = _cache_path(h)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.info(f"[Dfrog] RAG 缓存读取失败，忽略: {e}")
    return None


def _save_cache(h: str, entry: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(h).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.info(f"[Dfrog] RAG 缓存写入失败（忽略）: {e}")


# ==================== OCR 断点检查点（逐页保存识别结果，中断可续传）====================
# 扫描版大 PDF 走视觉 OCR 耗时长、易被额度耗尽/断网/关窗打断。这里把每一页识别出的文字
# 逐页追加写到 cache/{hash}.ocr.jsonl（一行一页）。下次上传同一文档（同 hash）时，
# 已识别的页直接复用、跳过 OCR，只补识别剩余页——不重复消耗模型额度。
# 全量缓存（含切块+向量）保存成功后，检查点冗余，自动删除。
def _ocr_ckpt_path(h: str) -> Path:
    return CACHE_DIR / f"{h}.ocr.jsonl"


def _load_ocr_ckpt(h: str) -> dict:
    """读取已有 OCR 检查点 → {页码: {"text":..., "ocr_used":...}}。无文件返回空 dict。"""
    p = _ocr_ckpt_path(h)
    if not p.exists():
        return {}
    pages: dict[int, dict] = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                pg = rec.get("page")
                if pg is not None:
                    pages[pg] = {
                        "text": rec.get("text", ""),
                        "ocr_used": rec.get("ocr_used", False),
                    }
    except Exception as e:
        logger.info(f"[Dfrog] RAG OCR 检查点读取失败，忽略: {e}")
    return pages


def _append_ocr_ckpt(h: str, page: int, text: str, ocr_used: bool, lock: threading.Lock):
    """把一页识别结果追加到检查点文件（线程安全，多 worker 并发写）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"page": page, "text": text, "ocr_used": ocr_used}, ensure_ascii=False
    )
    with lock:
        try:
            with open(_ocr_ckpt_path(h), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.info(f"[Dfrog] RAG OCR 检查点写入失败（忽略）: {e}")


def _delete_ocr_ckpt(h: str):
    """全量缓存已保存后，检查点冗余，删除。"""
    p = _ocr_ckpt_path(h)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ==================== 核心函数 ====================
def add_document(filename: str, content_bytes: bytes, progress_cb=None) -> dict:
    """解析 → 切分 → 向量化 → 入库。同文档命中缓存则跳过 OCR + Embedding。
    返回文档摘要（含 cached 标记，前端据此提示是否复用缓存）。
    progress_cb(done,total,page) 供 PDF OCR 进度回调（命中缓存时不调）。"""
    from datetime import datetime

    h = _file_hash(content_bytes)
    cached = _load_cache(h)
    safe_name = Path(filename).name  # 去掉路径，防目录穿越

    if cached:
        # 命中缓存：直接复用切块（含 embedding），跳过 OCR 和 Embedding 调用
        chunks = cached["chunks"]  # [{text, embedding}]
    else:
        # 传 hash_h 启用 OCR 断点检查点：已识别的页复用、只补识别剩余页
        text, ocr_used = parse_document(filename, content_bytes, progress_cb, hash_h=h)
        chunks_text = split_text(text)
        if not chunks_text:
            raise ValueError("文档没有可解析的文本内容（可能是扫描版 PDF）")
        embeddings = embed_texts(chunks_text)
        # 提取每块页码（[[PAGE N]] 标记）；页内无标记的块沿用上一块页码，便于检索按页去重
        pages: list[int] = []
        last_pg = 0
        for ct in chunks_text:
            pg = _extract_page(ct)
            if pg:
                last_pg = pg
            else:
                pg = last_pg
            pages.append(pg)
        chunks = [
            {"text": t, "embedding": e, "page": p}
            for t, p, e in zip(chunks_text, pages, embeddings)
        ]
        _save_cache(h, {
            "hash": h,
            "filename": safe_name,
            "chunks": chunks,
            "ocr_used": ocr_used,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        # 全量缓存已存（含全部切块+向量），OCR 检查点冗余，删除
        _delete_ocr_ckpt(h)

    doc_id = uuid.uuid4().hex[:12]

    # 存原始文件（用 doc_id 前缀，便于按 doc_id 删除；缓存只存解析结果，不存原始文件）
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / f"{doc_id}__{safe_name}").write_bytes(content_bytes)

    store = _load_store()
    # 覆盖：先删同 doc_id 旧块
    store = [c for c in store if c["doc_id"] != doc_id]
    for i, ch in enumerate(chunks):
        store.append({
            "id": f"{doc_id}-{i}",
            "doc_id": doc_id,
            "doc_name": safe_name,
            "text": ch["text"],
            "embedding": ch["embedding"],
            "page": ch.get("page", 0),
        })
    _save_store(store)
    return {
        "doc_id": doc_id,
        "doc_name": safe_name,
        "chunks": len(chunks),
        "cached": bool(cached),
    }


def list_documents() -> list[dict]:
    store = _load_store()
    docs: dict[str, dict] = {}
    for c in store:
        d = docs.setdefault(
            c["doc_id"],
            {"doc_id": c["doc_id"], "doc_name": c["doc_name"], "chunks": 0},
        )
        d["chunks"] += 1
    return list(docs.values())


def delete_document(doc_id: str) -> bool:
    store = _load_store()
    kept = [c for c in store if c["doc_id"] != doc_id]
    if len(kept) == len(store):
        return False
    _save_store(kept)
    # 删上传的原始文件
    for f in UPLOADS_DIR.glob(f"{doc_id}__*"):
        try:
            f.unlink()
        except Exception:
            pass
    return True


def _dedupe_by_page(scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """按页去重：scored 已按分数降序，同页只保留最相关的一块。
    page=0（无页码，如旧数据/非 PDF 文档）的块不参与去重，全部保留，避免误删。"""
    seen_pages: set[int] = set()
    deduped: list[tuple[float, dict]] = []
    for s, c in scored:
        pg = c.get("page", 0)
        if pg and pg in seen_pages:
            continue
        if pg:
            seen_pages.add(pg)
        deduped.append((s, c))
    return deduped


def retrieve(query: str, k: int = 5, dedupe_page: bool = True) -> list[dict]:
    """向量化查询，返回 top-k 相似片段。
    dedupe_page=True 时按页去重：同一页只保留最相关的一块，让 top-k 覆盖更多不同页——
    大文档（如多页教材）前几页的多个块常因相似度偏高挤占名额，导致后面页检索不到，
    按页去重后能均匀覆盖全文各页。"""
    store = _load_store()
    if not store:
        return []
    q_vec = embed_texts([query])[0]
    scored = [(_cosine(q_vec, c["embedding"]), c) for c in store]
    scored.sort(key=lambda x: x[0], reverse=True)
    if dedupe_page:
        scored = _dedupe_by_page(scored)
    return [
        {"text": c["text"], "doc_name": c["doc_name"], "score": round(s, 4), "page": c.get("page", 0)}
        for s, c in scored[:k]
    ]


# ==================== 流式聊天（DeepSeek 优先、千问回退）====================
async def _stream_chat(messages: list, max_tokens: int = 1024):
    channels: list[tuple] = []
    if os.environ.get("DEEPSEEK_API_KEY"):
        channels.append(
            ("deepseek", os.environ["DEEPSEEK_API_KEY"], "https://api.deepseek.com", "deepseek-chat")
        )
    if DASHSCOPE_API_KEY:
        channels.append(
            ("qwen", DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, DASHSCOPE_CHAT_MODEL)
        )

    emitted = False
    for name, key, base, model in channels:
        try:
            client = AsyncOpenAI(api_key=key, base_url=base)
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    emitted = True
                    yield chunk.choices[0].delta.content
            if emitted:
                return
        except Exception as e:
            if emitted:
                logger.info(f"[Dfrog] RAG {name} 流式中断（已输出部分）: {e}")
                return
            logger.info(f"[Dfrog] RAG {name} 调用失败，切换下一通道: {e}")

    if not emitted:
        yield "（模型调用失败了，稍后再试试）"


def _format_context(docs: list[dict]) -> str:
    return "\n\n".join(f"[片段{i + 1}]\n{d['text']}" for i, d in enumerate(docs))


async def answer_query(question: str):
    """RAG 问答：检索上下文 → 流式回答。async generator。"""
    docs = await asyncio.to_thread(retrieve, question, 10)
    if not docs:
        yield "还没有上传文档哦，先点上面的 📄 上传一个文档，我才能帮你回答～"
        return
    context = _format_context(docs)
    msgs = [
        {"role": "system", "content": RAG_ANSWER_SYSTEM.format(context=context)},
        {"role": "user", "content": question},
    ]
    async for delta in _stream_chat(msgs):
        yield delta


async def generate_quiz(prompt: str):
    """根据文档出题：检索更多上下文 → 流式生成题目。async generator。"""
    docs = await asyncio.to_thread(retrieve, prompt, 15)
    if not docs:
        yield "还没有上传文档哦，先点上面的 📄 上传一个文档，我才能帮你出题～"
        return
    context = _format_context(docs)
    msgs = [
        {"role": "system", "content": RAG_QUIZ_SYSTEM.format(context=context)},
        {"role": "user", "content": prompt.strip() or "请根据文档出 5 道题"},
    ]
    async for delta in _stream_chat(msgs):
        yield delta
