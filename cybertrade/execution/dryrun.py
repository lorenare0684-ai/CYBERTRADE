"""Dry-run broker: paper fills on live venue quotes.

`BrokerConfig.mode == "dryrun"` builds one of these.  Prices, payouts, and
the instrument catalog come from the real venue when a session exists;
fills, settlement, and P&L are simulated locally and **no order ever
reaches the venue** — the attached
:class:`~cybertrade.brokers.quotex.adapter.QuotexBroker` is constructed
with ``allow_orders=False`` and this class never calls it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..utils.mathx import clamp
from .paper import PaperBroker

log = logging.getLogger("cybertrade.dryrun")


class DryRunBroker(PaperBroker):
    """PaperBroker whose hurdle quotes come from a live venue facade.

    ``venue`` is any object with ``payout_for(asset, expiry_seconds)`` —
    normally the order-disabled :class:`QuotexBroker`.  When it also
    exposes ``.api``, engine boot wires live tick/instrument streaming
    into the normal pipeline while execution stays purely local.
    """

    def __init__(self, venue: Optional[Any] = None, **paper_kwargs: Any) -> None:
        super().__init__(**paper_kwargs)
        self.venue = venue
        api = getattr(venue, "api", None)
        if api is not None:
            self.api = api  # engine boot() streams live ticks through this

    @property
    def name(self) -> str:
        return "PAPER-DRY"

    def payout_for(self, asset: str, expiry_seconds: int) -> float:
        if self.venue is not None:
            try:
                q = float(self.venue.payout_for(asset, expiry_seconds))
                return clamp(q, 0.5, 0.95)
            except Exception:  # noqa: BLE001 — quote trouble must not stop paper
                log.exception("venue payout quote failed — paper fallback")
        return super().payout_for(asset, expiry_seconds)
