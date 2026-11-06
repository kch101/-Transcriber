"""Настройки приложения.

Порядок: значения по умолчанию -> config.json рядом с этим файлом (не хранится в git)
-> переменные окружения. Токен Hugging Face ищется так: HF_TOKEN -> config.json
-> токен, сохранённый командой `hf auth login`.
"""
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_FILE = APP_DIR / "config.json"

DEFAULTS = {
    "hf_token": "",
    "device": "cpu",
    "compute_type": "int8",
    "model_size": "large-v3",
    "batch_size": 8,
    "threads": 4,
    "default_language": "ru",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if os.environ.get("HF_TOKEN"):
        cfg["hf_token"] = os.environ["HF_TOKEN"]
    if not cfg.get("hf_token"):
        try:
            from huggingface_hub import get_token
            cfg["hf_token"] = get_token() or ""
        except Exception:
            pass
    return cfg
