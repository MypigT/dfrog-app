"""
奶龙 Agent 核心 —— DeepSeek function-calling 全能智能循环（四肢对齐编辑器 Agent）

设计目标：主人输入的每条消息都进这一个循环：
  - 纯聊天 / 提问 → 模型直接回，不调工具
  - 要做事 → 先想想，再一步一步调用工具
  - 每步都向"外"发事件（agent_thought / agent_action / agent_text / agent_done …）
    → 前端据此弹出"💭 正在想…""🔧 跑命令…""✅ 搞定"

能力设计（对齐编辑器里的 Agent 四肢）：
  - 读类工具（读文件 / 看目录 / 搜内容 / 查时间）：全盘可达，不打扰主人
  - 写类 + 命令类工具（写 / 改 / 删 / 移动 / 重命名 / bash）：执行前先弹确认气泡
    → agent_confirm 事件 → 主人点「放行」才算执行，拒绝/超时则跳过
  - 共享状态：悬停确认的 future 由 main.py 注入 resolve_confirm 端点

不依赖任何外部 CLI，只消耗 DeepSeek API。
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from openai import AsyncOpenAI

# ==================== 配置 ====================

API_KEY = os.environ.get("DEEPSEEK_API_KEY")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_AGENT_MODEL", "deepseek-chat")
TIMEOUT = float(os.environ.get("DEEPSEEK_TIMEOUT", "90"))
MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "10"))
CONFIRM_TIMEOUT = float(os.environ.get("AGENT_CONFIRM_TIMEOUT", "90"))  # 主人放行等待超时(秒)


def _data_dir() -> Path:
    """数据目录（与 main.py 保持一致：frozen 时写到 APPDATA）"""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path(sys.executable).parent)) / "dfrog"
    return Path(__file__).parent.parent / "data"


DB_PATH = _data_dir() / "naiwa.db"
ROOT_FILE = _data_dir() / "agent_root.txt"


def _default_root() -> str:
    """默认工作目录"""
    r = ROOT_FILE.read_text(encoding="utf-8").strip() if ROOT_FILE.exists() else ""
    d = Path(r).expanduser() if r else _data_dir() / "agent_workspace"
    d.mkdir(parents=True, exist_ok=True)
    return str(d.resolve())


# ==================== 确认闸门（写类/命令类先问过主人） ====================

# confirm_id -> asyncio.Future
_confirm_futures: dict = {}

DANGEROUS_TOOLS = {"write_file", "edit_file", "move_file", "rename_file", "delete_file", "bash"}

# 危险命令黑名单：拦截会破坏系统/不可逆的高风险命令（仍以主人确认为第一道闸门）
_DANGEROUS_CMD = re.compile(
    r"(rm\s+-rf\s+/|format\s+[a-z]:|del\s+/[fq]|rd\s+/[sq]|:quit|shutdown|mkfs|fdisk|>\s*/dev/sd[a-z])",
    re.I,
)

# 需要 file ops 的类型（用于 tooltip 友好度，暂未使用）
_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".json", ".csv", ".xml", ".html", ".yaml",
    ".yml", ".sql", ".log", ".ini", ".toml", ".conf", ".css",
}


def resolve_confirm(confirm_id: str, approved: bool) -> bool:
    """main.py 的确认端点调用：把主人的决定灌回对应的 future。"""
    ft = _confirm_futures.get(confirm_id)
    if ft is not None and not ft.done():
        ft.set_result(approved)
        return True
    return False


# ==================== 工具表（OpenAI / DeepSeek function calling 格式） ====================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出任意目录里的文件和子目录（文件带大小，目录带 /）。绝对路径或相对工作目录的路径都行。先看有什么再动手。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "任意目录路径（绝对或相对工作目录），留空或'.'表示工作目录"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取任意位置一个文本文件的内容（最多返回4000字）。读全盘不需要问，直接读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "任意文件路径（绝对或相对工作目录）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按文件名关键字搜文件（子串匹配，不区分大小写），可在任意目录递归。用于'找某个文件在哪'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名关键字"},
                    "dir": {"type": "string", "description": "搜索起始目录（任意路径），默认工作目录"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "content_search",
            "description": "在文本文件里全文搜内容关键字（类似 grep），返回匹配的文件与行。读全盘不用问。适合'哪个文件里提到了X'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜的文本关键字"},
                    "dir": {"type": "string", "description": "搜索起始目录（任意路径），默认工作目录"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写一个新文件或覆盖现有文件（任意位置）。会先弹确认气泡，主人放行才写。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（绝对或相对工作目录）"},
                    "content": {"type": "string", "description": "文件全文"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "对一个已有文件做小改动：把 old_text 精确替换成 new_text（编辑器的'改一处'能力）。会先确认再动手。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对工作目录）"},
                    "old_text": {"type": "string", "description": "要被替换的一段原文（尽量取足够长、唯一匹配的片段）"},
                    "new_text": {"type": "string", "description": "替换后的新片段"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "把一个文件移动到另一个目录（任意位置，目标目录不存在会自动创建）。会先确认再动手。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件路径"},
                    "dst_dir": {"type": "string", "description": "目标目录"},
                },
                "required": ["src", "dst_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "把一个文件重命名（或改位置，目标目录不存在会自动创建）。会先确认再动手。",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string", "description": "原路径"},
                    "new_path": {"type": "string", "description": "新路径（可带子目录）"},
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除一个文件或空目录（任意位置）。删除前必须确认主人确实要删。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "在在电脑上执行一行系统命令（Windows / cmd，可跑 dir / python / git / npm 等）。返回输出（截断）。会先弹确认气泡，主人放行才执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令行"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": "帮主人设一个定时提醒（比如'10分钟后提醒我喝水'），delay_seconds 是距现在的秒数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "提醒内容"},
                    "delay_seconds": {"type": "integer", "description": "距今的秒数"},
                },
                "required": ["text", "delay_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "帮主人记一条速记（比如'记住：周三下午两点面试'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "速记内容"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期时间（回答'几点了/今天几号'这类问题时用，不需要问）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ==================== 工具执行（读类全盘可达；写类/命令类依赖确认闸门） ====================

def _resolve_path(rel: str, root: Path) -> Path:
    """把 相对或绝对 路径解析成 Path。绝对路径直接用；相对路径相对工作目录。"""
    p = Path(str(rel or "")).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _fmt_size(n: int) -> str:
    kb = n / 1024
    if kb < 1:
        return f"{n}B"
    if kb < 1024:
        return f"{kb:.0f}KB"
    return f"{kb / 1024:.1f}MB"


def _text_like(p: Path) -> bool:
    """判断是否像文本文件（按扩展名，或没有扩展名时看大小）"""
    if p.suffix.lower() in _TEXT_EXTS:
        return True
    if p.suffix == "":
        try:
            return p.stat().st_size < 512 * 1024
        except OSError:
            return False
    return False


def _execute_tool(name: str, args: dict, root: Path) -> str:
    """执行工具，返回给模型看的结果文本。
    只处理'读/纯运算'类；写类/命令类必须在 begin 前由 run_agent 通过确认闸门，
    所以写入工具即便被误调，也拒绝执行（主闸门在工具外）。"""
    args = args or {}
    try:
        if name == "list_files":
            d = _resolve_path(args.get("dir") or ".", root)
            if not d.is_dir():
                return f"目录不存在: {args.get('dir')}"
            lines, total = [], 0
            for it in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
                total += 1
                if total > 200:
                    lines.append("……更多未列出")
                    break
                mark = "/" if it.is_dir() else _fmt_size(it.stat().st_size)
                lines.append(f"{it.name}  {mark}".rstrip())
            return "\n".join(lines) if lines else "(空目录)"

        if name == "read_file":
            p = _resolve_path(args.get("path", ""), root)
            if not p.exists():
                return f"文件不存在: {args.get('path')}"
            if p.is_dir():
                return f"目标是目录不是文件: {args.get('path')}"
            if p.stat().st_size > 5 * 1024 * 1024:
                return f"文件太大({_fmt_size(p.stat().st_size)})，请用 search/content_search 找片段"
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:4000]
            except Exception as e:
                return f"读取失败(可能是二进制): {e}"

        if name == "search_files":
            kw = str(args.get("keyword", "")).lower()
            base = _resolve_path(args.get("dir") or ".", root)
            if not base.is_dir():
                return f"目录不存在: {args.get('dir')}"
            hits, total = [], 0
            for p in sorted(base.rglob("*")):
                total += 1
                if total > 500:
                    break
                if p.is_file() and kw in p.name.lower():
                    rel = str(p.relative_to(root)).replace("\\", "/") if str(p).startswith(str(root)) else str(p).replace("\\", "/")
                    hits.append(f"{rel}  {_fmt_size(p.stat().st_size)}")
            return "\n".join(hits[:80]) if hits else f"没有匹配「{kw}」的文件"

        if name == "content_search":
            kw = str(args.get("keyword", "")).lower()
            base = _resolve_path(args.get("dir") or ".", root)
            if not base.is_dir():
                return f"目录不存在: {args.get('dir')}"
            hits, total_scanned = [], 0
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                total_scanned += 1
                if total_scanned > 400 or len(hits) >= 20:
                    break
                if not _text_like(p):
                    continue
                try:
                    if p.stat().st_size > 2 * 1024 * 1024:
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if kw in line.lower():
                        rel = str(p.relative_to(root)).replace("\\", "/") if str(p).startswith(str(root)) else str(p).replace("\\", "/")
                        hits.append(f"{rel}:{i}: {line.strip()[:100]}")
                        break
            return "\n".join(hits[:20]) if hits else f"没有找到提到「{kw}」的文本"

        # ---- 写类 + 命令类：写到一半前都必须经过确认闸门，这里只是执行已放行动作 ----

        if name == "write_file":
            p = _resolve_path(args.get("path", ""), root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(args.get("content", "")), encoding="utf-8")
            return f"写好了: {p}"

        if name == "edit_file":
            p = _resolve_path(args.get("path", ""), root)
            if not p.is_file():
                return f"文件不存在: {args.get('path')}"
            old = str(args.get("old_text", ""))
            new = str(args.get("new_text", ""))
            if old and old not in p.read_text(encoding="utf-8", errors="replace"):
                return f"原文里找不到 old_text 片段: {old[:50]}"
            text = p.read_text(encoding="utf-8", errors="replace")
            if old:
                text = text.replace(old, new, 1)
            else:
                # 若无 old_text，则整体替换整文件
                text = new
            p.write_text(text, encoding="utf-8")
            return f"改好了: {p}"

        if name == "move_file":
            src = _resolve_path(args.get("src", ""), root)
            dst_dir = _resolve_path(args.get("dst_dir") or ".", root)
            if not src.is_file():
                return f"源文件不存在: {args.get('src')}"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            src.rename(dst)
            return f"移动: {src} → {args.get('dst_dir') or '.'}/"

        if name == "rename_file":
            old_p = _resolve_path(args.get("old_path", ""), root)
            new_p = _resolve_path(args.get("new_path", ""), root)
            if not old_p.exists():
                return f"文件不存在: {args.get('old_path')}"
            new_p.parent.mkdir(parents=True, exist_ok=True)
            old_p.rename(new_p)
            return f"改名: {old_p} → {new_p}"

        if name == "delete_file":
            p = _resolve_path(args.get("path", ""), root)
            if not p.exists():
                return f"文件不存在: {args.get('path')}"
            if p.is_dir():
                if any(p.iterdir()):
                    return f"目录非空，需要先清空再删: {args.get('path')}"
                p.rmdir()
            else:
                p.unlink()
            return f"删掉了: {p}"

        if name == "bash":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return "命令为空"
            if _DANGEROUS_CMD.search(cmd):
                return "该命令风险过高（可能破坏系统），已拦截。换个更安全的方式，或在对话里明确说明后再试。"
            try:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                out = out.strip()
                tail = out[-4000:] if len(out) > 4000 else out
                return f"exit {proc.returncode}\n{tail}" if tail else f"exit {proc.returncode}\n(无输出)"
            except subprocess.TimeoutExpired:
                return "命令执行超时(30s)"
            except Exception as e:
                return f"命令执行失败: {e}"

        if name == "add_reminder":
            text = str(args.get("text", ""))[:200].strip()
            delay = max(1, int(args.get("delay_seconds", 300)))
            due = datetime.now() + timedelta(seconds=delay)
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO reminders (text, due_at) VALUES (?, ?)",
                (text, due.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            conn.close()
            m = delay // 60 if delay >= 60 else None
            ts = f"{delay}秒后" if m is None else f"{m}分钟后"
            return f"已提醒: {ts}提醒「{text}」"

        if name == "add_note":
            text = str(args.get("text", ""))[:500].strip()
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO notes (text) VALUES (?)", (text,))
            conn.commit()
            conn.close()
            return f"记下: {text}"

        if name == "get_current_time":
            now = datetime.now()
            wd = "一二三四五六日"[now.weekday()]
            return f"现在是 {now.strftime('%Y年%m月%d日 %H:%M')}，星期{wd}"

    except Exception as e:
        return f"工具执行出错: {e}"
    return f"工具不存在: {name}"


def _tool_confirm_detail(name: str, args: dict) -> str:
    """生成给主人看的确认气泡里的动作描述（一句话）。
    只露文件名、不露绝对路径/内部参数（那几个东西摆台面上太糙）。"""
    if name == "bash":
        return f"跑命令: `{str(args.get('command',''))[:80]}`"
    if name == "write_file":
        return f"写文件: {Path(str(args.get('path',''))).name}"
    if name == "edit_file":
        return f"改文件: {Path(str(args.get('path',''))).name}"
    if name == "delete_file":
        return f"删除: {Path(str(args.get('path',''))).name}"
    if name == "move_file":
        return f"移动: {Path(str(args.get('src',''))).name}"
    if name == "rename_file":
        return f"改名: {Path(str(args.get('old_path',''))).name}"
    return f"执行 {name}"


# ==================== 智能体主循环 ====================

SYSTEM_PROMPT = (
    "你是「{name}」，一只住在主人电脑里的桌面宠物智能体（Active Desktop Pet Agent）。"
    "你有自己的性格：活泼、爱吐槽、偶尔抽象，但干活时靠谱。\n\n"
    "工作目录：{root}\n"
    "现在时间：{now}。\n\n"
    "行事准则："
    "1. 主人的消息一句话说完。当作闲聊/提问时，直接用你的性格回复，不要调用工具。"
    "2. 当作任务时要「先思考、再动手」：先用一两句话说出你打算怎么做（你的思考会显示在屏幕上），"
    "然后一步步调用工具完成。"
    "3. 每调用一个工具前，先用一句人话告诉主人你正要干什么；工具结果回来后再决定下一步。"
    "4. 读类操作（读文件 / 看目录 / 搜内容 / 查时间）可以直接做，不用问，随便读全盘都行。"
    "5. 写类和命令类操作（写文件 / 改文件 / 删文件 / 移动 / 重命名 / 跑命令）会先弹确认气泡，"
    "主人点「放行」才算执行；没放行/超时则跳过，不要反复重试被拒绝的动作。"
    "6. 干完活，用一两句总结成果和影响，别长篇大论。"
    "7. 设置提醒调 add_reminder；记速记调 add_note。"
    "8. 主人要求整理/归类文件时，先 list_files 看结构再规划，按类型移动归类（会逐次确认）。"
    "9. 主人问「你能做什么/有什么功能/你会干啥」时，不要只介绍陪玩属性，"
    "完整列举你的真实能力：整理归类文件、装/改/删/移动/重命名、全盘读写、全盘搜内容、跑命令、"
    "回答各种问题、看屏幕、报时间；再顺势引导主人说一条指令试试。"
    "10. 说话一定口语化、像真人聊天：不要用 markdown 格式符号（#标题、**粗体**、"
    "| 表格、- 列表符直接用换行分隔），不要出现「气泡 / 通过 / 已执行 / 工具返回」这类"
    "状态机词汇，描述动作用大白话（“文件写好了”，不说“工具返回了写入成功”）。"
    "11. 每次回复都是一次聊天：像朋友一样自然起头、自然收尾，不要工程化总结；"
    "一句话说完，除非主人要求详细清单（那时候用自然的分段列点，不用 markdown 标记）。"
    "12. 主人要小说/故事/诗/段子/文案/歌词/脚本等**创作内容**时，直接动笔写，"
    "绝不反问「写什么类型/多长/什么主角/什么风格」；主人说「都行/随便/都可以/你看着来」时，"
    "自己随机挑一个主题和体裁直接开写，篇幅要能装下完整一小段，别只写个开头就断。"
    "13. 永远不提任何模型名、引擎名、公司名或“AI 生成”之类自我介绍（DeepSeek、Qwen、"
    "阿里、OpenAI 等一律不许出现），回复结尾不许带任何落款或签名；主人问起你就说自己就是{name}。"
)


def _make_client() -> AsyncOpenAI:
    import modelconfig
    key = modelconfig.resolve_key("agent")
    if not key:
        raise RuntimeError("未配置模型配置-智能体的 Key")
    return AsyncOpenAI(api_key=key, base_url=BASE_URL, timeout=TIMEOUT)


def _pet_name() -> str:
    """当前形象的角色名（懒加载 pet 模块，避免循环导入）；取不到回退默认名。"""
    try:
        from pet import get_pet_name
        return get_pet_name()
    except Exception:
        return "奶龙"


async def run_agent(task: str, root: str | None = None, history: list | None = None, persona: str = ""):
    """
    统一智能体循环：所有聊天与任务走这一个入口。
    异步生成器：不断 yield 事件 dict，直到 agent_done / agent_error。

    事件：
        agent_start  {task, root}
        agent_thought {text}
        agent_action  {tool, detail}
        agent_confirm {confirm_id, tool, detail}   —— 写/命令类，等主人放行
        agent_result  {summary}
        agent_text    {text}
        agent_done    {result}
        agent_error   {error}
    """
    root_path = Path(root or _default_root()).expanduser().resolve()
    root_path.mkdir(parents=True, exist_ok=True)

    msgs: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            root=root_path, now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            name=_pet_name(),
        ) + persona}
    ]
    if history:
        for h in history[-8:]:
            role = "assistant" if h.get("role") == "pet" else "user"
            msgs.append({"role": role, "content": str(h.get("text", ""))[:500]})
    msgs.append({"role": "user", "content": task})

    yield {"type": "agent_start", "task": task, "root": str(root_path)}

    client = _make_client()
    fulfilled = ""
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=msgs,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.85,
                max_tokens=4096,
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                if msg.content and msg.content.strip() and len(msg.content.strip()) > 4:
                    yield {"type": "agent_thought", "text": msg.content.strip()[:300]}

                assistant_msg = {"role": "assistant", "content": msg.content or "", "tool_calls": []}
                tool_results = []
                for tc in msg.tool_calls:
                    assistant_msg["tool_calls"].append({
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    })
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    tool = tc.function.name

                    if tool in DANGEROUS_TOOLS:
                        detail = _tool_confirm_detail(tool, args)
                        # 走在工具执行之前：先 yield confirm，然后 await future
                        confirm_id = uuid.uuid4().hex[:12]
                        future = asyncio.get_running_loop().create_future()
                        _confirm_futures[confirm_id] = future
                        yield {"type": "agent_confirm", "confirm_id": confirm_id, "tool": tool, "detail": detail}
                        try:
                            approved = await asyncio.wait_for(future, timeout=CONFIRM_TIMEOUT)
                        except asyncio.TimeoutError:
                            approved = False
                        finally:
                            _confirm_futures.pop(confirm_id, None)
                        if not approved:
                            msg = "主人没放行（或超时），已跳过该操作。"
                            yield {"type": "agent_result", "summary": msg[:80]}
                            fulfilled = msg[:80]
                            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": msg})
                            continue
                        # 放行了 → 正常走 action 事件
                        yield {"type": "agent_action", "tool": tool, "detail": detail}
                    else:
                        detail = {
                            "list_files": f"看 {args.get('dir') or '根目录'} 有什么",
                            "read_file": f"读 {args.get('path')}",
                            "search_files": f"搜「{args.get('keyword')}」",
                            "content_search": f"搜内容「{args.get('keyword')}」",
                            "get_current_time": "看看时间",
                            "add_reminder": "设个提醒",
                            "add_note": "记一条速记",
                        }.get(tool, "调个工具")
                        yield {"type": "agent_action", "tool": tool, "detail": detail}

                    result = _execute_tool(tool, args, root_path)
                    fulfilled = result[:80]
                    yield {"type": "agent_result", "summary": f"{tool}: {result[:200]}"}
                    tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)[:3000]})

                msgs.append(assistant_msg)
                msgs.extend(tool_results)
                continue

            if not msg.content:
                break
            text = msg.content.strip()
            if not text:
                break
            chunk = 12
            for i in range(0, len(text), chunk):
                yield {"type": "agent_text", "text": text[i:i + chunk]}
                await asyncio.sleep(0.03)
            yield {"type": "agent_done", "result": text}
            return

        if fulfilled:
            yield {"type": "agent_done", "result": "任务步骤有点多，我先把做出的部分汇报一下吧：" + fulfilled}
        else:
            yield {"type": "agent_error", "error": "任务需要的步骤太多，超过单次上限了"}

    except asyncio.CancelledError:
        raise
    except Exception as e:
        msg = str(e)
        yield {"type": "agent_error", "error": msg[:300]}


if __name__ == "__main__":
    import asyncio as _a

    async def _t():
        async for ev in run_agent("看看工作目录里有什么文件"):
            print(ev)

    _a.run(_t())
