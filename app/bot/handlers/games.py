"""Points, shop, red packets, lotteries and auctions."""

from __future__ import annotations



from .games_shared import (  # noqa: F401
    AuctionDraft,
    LotteryDraft,
    _LOTTERY_CONDITION_LABELS,
    _LOTTERY_DRAFT_TTL,
    _active_auction_for_group,
    _auction_bid_card,
    _auction_bid_hint,
    _auction_body_text,
    _auction_code,
    _auction_creating,
    _auction_drafts,
    _auction_label,
    _auction_minimum,
    _auction_query_page_size,
    _beijing_time_text,
    _bounded_int,
    _claim_auction_slot,
    _claim_packet_slot,
    _feature_denied,
    _group_for_message,
    _is_group_admin,
    _lottery_condition_text,
    _lottery_creating,
    _lottery_draft_expired,
    _lottery_drafts,
    _lottery_draw_summary,
    _lottery_expiry_tasks,
    _lottery_history_page_size,
    _lottery_summary,
    _need_group,
    _open_auction_for_bid,
    _parse_lottery_draw_time,
    _promotion_tasks,
    _refresh_auction_deadline,
    _reply_feature_denied,
    _schedule_lottery_draft_expiry,
    _schedule_promotion,
    _send_lottery_step,
    _update_auction_message,
    log,
    router,
)

# Section modules register handlers on the shared router; keep this order.
from . import games_points, games_redpacket, games_lottery, games_auction  # noqa: F401
from .games_auction import (  # noqa: F401
    _cancel_auction,
    auction_cancel_created,
    auction_history,
    auction_setup_callback,
    cmd_auction,
    cmd_bid,
    cmd_bid_info,
    lottery_cancel,
)
from .games_points import (  # noqa: F401
    cmd_buy,
    cmd_checkin,
)
from .games_lottery import (  # noqa: F401
    cmd_lottery,
    game_private_config,
    lottery_cancel_created,
    lottery_history,
    lottery_management_callback,
    lottery_private_config,
    lottery_setup_callback,
    on_lottery_join,
)
from .games_redpacket import (  # noqa: F401
    on_claim_packet,
)
