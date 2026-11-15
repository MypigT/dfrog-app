"""模型配置：每个功能页面可查看/修改「模型名 + Key」。

Key 两种模式：
- env：key_value 填环境变量名，运行时从 os.environ 读取（改环境变量即生效）。
- direct：key_value 直接填 Key 值。

配置存 DATA_DIR/model_config.json。模型调用处用 resolve_key(feature) 取生效 Key。
"""

import json
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", Path(sys.executable).parent)) / "dfrog"
else:
    DATA_DIR = Path(__file__).parent.parent / "data"

CONFIG_PATH = DATA_DIR / "model_config.json"

# 各功能默认配置：模型名 + Key 模式 + Key 值（默认走环境变量）
DEFAULTS = {
    "chat":   {"model": "deepseek-chat",       "key_mode": "env",  "key_value": "DEEPSEEK_API_KEY"},
    "agent":  {"model": "deepseek-chat",       "key_mode": "env",  "key_value": "DEEPSEEK_API_KEY"},
    "vision": {"model": "qwen3.8-max-0902",    "key_mode": "env",  "key_value": "DASHSCOPE_API_KEY"},
    "petimg": {"model": "qwen3.8-max-0902",    "key_mode": "env",  "key_value": "DASHSCOPE_API_KEY"},
    "notes":  {"model": "deepseek-chat",       "key_mode": "env",  "key_value": "DEEPSEEK_API_KEY"},
}


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # 合并默认值（新功能补默认）
            for k, v in DEFAULTS.items():
                if k not in data:
                    data[k] = dict(v)
            return data
    except Exception:
        pass
    return {k: dict(v) for k, v in DEFAULTS.items()}


def save_config(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_feature_config(feature: str) -> dict:
    cfg = load_config()
    return cfg.get(feature) or dict(DEFAULTS.get(feature, {"model": "", "key_mode": "env", "key_value": ""}))


def set_feature_config(feature: str, model: str, key_mode: str, key_value: str):
    cfg = load_config()
    cfg[feature] = {"model": model, "key_mode": key_mode, "key_value": key_value}
    save_config(cfg)
    return cfg[feature]


def resolve_key(feature: str) -> str:
    """取生效 Key：env 模式读环境变量，direct 模式直接用值。"""
    c = get_feature_config(feature)
    if c.get("key_mode") == "direct":
        return c.get("key_value", "")
    return os.environ.get(c.get("key_value", ""), "")
