import json
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "tcc-g15"
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS: dict[str, Any] = {
    "bind_addr": "0.0.0.0",
    "web_port": 8080,
    "web_enabled": False,
    "auth_enabled": False,
    "auth_user": "admin",
    "auth_pass": "",
}


def load_config() -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in _DEFAULTS:
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
