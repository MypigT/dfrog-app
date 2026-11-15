"""
奶龙「看屏幕」模块

功能：
- 截屏（mss 抓屏，缩放控制体积）
- 图像分析降级（没配 API Key 时，用亮度/主色调猜屏幕状态）
- 视觉模型评论（百炼 qwen3.8-max，DashScope 兼容 OpenAI 格式）

Key 配置：
- 环境变量 DASHSCOPE_API_KEY —— 一把 Key 通吃百炼所有模型（对话/视觉/嵌入）
- 没配 Key 时自动走图像分析降级，不报错；配了 Key 自动切真视觉
"""

import base64
import logging
import io
import os
import random
from collections import Counter

import mss
from PIL import Image

logger = logging.getLogger("dfrog")

# ==================== 百炼（DashScope）配置 ====================

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
VISION_MODEL = os.environ.get("DASHSCOPE_VISION_MODEL", "qwen3.8-max-0902")

MAX_WIDTH = 1280  # 截屏缩放上限，控制 base64 体积（API 计费也看体积）
JPEG_QUALITY = 70

VISION_PROMPT = """你正在看主人电脑的屏幕截图。先仔细看画面里**实际是什么**，再用"{name}"的语气，基于画面真实内容发言。

角色人设：
- 活泼爱吐槽，可以带点夸张，但每一句话都必须基于你真正看到的内容
- 中文，30~60 字即可，别超长

判断规则（先观察，再吐槽）：
- 画面里是代码/终端/文档/表格/设计软件/邮件等 → 主人在忙正事，可以夸一句或轻轻损一句，但**不许说他"摸鱼"**
- 画面里是视频/剧/游戏/短视频/娱乐网页 → 这时才有资格说"摸鱼"，语气兴奋点或吐槽点
- 画面里是浏览器/聊天窗口/资料页 → 如实说"在翻网页 / 查资料 / 跟人聊天"，别一律扣"摸鱼"帽子
- 画面黑屏/锁屏/空白 → 说看不清，或吐槽"对着空气干嘛"
- 画面里有一只小龙 → 那是{name}本尊，无视，别提
- 实在看不清就直说看不清，别编，更不许随便说"主人在摸鱼"
- 只输出{name}的台词，不要解释、不要描述、不加前后缀
"""


def _pet_name() -> str:
    """当前形象的角色名（懒加载 pet 模块，避免循环导入）；取不到回退默认名。"""
    try:
        from pet import get_pet_name
        return get_pet_name()
    except Exception:
        return "奶龙"


# ==================== 截屏 ====================

def capture_screen(max_width: int = MAX_WIDTH) -> Image.Image:
    """抓取主屏幕截图，缩放到 max_width 以内（聊天用小图加快响应）"""
    with mss.mss() as sct:
        primary = sct.monitors[1]  # monitors[0] 是全部虚拟屏，[1] 是主屏
        shot = sct.grab(primary)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:
        new_h = int(img.height * max_width / img.width)
        img = img.resize((max_width, new_h), Image.LANCZOS)
    return img


def capture_base64(img: Image.Image) -> str:
    """转 JPEG base64（调用方拼 data URL 前缀）"""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode()


# ==================== 降级方案：图像分析 ====================

def analyze_image(img: Image.Image) -> str:
    """
    没有视觉 Key 时的兜底：不做真视觉，
    用亮度/主色调猜屏幕状态，返回一句奶龙台词。
    """
    small = img.resize((32, 32), Image.LANCZOS).convert("RGB")
    pixels = list(small.getdata())
    n = len(pixels)
    brightness = sum((r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels) / n

    # 主色调分桶
    buckets = Counter()
    for r, g, b in pixels:
        buckets[(r // 64 * 64, g // 64 * 64, b // 64 * 64)] += 1
    dr, dg, db = buckets.most_common(1)[0][0]

    # 亮度 → 基础文案
    if brightness < 40:
        lines = ["黑灯瞎火的，你是不是又熬夜了。", "屏幕这么暗，眼睛不想要了？"]
    elif brightness > 150:
        lines = ["屏幕亮得刺眼，哈哈。", "好亮的屏，闪瞎我了。"]
    else:
        lines = ["屏幕亮度还行。", "嗯，屏幕看着挺正常的。"]

    # 主色调 → 追加花样
    if dg > dr + 40 and dg > db + 40:
        lines.append("满屏都是绿色……代码写魔怔了？")
    elif dr > dg + 40 and dr > db + 40:
        lines.append("红红的屏幕，看视频呢？")
    elif db > dr + 30 and db > dg + 30:
        lines.append("蓝蓝的，是不是在看网页？")

    return random.choice(lines)


# ==================== 视觉模型评论 ====================

def llm_vision_comment(img: Image.Image, prompt: str | None = None) -> str:
    """
    调用百炼 qwen3.8-max 看图评论（视觉模型）。
    返回奶龙台词；失败抛异常，由调用方兜底。
    """
    from openai import OpenAI

    client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
    img_b64 = capture_base64(img)
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": prompt or VISION_PROMPT.format(name=_pet_name())},
            ],
        }],
        max_tokens=200,
        temperature=1.2,
    )
    return resp.choices[0].message.content.strip()


# ==================== 统一入口 ====================

def look_at_screen(prompt: str | None = None) -> tuple[str, str]:
    """
    奶龙「看一眼屏幕」。返回 (台词, 引擎标记)。
    - 配了 DASHSCOPE_API_KEY → 真视觉（qwen3.8-max）
    - 没配 Key 或调用失败 → 图像分析降级
    """
    img = capture_screen()
    if DASHSCOPE_API_KEY:
        try:
            p = prompt
            if p is None:
                from datetime import datetime
                p = VISION_PROMPT.format(name=_pet_name()) + f"\n\n当前时间：{datetime.now().strftime('%H:%M')}。"
            return llm_vision_comment(img, p), "vision"
        except Exception as e:
            logger.info(f"[Dfrog] 视觉模型调用失败，走降级: {e}")
    return analyze_image(img), "fallback"


# ==================== 流式视觉评论 ====================

async def stream_vision_comment(img: Image.Image, prompt: str | None = None):
    """流式调用百炼 qwen3.8-max 看图评论，逐字 yield 文本增量"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
    img_b64 = capture_base64(img)
    stream = await client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": prompt or VISION_PROMPT.format(name=_pet_name())},
            ],
        }],
        max_tokens=200,
        temperature=1.2,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def stream_look_at_screen(prompt: str | None = None):
    """流式「看一眼屏幕」。有 Key 走真视觉流式，失败/无 Key 降级为一次性文案（仍逐字吐）。"""
    img = capture_screen()
    if DASHSCOPE_API_KEY:
        try:
            p = prompt
            if p is None:
                from datetime import datetime
                p = VISION_PROMPT.format(name=_pet_name()) + f"\n\n当前时间：{datetime.now().strftime('%H:%M')}。"
            async for delta in stream_vision_comment(img, p):
                yield delta
            return
        except Exception as e:
            logger.info(f"[Dfrog] 视觉模型流式调用失败，走降级: {e}")
    yield analyze_image(img)
