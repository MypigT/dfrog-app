"""
AI桌宠助手 · 桌宠形象管理 + 动作动图

形象 = 素材文件夹：一个形象目录里直接存动作文件（gif/png/apng 原样保存，保留动画），
manifest.json 记录动作清单，数组顺序即桌宠槽位顺序（待机/跳舞/笑/变形）。

数据存 DATA_DIR/avatar/：
  index.json                形象清单 + 当前激活形象
  {avatar_id}/
    manifest.json           动作清单 [{file, name}]
    {动作文件}              gif/png/apng 原样保存

兼容旧结构（文件夹层级 / 扁平带 source+anim）自动迁移。
"""

import io
import logging
import json
import math
import os
import re
import sys
import base64
import shutil
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from PIL import Image
from openai import OpenAI

logger = logging.getLogger("dfrog")

# ---- 模型配置（env 已在 main.py load_dotenv() 之后加载）----
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DASHSCOPE_CHAT_MODEL = os.environ.get("DASHSCOPE_CHAT_MODEL", "qwen3.8-max-0902")

# ---- 数据目录（自算，避免循环导入，同 rag.py）----
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", Path(sys.executable).parent)) / "dfrog"
else:
    DATA_DIR = Path(__file__).parent.parent / "data"

AVATAR_DIR = DATA_DIR / "avatar"
INDEX_PATH = AVATAR_DIR / "index.json"

DEFAULT_ACTIONS = [
    {"name": "摇摆", "type": "sway", "frames": 10, "amp": 0.6},
    {"name": "跳跃", "type": "jump", "frames": 10, "amp": 0.7},
    {"name": "点头", "type": "nod", "frames": 10, "amp": 0.5},
]

# 桌宠槽位（桌宠端按 manifest 顺序取前 4 个映射：待机/跳舞/笑/变形）
SLOT_NAMES = ["待机", "跳舞", "笑", "变形"]


# ==================== 动图生成核心（复用 gen_pet_animations.py）====================
def remove_white_bg(img: Image.Image, threshold: int = 245) -> Image.Image:
    """BFS floodfill 从四角去除白色连通区域，去白底得透明背景。"""
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


def plan_actions(img: Image.Image) -> list[dict]:
    """千问视觉分析图片，规划 3 个动作。无 Key 或解析失败用默认动作。"""
    import modelconfig
    key = modelconfig.resolve_key("petimg")
    if not key:
        logger.info("[Dfrog] 未配置 Key（模型配置-桌宠形象），使用默认动作")
        return list(DEFAULT_ACTIONS)
    client = OpenAI(api_key=key, base_url=DASHSCOPE_BASE_URL)
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
    for attempt in range(3):
        try:
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
            if start < 0 or end <= start:
                raise ValueError("未找到 JSON 数组")
            actions = json.loads(text[start:end])
            if not isinstance(actions, list) or len(actions) < 1:
                raise ValueError("动作数组为空")
            logger.info(f"[Dfrog] 规划动作: {json.dumps(actions, ensure_ascii=False)}")
            return actions
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            logger.info(f"[Dfrog] 规划动作失败({e})，使用默认动作")
            return list(DEFAULT_ACTIONS)
    return list(DEFAULT_ACTIONS)


def make_frames(transparent: Image.Image, action: dict) -> list[Image.Image]:
    """按动作类型程序化生成帧序列。"""
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
            amps = {"sway": 15, "nod": 8, "wave": 25}
            angle = amp * amps.get(type_, 12) * math.sin(2 * math.pi * t)
            frames.append(transparent.rotate(angle, center=(cx, cy),
                                             resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0)))
    return frames


def save_apng(frames: list[Image.Image], path: Path, duration: int = 100):
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=duration, loop=0, format="PNG")


# ==================== 形象管理（形象 = 素材文件夹，里面文件 = 动作）====================
def avatar_dir(aid: str) -> Path:
    return AVATAR_DIR / aid


def manifest_path(aid: str) -> Path:
    return avatar_dir(aid) / "manifest.json"


def _safe_filename(name: str) -> str:
    """动作文件名安全处理：去目录、替换非法字符"""
    name = Path(name).name
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return name or "action"


def _load_manifest(aid: str) -> list[dict]:
    try:
        data = json.loads(manifest_path(aid).read_text(encoding="utf-8"))
        return data.get("actions", [])
    except Exception:
        return []


def _save_manifest(aid: str, actions: list[dict]):
    avatar_dir(aid).mkdir(parents=True, exist_ok=True)
    manifest_path(aid).write_text(
        json.dumps({"actions": actions}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _find_avatar(state, aid):
    for a in state["avatars"]:
        if a["id"] == aid:
            return a
    return None


def _with_meta(aid: str, actions: list[dict]) -> list[dict]:
    """给动作补 url 和槽位名（前端展示用）"""
    out = []
    for i, an in enumerate(actions):
        out.append({
            "file": an["file"],
            "name": an.get("name", ""),
            "url": f"/pet/avatar/anim/{aid}/{an['file']}",
            "slot": SLOT_NAMES[i] if i < len(SLOT_NAMES) else "额外",
        })
    return out


# ---- 迁移：兼容旧结构 ----
def _migrate(data: dict) -> dict:
    # 旧文件夹层级 {folders:[{id,name,avatars:[{id,name,...}]}]}
    # → 每个 folder 合并为一个形象，folder 内 avatar 的 source.png 作为动作
    if isinstance(data, dict) and isinstance(data.get("folders"), list) and "avatars" not in data:
        avatars = []
        active = None
        old_active = data.get("active_avatar_id")
        for f in data["folders"]:
            aid = f["id"]
            actions = []
            for a in f.get("avatars", []):
                src = AVATAR_DIR / f["id"] / a["id"] / "source.png"
                if src.exists():
                    new_name = (a["name"] or a["id"]) + ".png"
                    dst = avatar_dir(aid) / new_name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    actions.append({"file": new_name, "name": a["name"]})
                    shutil.rmtree(AVATAR_DIR / f["id"] / a["id"], ignore_errors=True)
            if actions:
                _save_manifest(aid, actions)
            avatars.append({
                "id": aid, "name": f["name"],
                "created_at": f.get("created_at", ""),
            })
            if old_active == aid or old_active in [a["id"] for a in f.get("avatars", [])]:
                active = aid
        # 清理空 folder 目录
        for f in data["folders"]:
            d = AVATAR_DIR / f["id"]
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        state = {"avatars": avatars, "active_id": active}
        save_index(state)
        return state

    # 最旧扁平 {avatars:[{id,name,source,anim_count}], active_id}
    # 仅当 avatar 带 source 字段（老格式标记）才迁移；新结构（无 source、有 manifest）不匹配，原样放行
    if (isinstance(data, dict) and isinstance(data.get("avatars"), list)
            and "folders" not in data
            and any(isinstance(a, dict) and "source" in a for a in data["avatars"])):
        avatars = []
        for a in data["avatars"]:
            aid = a["id"]
            actions = []
            src = avatar_dir(aid) / "source.png"
            if src.exists():
                new_name = (a["name"] or aid) + ".png"
                dst = avatar_dir(aid) / new_name
                if src != dst:
                    shutil.copy2(src, dst)
                actions.append({"file": dst.name, "name": a["name"]})
            adir = avatar_dir(aid) / "anim"
            if adir.exists():
                for i, p in enumerate(sorted(adir.glob("*.png"))):
                    actions.append({"file": p.name, "name": f"动作{i+1}"})
                shutil.rmtree(adir, ignore_errors=True)
            if actions:
                _save_manifest(aid, actions)
            avatars.append({
                "id": aid, "name": a["name"],
                "created_at": a.get("created_at", ""),
            })
        state = {"avatars": avatars, "active_id": data.get("active_id")}
        save_index(state)
        return state

    # 新结构
    if not isinstance(data, dict) or not isinstance(data.get("avatars"), list):
        state = {"avatars": [], "active_id": None}
    else:
        state = data
    return state


def load_index() -> dict:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"avatars": [], "active_id": None}
    return _migrate(data)


def save_index(state: dict):
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def list_avatars() -> list[dict]:
    state = load_index()
    items = []
    for a in state["avatars"]:
        items.append({
            "id": a["id"],
            "name": a["name"],
            "role_name": a.get("role_name", ""),
            "anim_count": len(_load_manifest(a["id"])),
            "active": a["id"] == state.get("active_id"),
            "created_at": a.get("created_at", ""),
        })
    return items


def get_avatar(aid: str) -> dict | None:
    state = load_index()
    a = _find_avatar(state, aid)
    if not a:
        return None
    manifest = _with_meta(aid, _load_manifest(aid))
    return {
        "id": aid,
        "name": a["name"],
        "role_name": a.get("role_name", ""),
        "prompt": a.get("prompt", ""),
        "created_at": a.get("created_at", ""),
        "manifest": manifest,
        "active": a["id"] == state.get("active_id"),
    }


def create_avatar(name: str = "") -> dict:
    aid = uuid.uuid4().hex[:8]
    avatar_dir(aid).mkdir(parents=True, exist_ok=True)
    _save_manifest(aid, [])
    state = load_index()
    n = len(state["avatars"]) + 1
    avatar_name = (name or f"形象{n}").strip()
    state["avatars"].append({
        "id": aid, "name": avatar_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    state["active_id"] = aid
    save_index(state)
    return {"id": aid, "name": avatar_name}


def activate_avatar(aid: str) -> bool:
    state = load_index()
    if not _find_avatar(state, aid):
        return False
    state["active_id"] = aid
    save_index(state)
    return True


def rename_avatar(aid: str, name: str) -> bool:
    state = load_index()
    a = _find_avatar(state, aid)
    if not a:
        return False
    a["name"] = (name or "").strip() or a["name"]
    save_index(state)
    return True


def set_avatar_meta(aid: str, role_name: str, prompt: str) -> bool:
    """设置形象的角色名 + 提示词（塑造该形象的对话人设）"""
    state = load_index()
    a = _find_avatar(state, aid)
    if not a:
        return False
    if role_name is not None:
        a["role_name"] = role_name.strip()
    if prompt is not None:
        a["prompt"] = prompt.strip()
    save_index(state)
    return True


def delete_avatar(aid: str) -> bool:
    state = load_index()
    if not _find_avatar(state, aid):
        return False
    state["avatars"] = [a for a in state["avatars"] if a["id"] != aid]
    if state.get("active_id") == aid:
        state["active_id"] = state["avatars"][0]["id"] if state["avatars"] else None
    save_index(state)
    d = avatar_dir(aid)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return True


def get_current() -> dict | None:
    state = load_index()
    aid = state.get("active_id")
    if not aid:
        return None
    a = _find_avatar(state, aid)
    if not a:
        return None
    manifest = _with_meta(aid, _load_manifest(aid))
    return {
        "avatar_id": aid,
        "name": a["name"],
        "role_name": a.get("role_name", ""),
        "prompt": a.get("prompt", ""),
        "manifest": manifest,
    }


# 默认角色名（没设形象或没填角色名时回退，保持开箱即用）
DEFAULT_PET_NAME = "奶龙"


def get_pet_name() -> str:
    """当前激活形象的角色名；没形象或没填角色名时回退默认名。
    供各模块的系统提示词动态取名字，让对话身份跟随当前形象。"""
    cur = get_current()
    if cur:
        role = (cur.get("role_name") or "").strip()
        if role:
            return role
    return DEFAULT_PET_NAME


# ==================== 动作管理（增删改序，文件原样保存）====================
def add_action(aid: str, filename: str, content: bytes) -> dict:
    """给形象追加一个动作文件（原样保存，保留 gif 动画）"""
    state = load_index()
    if not _find_avatar(state, aid):
        raise ValueError("形象不存在")
    fname = _safe_filename(filename)
    stem = Path(filename).stem.strip() or "动作"
    actions = _load_manifest(aid)
    if any(a["file"] == fname for a in actions):
        fname = f"{len(actions)}_{fname}"
    (avatar_dir(aid) / fname).write_bytes(content)
    actions.append({"file": fname, "name": stem})
    _save_manifest(aid, actions)
    return {"file": fname, "name": stem}


def delete_action(aid: str, filename: str) -> bool:
    actions = _load_manifest(aid)
    actions = [a for a in actions if a["file"] != filename]
    _save_manifest(aid, actions)
    p = avatar_dir(aid) / filename
    if p.exists() and p.is_file():
        p.unlink()
    return True


def rename_action(aid: str, filename: str, new_name: str) -> bool:
    actions = _load_manifest(aid)
    for a in actions:
        if a["file"] == filename:
            a["name"] = (new_name or "").strip() or a["name"]
    _save_manifest(aid, actions)
    return True


def move_action(aid: str, filename: str, direction: str) -> bool:
    """调整动作顺序（up/down），顺序即桌宠槽位"""
    actions = _load_manifest(aid)
    idx = next((i for i, a in enumerate(actions) if a["file"] == filename), None)
    if idx is None:
        return False
    if direction == "up" and idx > 0:
        actions[idx], actions[idx - 1] = actions[idx - 1], actions[idx]
    elif direction == "down" and idx < len(actions) - 1:
        actions[idx], actions[idx + 1] = actions[idx + 1], actions[idx]
    _save_manifest(aid, actions)
    return True


# ==================== 上传 ====================
def save_folder_upload(files: list, name: str = "") -> dict:
    """上传一个素材文件夹 → 一个形象，里面每张图/gif 成为一个动作（原样保存）
    files: [(filename, content_bytes)]，按文件名排序，顺序即槽位"""
    aid = uuid.uuid4().hex[:8]
    adir = avatar_dir(aid)
    adir.mkdir(parents=True, exist_ok=True)
    files_sorted = sorted(files, key=lambda x: x[0])
    actions = []
    used = set()
    for idx, (filename, content) in enumerate(files_sorted):
        fname = _safe_filename(filename)
        if fname in used:
            fname = f"{idx}_{fname}"
        used.add(fname)
        stem = Path(filename).stem.strip() or f"动作{idx + 1}"
        (adir / fname).write_bytes(content)  # 原样保存，保留动画
        actions.append({"file": fname, "name": stem})
    _save_manifest(aid, actions)
    state = load_index()
    n = len(state["avatars"]) + 1
    avatar_name = (name or f"形象{n}").strip()
    state["avatars"].append({
        "id": aid, "name": avatar_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    state["active_id"] = aid
    save_index(state)
    return {"id": aid, "name": avatar_name, "anim_count": len(actions)}


def save_single_upload(content: bytes, filename: str, name: str = "") -> dict:
    """单图上传 → 一个形象，1 个动作（待机位）"""
    aid = uuid.uuid4().hex[:8]
    adir = avatar_dir(aid)
    adir.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(filename)
    stem = Path(filename).stem.strip() or "形象"
    (adir / fname).write_bytes(content)
    _save_manifest(aid, [{"file": fname, "name": stem}])
    state = load_index()
    n = len(state["avatars"]) + 1
    avatar_name = (name or stem or f"形象{n}").strip()
    state["avatars"].append({
        "id": aid, "name": avatar_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    state["active_id"] = aid
    save_index(state)
    return {"id": aid, "name": avatar_name, "anim_count": 1}


# ==================== AI 生成动作（保留，覆盖手动动作）====================
def generate_animations(aid: str) -> dict:
    """AI 生成动作动图（APNG），覆盖该形象现有动作。源图取第一个动作文件的第一帧。"""
    adir = avatar_dir(aid)
    actions = _load_manifest(aid)
    if not actions:
        raise ValueError("形象没有动作，无法生成（请先上传动作图）")
    src_file = adir / actions[0]["file"]
    if not src_file.exists():
        raise ValueError("动作文件不存在")
    img = Image.open(src_file)
    try:
        img.seek(0)  # gif 取第一帧
    except Exception:
        pass
    transparent = remove_white_bg(img.convert("RGBA"))
    planned = plan_actions(transparent)
    new_actions = []
    for i, act in enumerate(planned):
        frames = make_frames(transparent, act)
        out_path = adir / f"ai_{i}.png"
        save_apng(frames, out_path)
        new_actions.append({"file": out_path.name, "name": act.get("name", "")})
    # 清理旧动作文件（非 ai_ 前缀）
    for a in actions:
        p = adir / a["file"]
        if p.exists() and not a["file"].startswith("ai_"):
            try:
                p.unlink()
            except Exception:
                pass
    _save_manifest(aid, new_actions)
    state = load_index()
    for a in state["avatars"]:
        if a["id"] == aid:
            a["anim_count"] = len(new_actions)
    save_index(state)
    return {"actions": new_actions}
