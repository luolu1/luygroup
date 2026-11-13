"""Shared auction pricing rules used by bot handlers and the scheduler."""
from __future__ import annotations

from ..models import Auction

CURRENCY_ENTRY_FEE_POINTS = 10


def price_label(auction: Auction) -> str:
    if getattr(auction, "auction_type", "points") == "currency":
        return "CNY"
    return "积分"


def entry_fee(auction: Auction) -> int:
    if getattr(auction, "auction_type", "points") == "currency":
        return CURRENCY_ENTRY_FEE_POINTS
    return int(getattr(auction, "entry_fee", 0) or 0)
