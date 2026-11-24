"""Settings and history (JSON in the remover data folder)."""
import json
import os
import threading
import time

from . import paths

DEFAULTS = {
    "theme": "system",          # system | dark | light
    "accent": "blue",           # blue | violet | green | orange | pink
    "delete_mode": "quarantine",  # quarantine | delete
    "quarantine_days": 14,
    "default_level": "moderate",  # safe | moderate | advanced
    "restore_point": True,
    "quiet_default": False,
    "show_store": True,
    "animations": True,
    "exclusions": [],           # paths that are never touched
}
_lock = threading.Lock()


def _file():
    return paths.data_path("settings.json")


def load():
    try:
        with open(_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(changes):
    with _lock:
        cur = load()
        for k, v in (changes or {}).items():
            if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                cur[k] = v
        with open(_file(), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        return cur


# ---------------------------------------------------------------- history
def log(action, title, detail=None):
    rec = {"time": time.time(), "action": action, "title": title, "detail": detail or {}}
    with _lock:
        with open(paths.data_path("history.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def history(limit=300):
    try:
        with open(paths.data_path("history.jsonl"), encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def clear_history():
    try:
        os.remove(paths.data_path("history.jsonl"))
    except OSError:
        pass
