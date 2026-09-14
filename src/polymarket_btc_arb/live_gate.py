"""Live-mode gating: require explicit risk ack + key + py-clob-client."""

from __future__ import annotations

from typing import Any, Optional


def _py_clob_importable() -> bool:
    try:
        import py_clob_client  # noqa: F401

        return True
    except ImportError:
        return False


def resolve_dry_run(
    args: Any,
    cfg: dict,
    *,
    py_clob_ok: Optional[bool] = None,
) -> tuple[bool, str]:
    """Decide dry_run vs live from CLI flags + env + optional deps.

    Returns (dry_run, reason). Live only when ALL of:
      (a) --live and --i-understand-risk
      (b) POLYMARKET_PRIVATE_KEY present in cfg["_env"]
      (c) py-clob-client importable
    Otherwise force dry_run and explain why.
    """
    want_live = bool(getattr(args, "live", False))
    if not want_live:
        return True, "dry_run (no --live flag; config dry_run left as safety default)"

    if not bool(getattr(args, "i_understand_risk", False)):
        return True, "forced dry_run: --live requires --i-understand-risk"

    env = cfg.get("_env") or {}
    pk = env.get("private_key")
    if not pk:
        return True, "forced dry_run: POLYMARKET_PRIVATE_KEY not set in env/.env"

    ok = _py_clob_importable() if py_clob_ok is None else bool(py_clob_ok)
    if not ok:
        return True, "forced dry_run: py-clob-client not importable (pip install -e '.[live]')"

    return False, "LIVE enabled (--live + --i-understand-risk + key + py-clob-client)"
