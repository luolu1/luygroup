"""Dispatcher scenarios for auction setup ownership, lifetime, and stage controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Chat, Message
from sqlalchemy import select

from app.bot.handlers import games
from app.db import session_scope
from app.models import Auction, utcnow
from auction_dispatcher_fixture import Harness, Scene
from auction_dispatcher_transport import callbacks


async def description_stage(harness: Harness) -> tuple[Scene, Message]:
    scene = await harness.seed(f"SETUP{harness.serial + 1}")
    await harness.say(scene.group_id, scene.first, "/jp")
    await harness.say(scene.first.id, scene.first, "Synthetic keepsake")
    assert games._auction_drafts[scene.first.id].step == "description"
    return scene, harness.private_prompt(scene.first)


async def test_skip_is_visible_when_description_is_requested(harness: Harness) -> None:
    _scene, prompt = await description_stage(harness)
    assert {"aucsetup:skip:description", "aucsetup:back:item"} <= callbacks(prompt.reply_markup), prompt.reply_markup


async def test_skip_advances_with_empty_description_when_current(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    before = games._auction_drafts[scene.first.id]
    await harness.click(scene.first, prompt, "aucsetup:skip:description")
    draft = games._auction_drafts[scene.first.id]
    assert draft.description == "" and draft.step == "auction_type", draft
    assert draft.created_at == before.created_at
    assert "aucsetup:skip:description" not in callbacks(harness.private_prompt(scene.first).reply_markup)


async def test_type_selection_rejects_when_description_is_unfinished(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    before = games._auction_drafts[scene.first.id]
    requests = await harness.click(scene.first, prompt, "aucsetup:type:currency")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_wrong_user_clicks(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    before = games._auction_drafts[scene.first.id]
    requests = await harness.click(scene.second, prompt, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_wrong_chat_clicks(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    before = games._auction_drafts[scene.first.id]
    wrong_chat = prompt.model_copy(update={"chat": Chat(id=scene.group_id, type="supergroup")})
    requests = await harness.click(scene.first, wrong_chat, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_draft_is_expired(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    games._auction_drafts[scene.first.id] = replace(
        games._auction_drafts[scene.first.id], created_at=utcnow() - timedelta(minutes=5)
    )
    requests = await harness.click(scene.first, prompt, "aucsetup:skip:description")
    assert scene.first.id not in games._auction_drafts
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_draft_is_missing(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    games._auction_drafts.pop(scene.first.id)
    requests = await harness.click(scene.first, prompt, "aucsetup:skip:description")
    assert scene.first.id not in games._auction_drafts
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_wrong_step_clicks(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    await harness.say(scene.first.id, scene.first, "A nonempty description")
    before = games._auction_drafts[scene.first.id]
    requests = await harness.click(scene.first, prompt, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_old_prompt_is_replayed_after_back(harness: Harness) -> None:
    scene, old_prompt = await description_stage(harness)
    await harness.click(scene.first, old_prompt, "aucsetup:back:item")
    await harness.say(scene.first.id, scene.first, "Revised keepsake")
    before = games._auction_drafts[scene.first.id]
    requests = await harness.click(scene.first, old_prompt, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_old_draft_prompt_is_replayed(harness: Harness) -> None:
    scene, old_prompt = await description_stage(harness)
    await harness.say(scene.group_id, scene.first, "/jp")
    await harness.say(scene.first.id, scene.first, "New draft keepsake")
    before = games._auction_drafts[scene.first.id]
    requests = await harness.click(scene.first, old_prompt, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def test_skip_rejects_when_successful_callback_is_replayed(harness: Harness) -> None:
    scene, prompt = await description_stage(harness)
    await harness.click(scene.first, prompt, "aucsetup:skip:description")
    before = games._auction_drafts[scene.first.id]
    assert before.step == "auction_type"
    requests = await harness.click(scene.first, prompt, "aucsetup:skip:description")
    assert games._auction_drafts[scene.first.id] == before
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests)


async def complete_setup(harness: Harness, *, auction_type: str, skip: bool) -> tuple[Scene, Auction, SendMessage]:
    scene, prompt = await description_stage(harness)
    started = games._auction_drafts[scene.first.id].created_at
    if skip:
        await harness.click(scene.first, prompt, "aucsetup:skip:description")
    else:
        await harness.say(scene.first.id, scene.first, "Preserved description <&>")
    assert games._auction_drafts[scene.first.id].step == "auction_type"
    await harness.click(scene.first, harness.private_prompt(scene.first), f"aucsetup:type:{auction_type}")
    for text, step in (("100", "step"), ("10", "minutes"), ("30", "extend"), ("2", "confirm")):
        await harness.say(scene.first.id, scene.first, text)
        draft = games._auction_drafts[scene.first.id]
        assert draft.step == step and draft.created_at == started
    requests = await harness.click(scene.first, harness.private_prompt(scene.first), "aucsetup:create")
    async with session_scope() as session:
        auction = await session.scalar(select(Auction).where(Auction.creator_id == scene.first.id))
        assert auction is not None
        assert auction.description == ("" if skip else "Preserved description <&>")
        assert auction.auction_type == auction_type
        assert (auction.start_price, auction.step, auction.extend_minutes) == (100, 10, 2)
    post = next(request for request in requests if isinstance(request, SendMessage) and request.chat_id == scene.group_id)
    return scene, auction, post


async def test_skipped_description_creates_when_seven_steps_finish(harness: Harness) -> None:
    _scene, auction, post = await complete_setup(harness, auction_type="points", skip=True)
    assert {f"bid:{auction.id}:100", f"aucquery:{auction.id}"} <= callbacks(post.reply_markup)


async def test_nonempty_description_and_controls_survive_when_created(harness: Harness) -> None:
    _scene, auction, post = await complete_setup(harness, auction_type="points", skip=False)
    assert "Preserved description &lt;&amp;&gt;" in post.text
    assert {f"bid:{auction.id}:100", f"aucquery:{auction.id}"} <= callbacks(post.reply_markup)


async def test_currency_creation_uses_cny_when_buttons_are_emitted(harness: Harness) -> None:
    _scene, auction, post = await complete_setup(harness, auction_type="currency", skip=False)
    assert post.reply_markup is not None
    button = next(button for row in post.reply_markup.inline_keyboard for button in row if button.callback_data == f"bid:{auction.id}:100")
    assert "100 CNY" in button.text, button.text
    assert f"aucquery:{auction.id}" in callbacks(post.reply_markup)
