"""Risk limit structures and declarative checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LimitCheck:
    """Result of evaluating one limit."""

    name: str
    ok: bool
    value: float
    limit: float
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "value": round(self.value, 4),
            "limit": round(self.limit, 4),
            "message": self.message,
        }


@dataclass
class LimitBook:
    """Named collection of limits with evaluation state."""

    checks: List[LimitCheck] = field(default_factory=list)

    def add(self, check: LimitCheck) -> LimitCheck:
        self.checks.append(check)
        return check

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> List[LimitCheck]:
        return [c for c in self.checks if not c.ok]

    def first_failure(self) -> Optional[LimitCheck]:
        for c in self.checks:
            if not c.ok:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"all_ok": self.all_ok, "checks": [c.to_dict() for c in self.checks]}

    def clear(self) -> None:
        self.checks.clear()


@dataclass
class ExposureCaps:
    """Caps on simultaneous exposure."""

    max_concurrent: int = 3
    max_per_asset: int = 1
    max_currency_cluster: int = 2
    max_stake_sum: float = 150.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "max_per_asset": self.max_per_asset,
            "max_currency_cluster": self.max_currency_cluster,
            "max_stake_sum": self.max_stake_sum,
        }


__all__ = ["LimitCheck", "LimitBook", "ExposureCaps"]
