"""Live asset catalog: server-synced payouts, tradability, and asset kinds.

The venue publishes its instrument list over the ``instrument`` socket event;
between syncs the static :data:`cybertrade.constants.ASSET_CATALOG` keeps
payouts honest (paper mode and offline runs).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...constants import ASSET_CATALOG
from .models import QXAsset


@dataclass
class AssetCatalog:
    """Thread-safe instrument registry with payout + tradability lookups."""

    _assets: Dict[str, QXAsset] = field(default_factory=dict)
    synced_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- population --------------------------------------------------------
    def update(self, assets: List[QXAsset]) -> None:
        with self._lock:
            for a in assets:
                self._assets[a.name] = a
            self.synced_at = time.time()

    def upsert(self, asset: QXAsset) -> None:
        with self._lock:
            self._assets[asset.name] = asset

    @classmethod
    def from_static(cls) -> "AssetCatalog":
        """Seed from the bundled payout table (offline/paper default)."""
        cat = cls()
        for name, meta in ASSET_CATALOG.items():
            cat.upsert(QXAsset(
                name=name,
                asset_id=str(meta.get("id", name)),
                payout=float(meta.get("payout", 0.85)),
                open=True,
                is_otc=name.endswith("_otc") or str(meta.get("kind", "")).endswith("_otc"),
                kind=str(meta.get("kind", "")),
            ))
        return cat

    # -- queries -----------------------------------------------------------
    def get(self, name: str) -> Optional[QXAsset]:
        with self._lock:
            return self._assets.get(name)

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._assets)

    def __len__(self) -> int:
        with self._lock:
            return len(self._assets)

    def payout_for(self, name: str, default: float = 0.85) -> float:
        a = self.get(name)
        return a.payout if a else default

    def is_tradable(self, name: str) -> bool:
        """Venue open-flag only; session calendars stay with the survivor."""
        a = self.get(name)
        return bool(a.open) if a else True

    def kind_of(self, name: str) -> str:
        a = self.get(name)
        return a.kind if a else ""

    def by_kind(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        with self._lock:
            for a in self._assets.values():
                out.setdefault(a.kind or "unknown", []).append(a.name)
        return {k: sorted(v) for k, v in out.items()}

    def to_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": a.name,
                    "payout": a.payout,
                    "open": a.open,
                    "is_otc": a.is_otc,
                    "kind": a.kind,
                }
                for a in sorted(self._assets.values(), key=lambda x: x.name)
            ]


__all__ = ["AssetCatalog"]
