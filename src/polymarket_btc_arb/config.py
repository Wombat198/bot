"""Load YAML config + environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULTS: dict[str, Any] = {
    "strategy": {
        "market": "BTC_UP_DOWN",
        "timeframe": "15m",
        "min_edge": 0.02,
    },
    "risk": {
        "max_position_size": 20,
        "max_daily_loss": 50,
        "kill_switch": True,
    },
    "execution": {
        "dry_run": True,
        "require_both_legs": True,
        "prefer_maker": True,
    },
    "agents": {
        "watcher": True,
        "executor": True,
        "merger": True,
    },
    "poll": {
        "interval_sec": 2.0,
        "smoke_max_cycles": 0,
    },
    "apis": {
        "gamma_host": "https://gamma-api.polymarket.com",
        "clob_host": "https://clob.polymarket.com",
        "ws_market": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv()
    cfg = dict(DEFAULTS)
    cfg_path = path or os.environ.get("POLYMARKET_CONFIG")
    if cfg_path:
        p = Path(cfg_path)
        if p.is_file():
            with p.open() as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"config root must be a mapping: {p}")
            cfg = _deep_merge(cfg, loaded)

    # Env overrides for safety-critical flags
    dry = os.environ.get("POLYMARKET_DRY_RUN")
    if dry is not None:
        cfg["execution"]["dry_run"] = dry.strip().lower() in ("1", "true", "yes")

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    cfg["_env"] = {
        "private_key": pk or None,
        "funder": os.environ.get("POLYMARKET_FUNDER") or None,
        "signature_type": int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0")),
        "chain_id": int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
        "host": os.environ.get("POLYMARKET_HOST", cfg["apis"]["clob_host"]),
    }

    # Force dry_run if no key when someone tries to go live without secrets
    if not cfg["execution"]["dry_run"] and not pk:
        cfg["execution"]["dry_run"] = True
        cfg["_forced_dry_run"] = True
    return cfg
