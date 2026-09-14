"""Risk controls: position caps, daily loss, kill switch, dry-run default."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_position_size: float = 20.0
    max_daily_loss: float = 50.0
    kill_switch: bool = True
    dry_run: bool = True


@dataclass
class RiskManager:
    config: RiskConfig = field(default_factory=RiskConfig)
    _realized_pnl: float = 0.0
    _day: date = field(default_factory=date.today)
    _killed: bool = False
    _kill_reason: Optional[str] = None

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._realized_pnl = 0.0
            # Kill switch stays engaged until explicitly reset.
            logger.info("risk: new trading day %s — daily PnL reset", today)

    @property
    def dry_run(self) -> bool:
        return self.config.dry_run

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def kill_reason(self) -> Optional[str]:
        return self._kill_reason

    @property
    def daily_pnl(self) -> float:
        self._roll_day()
        return self._realized_pnl

    def record_pnl(self, amount: float) -> None:
        """Accumulate realized PnL; trip kill switch on max daily loss."""
        self._roll_day()
        self._realized_pnl += float(amount)
        if (
            self.config.kill_switch
            and self._realized_pnl <= -abs(self.config.max_daily_loss)
        ):
            self.engage_kill_switch(
                f"daily loss {self._realized_pnl:.4f} <= -{self.config.max_daily_loss}"
            )

    def engage_kill_switch(self, reason: str) -> None:
        self._killed = True
        self._kill_reason = reason
        logger.error("KILL SWITCH engaged: %s", reason)

    def reset_kill_switch(self) -> None:
        self._killed = False
        self._kill_reason = None
        logger.warning("kill switch reset by operator")

    def clamp_size(self, requested: float) -> float:
        return max(0.0, min(float(requested), float(self.config.max_position_size)))

    def allow_trade(self, size: float) -> tuple[bool, str]:
        """Gate a proposed trade. Returns (ok, reason)."""
        self._roll_day()
        if self._killed:
            return False, f"kill_switch: {self._kill_reason}"
        if size <= 0:
            return False, "size <= 0"
        if size > self.config.max_position_size:
            return (
                False,
                f"size {size} exceeds max_position_size {self.config.max_position_size}",
            )
        if (
            self.config.kill_switch
            and self._realized_pnl <= -abs(self.config.max_daily_loss)
        ):
            self.engage_kill_switch(
                f"daily loss {self._realized_pnl:.4f} hit limit"
            )
            return False, f"kill_switch: {self._kill_reason}"
        return True, "ok"
