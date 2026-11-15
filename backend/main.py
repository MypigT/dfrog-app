"""
Dfrog 后端入口
FastAPI 应用 + SQLite + 真实 DeepSeek LLM 对话 + 看屏幕(qwen3.8-max) + 主动互动(SSE)
"""

import asyncio
import contextlib
import io
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

load_dotenv()  # 读 backend/.env 或上层目录 .env（DEEPSEEK_API_KEY / DASHSCOPE_API_KEY）

from vision import DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, capture_base64, capture_screen, look_at_screen, stream_look_at_screen
from agent import run_agent, resolve_confirm, _execute_tool, _default_root
from rag import (
    add_document,
    answer_query,
    delete_document,
    generate_quiz,
    list_documents,
)
from pet import (
    create_avatar,
    generate_animations,
    list_avatars,
    activate_avatar,
    rename_avatar,
    set_avatar_meta,
    delete_avatar,
    get_current,
    get_pet_name,
    get_avatar,
    avatar_dir,
    add_action,
    delete_action,
    rename_action,
    move_action,
    save_folder_upload,
    save_single_upload,
)
from PIL import Image
import modelconfig

logger = logging.getLogger("dfrog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DASHSCOPE_CHAT_MODEL = os.environ.get("DASHSCOPE_CHAT_MODEL", "qwen3.8-max-0902")

# 视觉意图关键词：用户问"能不能看见/在干嘛/屏幕是什么"这类问题时，
# 聊天自动带上截图走多模态（DeepSeek 只能读文字，看不了图，直接走 Qwen）
VISION_INTENT = re.compile(r"看见|看到|屏幕|截图|画面|你在看|看什么|在干嘛|在干啥|干嘛呢|干啥呢")

# 项目根目录
# 打包成 exe 后（PyInstaller frozen），数据写到用户目录，避免 Program Files 不可写
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", Path(sys.executable).parent)) / "dfrog"
else:
    BASE_DIR = Path(__file__).parent.parent
    DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "naiwa.db"


# ==================== 设置（开机自启/静音/流式/主动互动/置顶，持久化 data/settings.json）====================
SETTINGS_PATH = DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "auto_start": False,
    "mute": False,
    "stream_output": True,
    "proactive": True,
    "always_on_top": True,
}


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
    except Exception:
        pass
    return dict(DEFAULT_SETTINGS)


def _save_settings(s: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_setting(key, default=None):
    return _load_settings().get(key, default)


def _autostart_lnk_path() -> str:
    startup = os.path.join(
        os.environ.get("APPDATA", str(Path.home())),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )
    return os.path.join(startup, "dfrog-autostart.lnk")


def set_autostart(on: bool) -> bool:
    """注册/取消 Windows 开机自启（启动目录放快捷方式，避免控制台闪屏）"""
    lnk = _autostart_lnk_path()
    if on:
        try:
            if getattr(sys, "frozen", False):
                target = sys.executable
                args_str = ""
                workdir = os.path.dirname(sys.executable)
            else:
                root = Path(__file__).parent.parent
                py = root / ".venv" / "Scripts" / "python.exe"
                launcher = root / "launcher.py"
                target = str(py if py.exists() else "python")
                args_str = f'"{launcher}"'  # 路径可能含空格，带引号
                workdir = str(root)
            # 用单引号包裹值，避免内层双引号冲突
            ps = (
                f"$ws=New-Object -ComObject WScript.Shell; "
                f"$sc=$ws.CreateShortcut('{lnk}'); "
                f"$sc.TargetPath='{target}'; "
                f"$sc.Arguments='{args_str}'; "
                f"$sc.WorkingDirectory='{workdir}'; "
                f"$sc.Save()"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=20)
            err = r.stderr.decode("utf-8", "ignore").strip()[:200] if r.stderr else ""
            if r.returncode != 0 or err:
                logger.info(f"[Dfrog] 开机自启 PowerShell 报错: {err}")
            return r.returncode == 0
        except Exception as e:
            logger.info(f"[Dfrog] 设置开机自启失败: {e}")
            return False
    try:
        if os.path.exists(lnk):
            os.remove(lnk)
        return True
    except Exception as e:
        logger.info(f"[Dfrog] 取消开机自启失败: {e}")
        return False


async def _stream_or_buffer(gen):
    """流式输出开关：关时把整段文本攒齐一次性吐出（仍是流，只是单块）"""
    if _get_setting("stream_output", True):
        async for x in gen:
            yield x
    else:
        buf = []
        async for x in gen:
            buf.append(x)
        if buf:
            yield "".join(buf)


@asynccontextmanager
async def lifespan(app):
    """启动时初始化数据库 + 拉起主动互动定时器；关闭时取消定时器"""
    init_db()
    logger.info(f"[Dfrog] 后端启动，数据库: {DB_PATH}")
    logger.info(f"[Dfrog] DeepSeek API Key: {'已配置' if os.environ.get('DEEPSEEK_API_KEY') else '未配置'}")
    logger.info(f"[Dfrog] 视觉 Key: {'已配置(' + DASHSCOPE_CHAT_MODEL + ')' if DASHSCOPE_API_KEY else '未配置，看屏幕走图像分析降级'}")
    # 开机自启自检：把启动目录的快捷方式与 auto_start 设置对齐。
    # 防止上次切「关」时删除失败留下残留 .lnk（设置是关却仍自启）。
    try:
        await asyncio.to_thread(set_autostart, _get_setting("auto_start", False))
    except Exception as e:
        logger.info(f"[Dfrog] 开机自启自检出错: {e}")
    task = asyncio.create_task(proactive_loop())
    reminder_task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    reminder_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    with contextlib.suppress(asyncio.CancelledError):
        await reminder_task


app = FastAPI(title="Dfrog API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 全局状态 ====================
# 同一时刻只允许一个 Agent 任务在跑
agent_running = False
# Agent 默认工作目录（聊天口令派活用），启动时从 agent_root.txt 恢复
agent_default_root: str | None = None


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS status (
            id INTEGER PRIMARY KEY DEFAULT 1,
            chaos INTEGER DEFAULT 50,
            triggered INTEGER DEFAULT 50,
            presence INTEGER DEFAULT 50,
            abstraction INTEGER DEFAULT 50,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("INSERT OR IGNORE INTO status (id) VALUES (1)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            done INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def get_status():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT chaos, triggered, presence, abstraction, updated_at FROM status WHERE id=1"
    ).fetchone()
    conn.close()
    if row:
        return {
            "chaos": row[0],
            "triggered": row[1],
            "presence": row[2],
            "abstraction": row[3],
            "updated_at": row[4],
        }
    return {"chaos": 50, "triggered": 50, "presence": 50, "abstraction": 50}


def update_status(changes: dict):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT chaos, triggered, presence, abstraction FROM status WHERE id=1"
    ).fetchone()
    chaos = max(0, min(100, row[0] + changes.get("chaos", 0)))
    triggered = max(0, min(100, row[1] + changes.get("triggered", 0)))
    presence = max(0, min(100, row[2] + changes.get("presence", 0)))
    abstraction = max(0, min(100, row[3] + changes.get("abstraction", 0)))
    conn.execute(
        "UPDATE status SET chaos=?, triggered=?, presence=?, abstraction=?, updated_at=datetime('now') WHERE id=1",
        (chaos, triggered, presence, abstraction),
    )
    conn.commit()
    conn.close()
    return {
        "chaos": chaos,
        "triggered": triggered,
        "presence": presence,
        "abstraction": abstraction,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "dfrog-backend"}


@app.get("/status")
def get_status_api():
    return get_status()


class StatusChanges(BaseModel):
    chaos: int = 0
    triggered: int = 0
    presence: int = 0
    abstraction: int = 0


@app.post("/status")
def update_status_api(changes: StatusChanges):
    return update_status(changes.model_dump())


class ActionRequest(BaseModel):
    action: str


# 动作 → 场景提示词：戳/长按/拖/喂食时，把场景发给 LLM 生成奶龙反应（不用固定台词）
ACTION_PROMPTS = {
    "poke": "主人戳了你一下",
    "long_press": "主人一直按着你不放",
    "drag": "主人拖着你到处走",
    "feed": "主人喂了你一口吃的",
}

# 动作对性格数值的影响（玩法数值，与台词无关）
ACTION_STATUS = {
    "poke": {"chaos": 5, "presence": 5},
    "long_press": {"triggered": 5, "presence": 3},
    "drag": {"presence": 2},
    "feed": {"presence": 10, "chaos": 3},
}


@app.post("/action")
async def do_action(req: ActionRequest):
    """动作互动：场景提示词发给 LLM，流式生成奶龙式反应"""
    prompt = ACTION_PROMPTS.get(req.action, ACTION_PROMPTS["poke"])
    update_status(ACTION_STATUS.get(req.action, ACTION_STATUS["poke"]))
    return StreamingResponse(_stream_or_buffer(stream_naiwa_reply(prompt)), media_type="text/plain")


# ==================== 聊天接口 ====================

class ChatRequest(BaseModel):
    message: str
    messages: list[dict] | None = None


SYSTEM_PROMPT = """你是"{name}"，一只住在主人电脑桌面上的桌宠。

你和主人的关系：
- 主人是你的饲养员，也是你的好朋友、聊天搭子
- 主人戳你、拖你、喂你，都是在和你玩，用{name}的方式回应

性格：
- 活泼、爱吐槽、偶尔抽象，但就是个爱聊天的普通朋友

说话风格（重要）：
- 像熟人聊天一样自然，句子长短随内容，不要有模板腔
- 不要每句都"哈哈"，禁止固定的开场和收尾（"收到""搞定啦"这类）
- 用中文，正常说话，该长就长，别总一句话敷衍
- 不要工程化总结、不要汇报式罗列，除非主人明确要列表

要求：
- 保持{name}身份，不要跳出角色
- 不知道就说不知道，别编造；看到什么就说什么
"""


def _system_prompt() -> str:
    """系统提示词：角色名跟随当前激活形象，没设则回退默认名。"""
    return SYSTEM_PROMPT.format(name=get_pet_name())


def _avatar_persona_suffix() -> str:
    """当前激活形象的角色名+提示词，拼到系统提示词后塑造对话人设"""
    cur = get_current()
    if not cur:
        return ""
    role = (cur.get("role_name") or "").strip()
    prompt = (cur.get("prompt") or "").strip()
    if not role and not prompt:
        return ""
    parts = []
    if role:
        parts.append(f"你的角色名是「{role}」，对话时以这个身份自居、保持这个角色。")
    if prompt:
        parts.append(prompt)
    return "\n\n" + "\n".join(parts)


def _now_str() -> str:
    """当前时间字符串（喂给 LLM，让它能回答"几点了"这类问题）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M %A")


def _call_llm(client_kwargs: dict, model: str, msgs: list) -> str:
    """通用 LLM 调用（OpenAI 兼容协议）"""
    from openai import OpenAI

    client = OpenAI(**client_kwargs)
    resp = client.chat.completions.create(
        model=model,
        messages=msgs,
        max_tokens=300,
        temperature=1.3,
    )
    return resp.choices[0].message.content.strip()


def llm_naiwa_reply_with_vision(message: str, history=None) -> str:
    """带截图的多模态聊天（视觉模型 Qwen）：抓屏 + 用户问题一起发给模型。
    聊天用 960px 小图（旗舰视觉模型推理慢，缩小能明显提速）"""
    img = capture_screen(max_width=960)
    img_b64 = capture_base64(img)
    persona = _avatar_persona_suffix()
    msgs = [{
        "role": "system",
        "content": _system_prompt() + persona + "\n\n你现在能看到主人的屏幕截图，请结合截图真实内容回答；看不清就直说看不清，不要瞎猜主人在干嘛。",
    }]
    if history:
        msgs.extend(history)
    msgs.append({
        "role": "user",
        "content": [
            {"type": "text", "text": message},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ],
    })
    return _call_llm(
        {"api_key": modelconfig.resolve_key("vision"), "base_url": DASHSCOPE_BASE_URL},
        DASHSCOPE_CHAT_MODEL,
        msgs,
    )


def llm_naiwa_reply(message: str, history=None) -> tuple[str, str]:
    """
    生成奶龙回复，返回 (回复, 引擎标记)。
    双通道：优先 DeepSeek；503/超时等失败自动切百炼 Qwen（视觉同款，一把 Key）；
    两边都挂才返回兜底文案。
    """
    # 视觉意图：问"能不能看见/在干嘛" → 带截图走多模态（DeepSeek 看不了图，直接 Qwen）
    if VISION_INTENT.search(message) and DASHSCOPE_API_KEY:
        try:
            reply = llm_naiwa_reply_with_vision(message, history)
            return reply, "vision-chat"
        except Exception as e:
            logger.info(f"[Dfrog] 视觉聊天失败，退回文字通道: {e}")

    persona = _avatar_persona_suffix()
    msgs = [{"role": "system", "content": _system_prompt() + persona + f"\n\n当前时间: {_now_str()}"}]
    if history:
        msgs.extend(history)
    else:
        msgs.append({"role": "user", "content": message})

    # 1) DeepSeek（主通道）
    ds_key = modelconfig.resolve_key("chat")
    if ds_key:
        try:
            reply = _call_llm(
                {"api_key": ds_key, "base_url": "https://api.deepseek.com"},
                "deepseek-chat",
                msgs,
            )
            return reply, "deepseek"
        except Exception as e:
            logger.info(f"[Dfrog] DeepSeek 调用失败，切换百炼: {e}")

    # 2) 百炼 Qwen（备用通道，视觉模型同款，本身就是能聊天的大模型）
    qw_key = modelconfig.resolve_key("vision")
    if qw_key:
        try:
            reply = _call_llm(
                {"api_key": qw_key, "base_url": DASHSCOPE_BASE_URL},
                DASHSCOPE_CHAT_MODEL,
                msgs,
            )
            return reply, "qwen"
        except Exception as e:
            logger.info(f"[Dfrog] Qwen 调用失败: {e}")

    return "", "fallback"


async def _stream_channel(api_key: str, base_url: str, model: str, msgs: list):
    """单个 LLM 通道的流式输出，产出文本增量"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    stream = await client.chat.completions.create(
        model=model,
        messages=msgs,
        max_tokens=300,
        temperature=1.3,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def stream_naiwa_reply(message: str, history=None):
    """
    流式生成奶龙回复（async generator，产出文本增量）。
    通道：视觉意图→Qwen 视觉流；DeepSeek 流→失败切 Qwen 流→兜底文案。
    已输出部分内容后中断就不再切通道（避免两段拼一起）。
    """
    persona = _avatar_persona_suffix()
    msgs = [{"role": "system", "content": _system_prompt() + persona + f"\n\n当前时间: {_now_str()}"}]
    if history:
        msgs.extend(history)
    else:
        msgs.append({"role": "user", "content": message})

    # 视觉意图：问"能不能看见/在干嘛" → 带截图走多模态流
    if VISION_INTENT.search(message) and DASHSCOPE_API_KEY:
        emitted = False
        try:
            img = capture_screen(max_width=960)
            img_b64 = capture_base64(img)
            vmsgs = [{
                "role": "system",
                "content": _system_prompt() + persona + "\n\n当前时间: " + _now_str() + "\n你现在能看到主人的屏幕截图，请结合截图内容回答。",
            }]
            if history:
                vmsgs.extend(history)
            vmsgs.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": message},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            })
            async for delta in _stream_channel(DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, DASHSCOPE_CHAT_MODEL, vmsgs):
                emitted = True
                yield delta
            if emitted:
                return
        except Exception as e:
            if emitted:
                logger.info(f"[Dfrog] 视觉流式中断: {e}")
                return
            logger.info(f"[Dfrog] 视觉流式聊天失败，退回文字通道: {e}")

    # 文字通道：DeepSeek 优先，失败切 Qwen
    channels = []
    ds_key = modelconfig.resolve_key("chat")
    if ds_key:
        channels.append(("deepseek", ds_key, "https://api.deepseek.com", "deepseek-chat"))
    qw_key = modelconfig.resolve_key("vision")
    if qw_key:
        channels.append(("qwen", qw_key, DASHSCOPE_BASE_URL, DASHSCOPE_CHAT_MODEL))

    emitted = False
    for name, key, base, model in channels:
        try:
            async for delta in _stream_channel(key, base, model, msgs):
                emitted = True
                yield delta
            if emitted:
                return
        except Exception as e:
            if emitted:
                logger.info(f"[Dfrog] {name} 流式中断（已输出部分内容）: {e}")
                return
            logger.info(f"[Dfrog] {name} 流式调用失败，切换下一通道: {e}")

    if not emitted:
        # 不产出固定兜底话；聊天调度层拿到空回复时由前端显示中性的"没听清"。
        yield ""


# ==================== 统一聊天调度：一个对话框解决大部分问题 ====================
# （调度不再靠独立的意图分类通道 —— 每条消息都进 Agent 循环，
#   由 DeepSeek function-calling 自己决定「光聊」还是「先想再调工具」。）


def _resolve_root_reply(msg: str) -> str | None:
    """把'在哪个文件夹干活'的回答解析成已存在的目录绝对路径；认不出来返回 None"""
    s = msg.strip().strip("'\"")
    try:
        if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith(("/", "~", ".")):
            p = Path(s).expanduser().resolve()
            if p.is_dir():
                return str(p)
    except Exception:
        pass
    low = s.lower()
    home = Path.home()
    for keys, p in (
        (("桌面", "desktop"), home / "Desktop"),
        (("下载", "download"), home / "Downloads"),
        (("文档", "document"), home / "Documents"),
    ):
        if any(k in low for k in keys) and p.is_dir():
            return str(p)
    return None


async def _agent_task_stream(task: str, root: str, history: list | None = None):
    """把 Agent 干活过程流式成奶龙台词：进度 emoji / 最终答复从这条聊天流回对话框。
    同时把事件 publish 到 SSE，桌面 🔧 气泡和面板进度都跟得到"""
    global agent_running
    if agent_running:  # 面板任务正在跑
        yield "⚠ 还有一个任务在跑呢，等它完事我再接活…"
        return
    agent_running = True
    did_tool = False   # 本次对话是否真的调过工具；纯聊天不补"搞定啦"固定收尾
    try:
        async for ev in run_agent(task, root, history, persona=_avatar_persona_suffix()):
            publish_event(ev)  # 桌面 🔧 气泡照旧
            etype = ev.get("type")
            if etype == "agent_start":
                # 不再发固定开场「收到主人～这就看看…」——纯聊天（如问能干嘛）也
                # 会先甩一句很突兀；开场白控制权交给 DeepSeek 自己的 agent_thought /
                # agent_text，这样闲聊就是纯闲聊，不强行"看看…"
                pass
            elif etype == "agent_thought":
                text = str(ev.get("text", "")).strip()
                if text:
                    yield f"🤔 {text[:120]}\n"
            elif etype == "agent_action":
                # 🔧 动作详情只走全局 SSE 弹桌面气泡（5秒消失），已经由 publish_event(ev) 广播过；
                # 不再 yield 进聊天流——对话框只留 🤔想法 / 🙋确认 / ·摘要 / 最终答复，避免怼原始工具名+路径
                did_tool = True
                pass
            elif etype == "agent_confirm":
                did_tool = True
                # 口语化:主人,可以让我{做什么}吗?(文件名/命令名可选附在后面)
                cn = TOOL_CN_MAIN.get(ev.get("tool"), "动一下电脑")
                detail = str(ev.get("detail") or "")
                name = detail.split(": ", 1)[-1] if ": " in detail else ""
                yield f"🙋 主人,可以让我{cn}" + (f"({name})" if name else "") + "吗?\n"
            elif etype == "agent_result":
                summary = str(ev.get("summary", "")).strip()
                # 过滤掉机器味重的原始行（工具名前缀 / Windows 绝对路径）：
                # "· write_file: 写好了: C:\...\confirm_probe.txt" 不适合摆在对话框台面；
                # 带表情的人工摘要（✅ 建好了…）和纯中文说明保留
                if summary and not re.search(r"(_\w+_file|bash|content_search)\b|^[a-z_]+:\s|\w+:\s*C:\\|^\s*\w+:\s*写|删|改", summary):
                    yield f"  · {summary[:80]}\n"
            elif etype == "agent_text":
                yield str(ev.get("text", ""))  # 最终答复：直接透传进气泡
            elif etype == "agent_done":
                # 不再补「✅搞定啦主人～还有事尽管吩咐～」固定收尾——那是复读模板,
                # 每次干活都一模一样;模型 agent_text 自己已经说完了,不再叠一句。
                return
            elif etype == "agent_error":
                yield f"❌ {str(ev.get('error', '出了点岔子'))[:400]}"
                return
    finally:
        agent_running = False


@app.post("/chat")
async def chat(req: ChatRequest):
    """聊天接口：统一调度。闲聊 / 派活干活 / 提醒 / 速记都从这一条流式通道回对话框。
    这里不再做意图分类 —— 视觉意图走视觉老通道，其余一切放进 Agent 循环，
    由 DeepSeek function-calling 自己判断该光聊还是该先想再动手。"""
    # 聊天消耗存在感、增加魔性（前端靠轮询 /status 拿新数值）
    update_status({"presence": 2, "chaos": 1, "abstraction": 1})

    # 前端传来的历史对话，映射成 LLM 格式（role: user/assistant）
    history = None
    if req.messages:
        history = []
        for m in req.messages:
            role = "assistant" if m.get("role") == "pet" else "user"
            history.append({"role": role, "content": m.get("text", "")})

    msg = req.message.strip()

    global agent_default_root

    # ① 视觉意图（问我在看什么这类）：保留经典视觉流（需要 DASHSCOPE_API_KEY）
    if VISION_INTENT.search(msg) and DASHSCOPE_API_KEY:
        return StreamingResponse(
            _stream_or_buffer(stream_naiwa_reply(msg, history)),
            media_type="text/plain",
        )

    # ② 统一入口：其余所有消息都进 Agent 循环。
    #    纯聊 → 模型直接回；要干活 → 先思考、再调工具、逐步反馈。
    #    若主人直接报了目录（C:\... / 桌面 / 下载…）→ 顺手设为默认工作目录。
    if not agent_default_root:
        guess = _resolve_root_reply(msg)
        if guess:
            agent_default_root = guess
            _save_agent_root(guess)

    return StreamingResponse(
        _stream_or_buffer(_agent_task_stream(msg, agent_default_root, history)),
        media_type="text/plain",
    )


# ==================== RAG 文档问答 ====================
class RagAskRequest(BaseModel):
    message: str


class RagDeleteRequest(BaseModel):
    doc_id: str


# ==================== RAG 上传进度（大扫描 PDF 逐页视觉 OCR，前端轮询此状态显示进度）====================
_UPLOAD_PROGRESS = {"stage": "idle", "done": 0, "total": 0, "page": 0}
_UPLOAD_PROGRESS_LOCK = threading.Lock()


def _set_upload_progress(done, total, page):
    with _UPLOAD_PROGRESS_LOCK:
        _UPLOAD_PROGRESS["stage"] = "processing"
        _UPLOAD_PROGRESS["done"] = done
        _UPLOAD_PROGRESS["total"] = total
        _UPLOAD_PROGRESS["page"] = page


def _reset_upload_progress():
    with _UPLOAD_PROGRESS_LOCK:
        _UPLOAD_PROGRESS.update({"stage": "idle", "done": 0, "total": 0, "page": 0})


@app.post("/rag/upload")
async def rag_upload(file: UploadFile = File(...)):
    """上传文档 → 解析 / 切块 / 向量化 → 入库。大扫描 PDF 逐页 OCR，前端轮询 /rag/upload/status 看进度"""
    content = await file.read()
    _reset_upload_progress()
    try:
        # add_document 内部调千问 embedding / 视觉 OCR 是阻塞 HTTP，丢线程池别卡事件循环
        result = await asyncio.to_thread(add_document, file.filename, content, _set_upload_progress)
    except Exception as e:
        _reset_upload_progress()
        return JSONResponse(status_code=400, content={"error": str(e)})
    _reset_upload_progress()
    return result


@app.get("/rag/upload/status")
async def rag_upload_status():
    """上传进度（前端轮询）：{stage: idle|processing, done, total, page}"""
    with _UPLOAD_PROGRESS_LOCK:
        return dict(_UPLOAD_PROGRESS)


@app.post("/rag/list")
async def rag_list():
    items = await asyncio.to_thread(list_documents)
    return {"items": items}


@app.post("/rag/delete")
async def rag_delete(req: RagDeleteRequest):
    deleted = await asyncio.to_thread(delete_document, req.doc_id)
    return {"deleted": deleted}


@app.post("/rag/ask")
async def rag_ask(req: RagAskRequest):
    """根据已入库文档问答（流式纯文本，前端 getReader 读取）"""
    return StreamingResponse(
        answer_query(req.message.strip()),
        media_type="text/plain",
    )


@app.post("/rag/quiz")
async def rag_quiz(req: RagAskRequest):
    """根据已入库文档出题（流式纯文本）"""
    return StreamingResponse(
        generate_quiz(req.message.strip()),
        media_type="text/plain",
    )


# ==================== 主动互动：SSE 推送 + 定时随机触发 ====================

# 主动互动间隔（秒），可用环境变量调
PROACTIVE_MIN = int(os.environ.get("PROACTIVE_MIN_SECONDS", "180"))
PROACTIVE_MAX = int(os.environ.get("PROACTIVE_MAX_SECONDS", "420"))

# SSE 订阅者队列（每个连上的桌宠一个）
subscribers: set = set()


def publish_event(ev: dict):
    """把事件推给所有在线的桌宠"""
    for q in list(subscribers):
        q.put_nowait(ev)


async def event_stream(q):
    """SSE 长连接：有事件就发，空闲发心跳"""
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=20)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        subscribers.discard(q)


@app.get("/events")
async def events():
    """桌宠订阅主动互动事件（SSE）"""
    q = asyncio.Queue()
    subscribers.add(q)
    return StreamingResponse(
        event_stream(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/screenshot")
def screenshot():
    """抓屏（调试/手动查看用），返回 data URL"""
    from vision import capture_base64, capture_screen

    img = capture_screen()
    return {
        "image": "data:image/jpeg;base64," + capture_base64(img),
        "size": f"{img.width}x{img.height}",
    }


class VisionRequest(BaseModel):
    prompt: str | None = None  # 可选自定义 prompt，默认用奶龙评论模板


@app.post("/vision")
async def vision(req: VisionRequest):
    """奶龙看一眼屏幕，流式返回评论（真视觉 or 降级分析）"""
    return StreamingResponse(_stream_or_buffer(stream_look_at_screen(req.prompt)), media_type="text/plain")


# ==================== Agent 自动化：工具调用循环，自动完成多步任务 ====================
# agent_running / agent_default_root 已定义在文件顶部「全局状态」区

# 默认工作目录持久化:面板选目录 / 聊天口令派活共用,存 backend/data/agent_root.txt(重启不丢)
AGENT_ROOT_FILE = "data/agent_root.txt"


def _load_agent_root() -> str | None:
    """默认工作目录:AGENT_ROOT_DIR 环境变量优先,其次上次面板选的(重启不丢)"""
    if os.environ.get("AGENT_ROOT_DIR", "").strip():
        return os.environ["AGENT_ROOT_DIR"].strip()
    try:
        from pathlib import Path
        f = Path(AGENT_ROOT_FILE)
        if f.exists():
            v = f.read_text(encoding="utf-8").strip()
            return v or None
    except Exception:
        pass
    return None


def _save_agent_root(v: str | None) -> None:
    try:
        from pathlib import Path
        Path("data").mkdir(exist_ok=True)
        Path(AGENT_ROOT_FILE).write_text(v or "", encoding="utf-8")
    except Exception:
        pass


agent_default_root = _load_agent_root()


class ConfirmRequest(BaseModel):
    confirm_id: str
    approved: bool


# 危险工具名 → 中文(聊天流口语化用)
TOOL_CN_MAIN = {
    "bash": "跑个命令",
    "write_file": "写个文件",
    "edit_file": "改个文件",
    "delete_file": "删个文件",
    "move_file": "挪动一下",
    "rename_file": "改个名字",
}


@app.post("/agent/confirm")
async def agent_confirm(req: ConfirmRequest):
    """主人在桌面确认气泡点「放行 / 算了」→ 把决定灌回 agent 循环里挂起的 future"""
    ok = resolve_confirm(req.confirm_id, bool(req.approved))
    return {"ok": ok}


class ToolInvokeRequest(BaseModel):
    tool: str
    args: dict = {}


# 工具台直接调用：仅读类 + 查时间，不经 LLM、不弹确认（写类/命令类需在对话里经主人确认）
_SAFE_TOOLS = {"list_files", "read_file", "search_files", "content_search", "get_current_time"}


@app.post("/agent/tool")
async def agent_tool_invoke(req: ToolInvokeRequest):
    """工具台：直接执行读类/查时间工具，返回结果文本。写类/命令类不走此端点。"""
    if req.tool not in _SAFE_TOOLS:
        return JSONResponse(
            {"error": f"「{req.tool}」是写类/命令类操作，需在对话里说一声、经你确认后执行"},
            status_code=400,
        )
    root = Path(_default_root()).resolve()
    try:
        result = await asyncio.to_thread(_execute_tool, req.tool, req.args or {}, root)
    except Exception as e:
        return {"result": f"调用出错: {e}"}
    return {"result": result}


@app.post("/history/ping")
async def history_ping():
    """奶龙聊天窗口每写一条历史(发送消息/收到回复)后 ping 一次,通知历史面板即时刷新。
    事件驱动而非轮询:面板订阅 /events,收到 type=history 才 refreshHistory。"""
    publish_event({"type": "history"})
    return {"ok": True}


# ==================== 对话历史:服务端持久化 + 多会话(大对话框面板共用一份) ====================
# 历史原先只在奶龙窗口的 localStorage 里(面板靠 IPC 读它)。新增独立大对话框面板
# 窗口后改「服务端一份为准」:每次 recordHistory 同时写这里(data/chat_history.json)
# 并广播 history 事件,文本框面板刷新=读 /history/list 重建显示。
# 现在再升级为多会话:chat_history.json 存 {sessions:[{id,title,messages,...}], active_id};
# /history/list / /history/add 默认操作「当前会话」,桌宠窗口不带 session 参数也透明跟随。
HIST_FILE = DATA_DIR / "chat_history.json"
HIST_MAX = 300


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_session(title="新会话"):
    sid = uuid.uuid4().hex[:8]
    return {
        "id": sid,
        "title": title,
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def _hist_load():
    """读取历史状态 {sessions, active_id};老扁平 {items:[...]} 自动迁移成一个会话。"""
    try:
        data = json.loads(HIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # 老格式迁移:{items:[...]} → 单会话
    if isinstance(data, dict) and isinstance(data.get("items"), list) and "sessions" not in data:
        state = {
            "sessions": [{
                "id": uuid.uuid4().hex[:8],
                "title": "默认会话",
                "messages": data["items"],
                "created_at": _now(),
                "updated_at": _now(),
            }],
            "active_id": None,
        }
        _hist_save(state)
        return state
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        state = {"sessions": [], "active_id": None}
    else:
        state = data
    if not state["sessions"]:
        s = _new_session("默认会话")
        state["sessions"] = [s]
        state["active_id"] = s["id"]
        _hist_save(state)
    if state.get("active_id") not in [s["id"] for s in state["sessions"]]:
        state["active_id"] = state["sessions"][0]["id"]
    return state


def _hist_save(state):
    try:
        HIST_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8"
        )
    except Exception:
        pass


def _active_session(state):
    for s in state["sessions"]:
        if s["id"] == state.get("active_id"):
            return s
    return state["sessions"][0]


def _find_session(state, sid):
    for s in state["sessions"]:
        if s["id"] == sid:
            return s
    return None


class HistoryAddRequest(BaseModel):
    role: str
    text: str
    session_id: str | None = None


@app.post("/history/add")
async def history_add(req: HistoryAddRequest):
    """写一条历史到指定/当前会话 + 广播 history 事件"""
    text = (req.text or "").strip()
    if not text:
        return {"ok": True}
    role = "user" if req.role == "user" else "pet"
    state = _hist_load()
    sess = _find_session(state, req.session_id) or _active_session(state)
    sess["messages"].append({"role": role, "text": text[:500], "ts": _now()})
    if len(sess["messages"]) > HIST_MAX:
        sess["messages"] = sess["messages"][-HIST_MAX:]
    sess["updated_at"] = _now()
    _hist_save(state)
    publish_event({"type": "history"})
    return {"ok": True}


@app.get("/history/list")
async def history_list(session_id: str | None = None):
    """返回指定会话(否则当前会话)的消息,仍为 {items:[...]}(pet.js 兼容)"""
    state = _hist_load()
    sess = _find_session(state, session_id) or _active_session(state)
    return {"items": sess["messages"]}


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SessionIdRequest(BaseModel):
    session_id: str


@app.get("/history/sessions")
async def history_sessions():
    """会话列表(不含消息体),供前端下拉"""
    state = _hist_load()
    items = []
    for s in state["sessions"]:
        items.append({
            "id": s["id"],
            "title": s["title"],
            "count": len(s["messages"]),
            "updated_at": s["updated_at"],
            "active": s["id"] == state.get("active_id"),
        })
    return {"items": items}


@app.post("/history/sessions")
async def history_session_create(req: SessionCreateRequest):
    """新建会话并设为当前"""
    state = _hist_load()
    sess = _new_session((req.title or "").strip() or "新会话")
    state["sessions"].append(sess)
    state["active_id"] = sess["id"]
    _hist_save(state)
    publish_event({"type": "history"})
    return {"id": sess["id"], "title": sess["title"]}


@app.post("/history/sessions/delete")
async def history_session_delete(req: SessionIdRequest):
    """删除会话;若删的是当前会话,自动切到另一个"""
    state = _hist_load()
    before = len(state["sessions"])
    state["sessions"] = [s for s in state["sessions"] if s["id"] != req.session_id]
    if len(state["sessions"]) == before:
        return {"deleted": False}
    if state.get("active_id") == req.session_id:
        state["active_id"] = state["sessions"][0]["id"] if state["sessions"] else None
    if not state["sessions"]:
        s = _new_session("默认会话")
        state["sessions"] = [s]
        state["active_id"] = s["id"]
    _hist_save(state)
    publish_event({"type": "history"})
    return {"deleted": True, "active_id": state["active_id"]}


@app.post("/history/sessions/set-active")
async def history_session_set_active(req: SessionIdRequest):
    """切换当前会话"""
    state = _hist_load()
    if _find_session(state, req.session_id):
        state["active_id"] = req.session_id
        _hist_save(state)
        publish_event({"type": "history"})
        return {"ok": True, "active_id": req.session_id}
    return {"ok": False}


# ==================== 提醒：定时提醒，到期 SSE 推送 ====================

class ReminderRequest(BaseModel):
    text: str
    delay_seconds: int = 300  # 默认 5 分钟


@app.post("/reminders")
def add_reminder(req: ReminderRequest):
    due = datetime.now() + timedelta(seconds=max(1, req.delay_seconds))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reminders (text, due_at) VALUES (?, ?)",
        (req.text, due.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return {"id": cur.lastrowid, "text": req.text, "due_at": due.strftime("%H:%M:%S")}


@app.get("/reminders")
def list_reminders():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, due_at, done FROM reminders ORDER BY due_at"
    ).fetchall()
    conn.close()
    return [{"id": r[0], "text": r[1], "due_at": r[2], "done": r[3]} for r in rows]


@app.delete("/reminders/{rid}")
def delete_reminder(rid: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return {"ok": True}


async def reminder_loop():
    """每 10 秒检查一次到期提醒，推送给桌宠"""
    while True:
        await asyncio.sleep(10)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT id, text FROM reminders WHERE done=0 AND due_at <= ?", (now,)
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE reminders SET done=1 WHERE id=?", [(r[0],) for r in rows]
                )
                conn.commit()
            conn.close()
            for rid, text in rows:
                publish_event({"type": "reminder", "text": text})
                logger.info(f"[Dfrog] 提醒: {text}")
        except Exception as e:
            logger.info(f"[Dfrog] 提醒检查出错: {e}")


# ==================== 速记：随手记，AI 整理 ====================

class NoteRequest(BaseModel):
    text: str


@app.post("/notes")
def add_note(req: NoteRequest):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO notes (text) VALUES (?)", (req.text,))
    conn.commit()
    conn.close()
    return {"id": cur.lastrowid}


@app.get("/notes")
def list_notes():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, text, created_at FROM notes ORDER BY id DESC").fetchall()
    conn.close()
    return [{"id": r[0], "text": r[1], "created_at": r[2]} for r in rows]


@app.delete("/notes/{nid}")
def delete_note(nid: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM notes WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/notes/summarize")
async def summarize_notes():
    """AI 把所有速记整理成一份清单（流式）"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT text FROM notes ORDER BY id").fetchall()
    conn.close()
    if not rows:
        async def _empty():
            yield "还没有速记。"
        return StreamingResponse(_empty(), media_type="text/plain")
    text = "\n".join(f"- {r[0]}" for r in rows)
    return StreamingResponse(
        _stream_or_buffer(stream_naiwa_reply(f"下面是我的所有速记，帮我整理成一份简洁的中文清单：\n{text}")),
        media_type="text/plain",
    )


# 主动互动场景提示词：不写死台词，把场景丢给 LLM 自由生成
PROACTIVE_SCENARIOS = [
    # (权重, 场景模板，{now} 由运行时填充)
    (0.45, "主人已经很久没和你说话了（现在{now}）。你有点想他了，像好朋友一样随口说一句话，别太长。"),
    (0.25, "现在是{now}，主人正盯着屏幕忙自己的事。你偷偷看了他一眼，像{name}一样随口唠一句（可以关心、吐槽或捣蛋，自然点）。"),
    (0.20, "主人忙了好久没理你了（现在是{now}）。随口甩给他一句，像真人聊天一样，撒娇、吐槽或整活都行。"),
    (0.10, "今天快过完了（{now}）。{name}想对主人说一句自然的话，像睡前或收工时的那种。"),
]

async def proactive_loop():
    """
    后台定时器：每 3-7 分钟随机触发一次主动互动。
    kind：场景提示词（{now} 填充）→ LLM 自由生成台词（挂了安静跳过，不留固定话）
    """
    while True:
        await asyncio.sleep(random.randint(PROACTIVE_MIN, PROACTIVE_MAX))
        if not _get_setting("proactive", True):
            continue  # 主动互动开关关了，本轮跳过
        try:
            weights = [w for w, _ in PROACTIVE_SCENARIOS]
            scene = random.choices(PROACTIVE_SCENARIOS, weights=weights)[0][1]
            prompt = scene.format(now=_now_str(), name=get_pet_name())
            # LLM 调用是阻塞 HTTP，丢线程池里，别卡住事件循环
            reply = await asyncio.to_thread(llm_naiwa_reply, prompt)
            if not reply:
                continue
            action = random.choices(["", "laugh", "dance"], weights=[0.7, 0.15, 0.15])[0]
            if action:
                publish_event({"type": "action", "action": action, "text": reply})
            else:
                publish_event({"type": "speak", "text": reply})
            update_status({"presence": 2})
        except Exception as e:
            logger.info(f"[Dfrog] 主动互动出错: {e}")


# ==================== 桌宠形象管理（形象 = 素材文件夹，里面文件 = 动作）====================
class PetAvatarIdRequest(BaseModel):
    avatar_id: str


class PetAvatarNameRequest(BaseModel):
    avatar_id: str
    name: str


class PetActionFileRequest(BaseModel):
    avatar_id: str
    file: str


class PetActionRenameRequest(BaseModel):
    avatar_id: str
    file: str
    name: str


class PetActionMoveRequest(BaseModel):
    avatar_id: str
    file: str
    direction: str  # "up" / "down"


@app.post("/pet/avatar/upload")
async def pet_avatar_upload(file: UploadFile = File(...)):
    """单图上传 → 一个形象 + 1 个动作（待机位）"""
    content = await file.read()
    try:
        result = await asyncio.to_thread(save_single_upload, content, file.filename or "image")
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    publish_event({"type": "pet_avatar"})
    return result


@app.post("/pet/avatar/create")
def pet_avatar_create(req: PetAvatarNameRequest):
    """新建空形象（无动作），后续可追加动作"""
    return create_avatar(req.name)


@app.post("/pet/avatar/generate")
async def pet_avatar_generate(req: PetAvatarIdRequest):
    """AI 生成动作动图（覆盖手动动作）"""
    try:
        result = await asyncio.to_thread(generate_animations, req.avatar_id)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    publish_event({"type": "pet_avatar"})  # 生成完成 → 桌宠/侧栏刷新动图
    return {"anim_count": len(result.get("actions", []))}


@app.get("/pet/avatars")
def pet_avatars():
    return {"items": list_avatars()}


@app.get("/pet/avatar/current")
def pet_avatar_current():
    return get_current() or {"avatar_id": None, "name": "", "manifest": []}


@app.get("/pet/avatar/{aid}")
def pet_avatar_detail(aid: str):
    """形象详情：含动作清单（带 url + 槽位名），供前端展示/调序"""
    av = get_avatar(aid)
    if not av:
        return JSONResponse(status_code=404, content={"error": "形象不存在"})
    return av


@app.post("/pet/avatar/activate")
async def pet_avatar_activate(req: PetAvatarIdRequest):
    ok = await asyncio.to_thread(activate_avatar, req.avatar_id)
    if ok:
        publish_event({"type": "pet_avatar"})
    return {"ok": ok}


@app.post("/pet/avatar/rename")
def pet_avatar_rename(req: PetAvatarNameRequest):
    ok = rename_avatar(req.avatar_id, req.name)
    publish_event({"type": "pet_avatar"})
    return {"ok": ok}


class PetAvatarMetaRequest(BaseModel):
    avatar_id: str
    role_name: str = ""
    prompt: str = ""


@app.post("/pet/avatar/meta")
def pet_avatar_meta(req: PetAvatarMetaRequest):
    """设置形象的角色名 + 提示词（塑造该形象的对话人设）"""
    ok = set_avatar_meta(req.avatar_id, req.role_name, req.prompt)
    publish_event({"type": "pet_avatar"})
    return {"ok": ok}


@app.get("/pet/avatar/anim/{aid}/{filename}")
def pet_avatar_anim(aid: str, filename: str):
    """动作文件（gif/png/apng 原样返回，按扩展名定 mime）"""
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse(status_code=400, content={"error": "非法文件名"})
    p = avatar_dir(aid) / filename
    if not p.exists():
        return JSONResponse(status_code=404, content={"error": "动作文件不存在"})
    import mimetypes
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media_type)


@app.post("/pet/avatar/delete")
async def pet_avatar_delete(req: PetAvatarIdRequest):
    ok = await asyncio.to_thread(delete_avatar, req.avatar_id)
    return {"ok": ok}


# ==================== 动作管理（增删改序，文件原样保存）====================
@app.post("/pet/avatar/action/add")
async def pet_avatar_action_add(avatar_id: str = Form(...), file: UploadFile = File(...)):
    """给形象追加一个动作文件（原样保存，保留 gif 动画）"""
    content = await file.read()
    try:
        result = await asyncio.to_thread(add_action, avatar_id, file.filename or "action", content)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    publish_event({"type": "pet_avatar"})
    return result


@app.post("/pet/avatar/action/delete")
def pet_avatar_action_delete(req: PetActionFileRequest):
    ok = delete_action(req.avatar_id, req.file)
    publish_event({"type": "pet_avatar"})
    return {"ok": ok}


@app.post("/pet/avatar/action/rename")
def pet_avatar_action_rename(req: PetActionRenameRequest):
    ok = rename_action(req.avatar_id, req.file, req.name)
    publish_event({"type": "pet_avatar"})
    return {"ok": ok}


@app.post("/pet/avatar/action/move")
def pet_avatar_action_move(req: PetActionMoveRequest):
    ok = move_action(req.avatar_id, req.file, req.direction)
    publish_event({"type": "pet_avatar"})
    return {"ok": ok}


@app.post("/pet/folder/upload")
async def pet_folder_upload(files: list[UploadFile] = File(...), name: str = Form("")):
    """上传一个素材文件夹 → 一个形象，里面每张图/gif 成为一个动作（原样保存）"""
    items = []
    for f in files:
        content = await f.read()
        items.append((f.filename or "image", content))
    if not items:
        return JSONResponse(status_code=400, content={"error": "没有文件"})
    try:
        result = await asyncio.to_thread(save_folder_upload, items, name)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    publish_event({"type": "pet_avatar"})  # 新形象入库 → 桌宠/侧栏刷新
    return result


# ==================== 模型配置（每个功能页面查看/修改模型名+Key）====================
class ModelConfigRequest(BaseModel):
    feature: str
    model: str
    key_mode: str  # "env" 环境变量名 / "direct" 直接填 Key
    key_value: str


@app.get("/model/config")
def model_config_get():
    return {"items": modelconfig.load_config()}


@app.post("/model/config")
def model_config_set(req: ModelConfigRequest):
    modelconfig.set_feature_config(req.feature, req.model, req.key_mode, req.key_value)
    return {"ok": True, "config": modelconfig.get_feature_config(req.feature)}


# ==================== 设置 ====================
class SettingsRequest(BaseModel):
    settings: dict


@app.get("/settings")
def get_settings():
    return _load_settings()


@app.post("/settings")
def set_settings(req: SettingsRequest):
    cur = _load_settings()
    changed = {}
    for k, v in (req.settings or {}).items():
        if k in cur and cur[k] != v:
            cur[k] = v
            changed[k] = v
    _save_settings(cur)
    if "auto_start" in changed:
        set_autostart(cur["auto_start"])
    if changed:
        publish_event({"type": "settings"})
    return {"ok": True, "settings": cur}


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except KeyboardInterrupt:
        # Ctrl+C/关闭终端时的停止信号,静默退出(正常行为,不该打印 traceback)
        pass
