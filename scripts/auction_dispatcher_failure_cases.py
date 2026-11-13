"""Post-commit Telegram failures must not hide accepted auction bids."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage

from auction_dispatcher_bid_cases import assert_card
from auction_dispatcher_fixture import Harness
from auction_dispatcher_transport import AuctionRequest, bid_cards


class WarningRecords(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def warnings() -> Iterator[list[logging.LogRecord]]:
    handler = WarningRecords()
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


async def test_card_survives_when_original_edit_fails(harness: Harness) -> None:
    scene = await harness.seed("FAILEDIT")
    harness.session.fail_next.add("editMessageText")
    with warnings() as records:
        requests = await harness.say(scene.group_id, scene.first, "/bid FAILEDIT 150")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 150),) and dict(state.balances)[scene.first.id] == 350
    cards = bid_cards(requests)
    assert len(cards) == 1
    assert_card(scene, cards[0], state)
    assert requests.index(cards[0]) < next(index for index, request in enumerate(requests) if isinstance(request, EditMessageText))
    assert len([record for record in records if record.name == "app.bot.handlers.games"]) == 1, records


async def test_card_survives_when_callback_acknowledgement_fails(harness: Harness) -> None:
    scene = await harness.seed("FAILACK")
    harness.session.fail_next.add("answerCallbackQuery")
    with warnings() as records:
        requests = await harness.click(scene.first, scene.original, f"bid:{scene.auction.id}:100")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 100),) and dict(state.balances)[scene.first.id] == 400
    cards = bid_cards(requests)
    assert len(cards) == 1
    assert_card(scene, cards[0], state)
    assert requests.index(cards[0]) < next(index for index, request in enumerate(requests) if isinstance(request, AnswerCallbackQuery))
    assert any(isinstance(request, EditMessageText) for request in requests)
    assert len([record for record in records if record.name == "app.bot.handlers.games"]) == 1, records


async def test_card_survives_when_command_delete_fails(harness: Harness) -> None:
    scene = await harness.seed("FAILDEL")
    harness.session.fail_next.add("deleteMessage")
    with warnings() as records:
        requests = await harness.say(scene.group_id, scene.first, "/bid FAILDEL 150")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 150),)
    cards = bid_cards(requests)
    assert len(cards) == 1
    assert_card(scene, cards[0], state)
    assert requests.index(cards[0]) < next(index for index, request in enumerate(requests) if isinstance(request, DeleteMessage))
    assert len([record for record in records if record.name == "app.bot.helpers"]) == 1, records


async def test_card_survives_when_command_receipt_send_fails(harness: Harness) -> None:
    scene = await harness.seed("FAILNOTE")

    async def fail_receipt(request: AuctionRequest) -> None:
        if isinstance(request, SendMessage) and request.text.startswith("✅"):
            raise TelegramBadRequest(method=request, message="synthetic receipt failure")

    harness.session.on_request = fail_receipt
    with warnings() as records:
        requests = await harness.say(scene.group_id, scene.first, "/bid FAILNOTE 150")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 150),)
    cards = bid_cards(requests)
    assert len(cards) == 1
    assert_card(scene, cards[0], state)
    assert any(isinstance(request, EditMessageText) for request in requests)
    assert len([record for record in records if record.name == "app.bot.handlers.games"]) == 1, records


async def test_edit_refreshes_price_and_bidder_together_when_new_bid_interleaves(harness: Harness) -> None:
    scene = await harness.seed("FRESHPAIR")

    async def interleave(request: AuctionRequest) -> None:
        if isinstance(request, AnswerCallbackQuery):
            harness.session.on_request = None
            await harness.say(scene.group_id, scene.second, "/bid FRESHPAIR 180")

    harness.session.on_request = interleave
    requests = await harness.click(scene.first, scene.original, f"bid:{scene.auction.id}:100")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 100), (scene.second.id, 180))
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 320}
    edits = [request for request in requests if isinstance(request, EditMessageText)]
    assert edits and f"@{scene.second.username}</a>" in edits[-1].text and "180 积分" in edits[-1].text, edits
    assert f"@{scene.first.username}</a>" not in edits[-1].text, edits[-1].text
