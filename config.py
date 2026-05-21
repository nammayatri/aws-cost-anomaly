import json
import os
from pathlib import Path

_DEFAULTS = {
    "aws_profile": None,
    "aws_region": "ap-south-1",
    "slack_bot_token": None,
    "slack_channel_id": None,
    "increase_pct_threshold": 10.0,
    "decrease_pct_threshold": 5.0,
    "abs_threshold": 1.0,
    "noise_floor": 1.0,
    "lookback_days": 21,
    "top_usage_types": 3,
    "mention": "",
}

_CAST = {
    "increase_pct_threshold": float,
    "decrease_pct_threshold": float,
    "abs_threshold": float,
    "noise_floor": float,
    "lookback_days": int,
    "top_usage_types": int,
}


def load(config_path: str | None = None) -> dict:
    cfg = dict(_DEFAULTS)

    path = Path(config_path) if config_path else Path(__file__).parent / "config.json"
    if path.exists():
        with path.open() as f:
            cfg.update(json.load(f))

    for key in cfg:
        env_val = os.environ.get(key.upper())
        if env_val is not None and env_val != "":
            cfg[key] = _CAST.get(key, str)(env_val)

    missing = [k for k in ("slack_bot_token", "slack_channel_id") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Missing required config keys: {missing}. Set via env or config.json")

    return cfg
