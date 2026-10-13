"""Tests for coordinator modes and the activity feed."""
from datetime import timedelta

from custom_components.trello_todo.const import MODE_FEED, MODE_WEBHOOK

from .common import API, action, actions_url, calls, subentry_id

CARD = {"id": "c1", "name": "Buy stamps", "shortLink": "abc"}
ALEX_LIST = {"id": "l_alex", "name": "Alex"}


def _coordinator(entry, board_id="b1"):
    return entry.runtime_data.boards[subentry_id(entry, board_id)]


def _listen(coordinator):
    fired = []
    coordinator.processor.add_listener(lambda event_type, attrs: fired.append((event_type, attrs)))
    return fired


async def test_startup_runs_baseline_feed_poll(hass, loaded, mock_trello):
    assert len(calls(mock_trello, "GET", f"{API}/boards/b1/actions")) == 1
    assert _coordinator(loaded).processor.baselined is True


async def test_first_feed_poll_is_baseline_only(hass, load_entry, mock_trello):
    """Review Focus 2: activity from before startup never fires."""
    mock_trello.get(actions_url("b1"), payload=[action("old", "commentCard", card=CARD, list=ALEX_LIST, text="x")])
    entry = await load_entry()
    coordinator = _coordinator(entry)
    assert coordinator.processor.newest_id == "old"
    fired = _listen(coordinator)
    mock_trello.clear()
    mock_trello.get(actions_url("b1"), payload=[])
    await coordinator.async_poll_feed()
    assert fired == []


async def test_feed_fires_new_activity(hass, loaded, mock_trello):
    coordinator = _coordinator(loaded)
    fired = _listen(coordinator)
    mock_trello.clear()
    mock_trello.get(actions_url("b1"), payload=[action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="On it")])
    mock_trello.get(f"{API}/boards/b1", payload={}, repeat=True)  # refresh may follow; content irrelevant here
    await coordinator.async_poll_feed()
    assert [(t, a["comment"]) for t, a in fired] == [("comment_added", "On it")]


async def test_feed_uses_newest_id_as_since(hass, load_entry, mock_trello):
    mock_trello.get(actions_url("b1"), payload=[action("a9", "commentCard", card=CARD, list=ALEX_LIST, text="x")])
    entry = await load_entry()
    mock_trello.clear()
    mock_trello.get(actions_url("b1"), payload=[])
    await _coordinator(entry).async_poll_feed()
    req = calls(mock_trello, "GET", f"{API}/boards/b1/actions")[-1]  # clear() keeps recorded requests
    assert req.kwargs["params"]["since"] == "a9"


async def test_feed_failure_is_quiet(hass, loaded, mock_trello):
    coordinator = _coordinator(loaded)
    mock_trello.clear()
    mock_trello.get(actions_url("b1"), status=500)
    await coordinator.async_poll_feed()  # must not raise
    assert coordinator.last_update_success


async def test_modes_set_intervals(hass, loaded):
    coordinator = _coordinator(loaded)
    assert coordinator.mode == MODE_FEED
    assert coordinator.update_interval == timedelta(seconds=120)
    coordinator.set_mode(MODE_WEBHOOK)
    assert coordinator.update_interval == timedelta(minutes=15)
    assert coordinator._feed_unsub is None
    coordinator.set_mode(MODE_FEED)
    assert coordinator.update_interval == timedelta(seconds=120)
    assert coordinator._feed_unsub is not None


async def test_activity_context_has_me_done_list_and_names(hass, loaded):
    ctx = _coordinator(loaded)._activity_context()
    assert (ctx.me, ctx.done_list) == ("m1", "l_done")
    assert ctx.members == {"m1": "Sam", "m2": "Alex"}
    assert ctx.card_lists["c1"] == ("l_alex", "Alex")


async def test_set_mode_same_mode_is_quiet(hass, loaded):
    coordinator = _coordinator(loaded)
    updates = []
    remove = coordinator.async_add_listener(lambda: updates.append(1))
    coordinator.set_mode(coordinator.mode)
    remove()
    assert updates == []
