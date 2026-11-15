"""AI桌宠助手 · 图生多动作动图（最小验证）

给一张白底桌宠图，千问视觉模型规划动作，程序化生成透明背景 APNG 动图。
流程：抠图去白底 → 大模型规划 3 个动作 → 程序化生成帧序列 → 合成透明 APNG。

用法：
    .venv/Scripts/python.exe tools/gen_pet_animations.py [图片路径] [输出目录]
不传图片路径时，自动生成一张白底测试图。

说明：qwen3.8-max 是视觉理解模型，不生成像素；动作帧由程序化变换生成，
大模型负责「理解图片 + 规划动作类型/幅度」。
"""
import sys
import os
import io
import json
import math
import base64
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw
from openai import OpenAI

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DASHSCOPE_CHAT_MODEL = os.environ.get("DASHSCOPE_CHAT_MODEL", "qwen3.8-max-0902")

DEFAULT_ACTIONS = [
    {"name": "摇摆", "type": "sway", "frames": 10, "amp": 0.6},
    {"name": "跳跃", "type": "jump", "frames": 10, "amp": 0.7},
    {"name": "点头", "type": "nod", "frames": 10, "amp": 0.5},
]


# ---- 抠图去白底（BFS floodfill，从四角去除白色连通区域）----
def remove_white_bg(img: Image.Image, threshold: int = 245) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    visited = [[False] * w for _ in range(h)]
    dq = deque()
    for x, y in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if not visited[y][x]:
            r, g, b, _ = px[x, y]
            if r > threshold and g > threshold and b > threshold:
                dq.append((x, y))
                visited[y][x] = True
                while dq:
                    cx, cy = dq.popleft()
                    r2, g2, b2, _ = px[cx, cy]
                    px[cx, cy] = (r2, g2, b2, 0)
                    for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < w and 0 <= ny < h and not visited[ny][nx]:
                            r3, g3, b3, _ = px[nx, ny]
                            if r3 > threshold and g3 > threshold and b3 > threshold:
                                visited[ny][nx] = True
                                dq.append((nx, ny))
    return img


# ---- 大模型规划动作 ----
def plan_actions(img: Image.Image) -> list[dict]:
    if not DASHSCOPE_API_KEY:
        print("[警告] 未配置 DASHSCOPE_API_KEY，使用默认动作")
        return list(DEFAULT_ACTIONS)
    client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    prompt = (
        "你是一名桌宠动作设计师。分析这张桌宠图片，规划 3 个不同的动作，让桌宠动起来。"
        "每个动作输出 JSON：\n"
        "- name: 动作名(中文)\n"
        "- type: 变换类型，从 sway(摇摆)/jump(跳跃)/nod(点头)/wave(招手)/rotate(旋转)/scale(脉冲缩放) 选一个\n"
        "- frames: 帧数(8-12)\n"
        "- amp: 变换幅度(0.3-1.0)\n"
        "只输出 JSON 数组，不要任何其他文字或解释。"
        '格式：[{"name":"摇摆","type":"sway","frames":10,"amp":0.6}, ...]'
    )
    resp = client.chat.completions.create(
        model=DASHSCOPE_CHAT_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        max_tokens=500, temperature=0.3,
    )
    text = resp.choices[0].message.content.strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    try:
        if start < 0 or end <= start:
            raise ValueError("未找到 JSON 数组")
        actions = json.loads(text[start:end])
        if not isinstance(actions, list) or len(actions) < 1:
            raise ValueError("动作数组为空")
        print(f"大模型规划动作: {json.dumps(actions, ensure_ascii=False)}")
        return actions
    except Exception as e:
        print(f"模型输出解析失败({e})，使用默认动作: {json.dumps(DEFAULT_ACTIONS, ensure_ascii=False)}")
        return list(DEFAULT_ACTIONS)


# ---- 程序化生成帧序列 ----
def make_frames(transparent: Image.Image, action: dict) -> list[Image.Image]:
    type_ = action.get("type", "sway")
    n = int(action.get("frames", 10))
    amp = float(action.get("amp", 0.6))
    w, h = transparent.size
    cx, cy = w / 2, h / 2
    frames: list[Image.Image] = []
    for i in range(n):
        t = i / n
        if type_ == "jump":
            dy = -amp * 40 * math.sin(math.pi * t)
            canvas = Image.new("RGBA", (w, h + int(amp * 40) + 10), (0, 0, 0, 0))
            canvas.paste(transparent, (0, int(dy) + 5), transparent)
            frames.append(canvas)
        elif type_ == "scale":
            s = 1 + amp * 0.15 * math.sin(2 * math.pi * t)
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            resized = transparent.resize((nw, nh), Image.BICUBIC)
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2), resized)
            frames.append(canvas)
        elif type_ == "rotate":
            angle = 360 * t
            frames.append(transparent.rotate(angle, center=(cx, cy),
                                             resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0)))
        else:
            # sway / nod / wave 都用旋转，幅度不同
            amps = {"sway": 15, "nod": 8, "wave": 25}
            angle = amp * amps.get(type_, 12) * math.sin(2 * math.pi * t)
            frames.append(transparent.rotate(angle, center=(cx, cy),
                                             resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0)))
    return frames


def save_apng(frames: list[Image.Image], path: Path, duration: int = 100):
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=duration, loop=0, format="PNG")


# ---- 生成白底测试图 ----
def make_test_image(path: Path):
    img = Image.new("RGB", (300, 300), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([80, 40, 220, 180], fill="#FFB74D", outline="#E65100", width=3)   # 头
    d.ellipse([100, 170, 200, 270], fill="#FFB74D", outline="#E65100", width=3)  # 身体
    d.ellipse([110, 90, 130, 110], fill="black")   # 左眼
    d.ellipse([170, 90, 190, 110], fill="black")   # 右眼
    d.arc([120, 120, 180, 150], 0, 180, fill="black", width=3)  # 嘴
    img.save(path)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("desktop/assets/anim")
    out_dir.mkdir(parents=True, exist_ok=True)

    if src:
        img = Image.open(src)
        print(f"输入图: {src} {img.size}")
    else:
        src = "data/pet_test.png"
        make_test_image(Path(src))
        img = Image.open(src)
        print(f"未指定图片，生成白底测试图: {src}")

    transparent = remove_white_bg(img)
    print("已去白底，得到透明背景")

    actions = plan_actions(transparent)

    manifest = {"animations": []}
    for act in actions:
        frames = make_frames(transparent, act)
        safe = "".join(c for c in str(act.get("name", "action")) if c not in '\\/:*?"<>|') or "action"
        out_path = out_dir / f"{safe}.png"
        save_apng(frames, out_path)
        manifest["animations"].append({
            "file": out_path.name,
            "name": act.get("name", ""),
            "type": act.get("type", ""),
        })
        print(f"  生成: {out_path}  ({len(frames)}帧, 类型 {act.get('type')})")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  动图清单: {manifest_path}")
    print(f"\n完成，{len(actions)} 个动图在: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
