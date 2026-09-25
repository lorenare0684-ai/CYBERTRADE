"""Unofficial Quotex integration (website session + Socket.IO trading)."""

from __future__ import annotations

from .adapter import QuotexBroker
from .api import QuotexAPI
from .client import QuotexSocket
from .models import (
    QXAsset,
    QXBalance,
    QXCandle,
    QXOrderRequest,
    QXOrderResult,
    QXSession,
)
from . import constants, protocol

__all__ = [
    "QuotexAPI",
    "QuotexSocket",
    "QuotexBroker",
    "QXAsset",
    "QXBalance",
    "QXCandle",
    "QXOrderRequest",
    "QXOrderResult",
    "QXSession",
    "constants",
    "protocol",
]
