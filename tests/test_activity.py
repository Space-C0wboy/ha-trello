"""Tests for mapping Trello actions to events."""
import pytest

from custom_components.trello_todo.activity import EVENT_TYPES, ActionContext, map_action

from .common import action

CTX = ActionContext(me="m1", done_list="l_done", members={"m1": "Sam", "m2": "Alex"})
CARD = {"id": "c1", "name": "Buy stamps", "shortLink": "abc"}
ALEX_LIST = {"id": "l_alex", "name": "Alex"}
DONE_LIST = {"id": "l_done", "name": "Done & Archive"}


def test_event_types_complete():
    assert EVENT_TYPES == [
        "card_created", "card_moved", "card_archived", "card_unarchived", "due_changed", "due_completed",
        "card_updated", "card_deleted", "comment_added", "member_added", "member_removed", "label_added",
        "label_removed", "checklist_item_checked", "attachment_added",
    ]


def test_common_attributes():
    event_type, attrs = map_action(action("a1", "createCard", card=CARD, list=ALEX_LIST), CTX)
    assert event_type == "card_created"
    assert attrs == {
        "card_id": "c1",
        "card_name": "Buy stamps",
        "card_url": "https://trello.com/c/abc",
        "list_id": "l_alex",
        "list_name": "Alex",
        "by": {"id": "m2", "name": "Alex", "initials": "A"},
        "by_me": False,
        "action_id": "a1",
        "date": "2026-10-13T12:00:00.000Z",
    }


def test_by_me():
    _, attrs = map_action(action("a1", "createCard", by="m1", card=CARD, list=ALEX_LIST), CTX)
    assert attrs["by_me"] is True
    assert attrs["by"]["name"] == "Sam"


@pytest.mark.parametrize("kind", ["copyCard", "convertToCardFromCheckItem", "moveCardToBoard"])
def test_other_creations(kind):
    assert map_action(action("a1", kind, card=CARD, list=ALEX_LIST), CTX)[0] == "card_created"


def test_card_moved_to_done_list():
    event_type, attrs = map_action(
        action("a1", "updateCard", card=CARD, old={"idList": "l_alex"}, listBefore=ALEX_LIST, listAfter=DONE_LIST), CTX
    )
    assert event_type == "card_moved"
    assert attrs["from_list"] == {"id": "l_alex", "name": "Alex"}
    assert attrs["to_list"] == {"id": "l_done", "name": "Done & Archive"}
    assert attrs["to_done_list"] is True
    assert (attrs["list_id"], attrs["list_name"]) == ("l_done", "Done & Archive")


def test_card_moved_elsewhere():
    sam = {"id": "l_sam", "name": "Sam"}
    _, attrs = map_action(
        action("a1", "updateCard", card=CARD, old={"idList": "l_alex"}, listBefore=ALEX_LIST, listAfter=sam), CTX
    )
    assert attrs["to_done_list"] is False


@pytest.mark.parametrize(
    ("old", "card_extra", "expected"),
    [
        ({"closed": False}, {"closed": True}, "card_archived"),
        ({"closed": True}, {"closed": False}, "card_unarchived"),
        ({"due": None}, {"due": "2026-10-20T05:00:00.000Z"}, "due_changed"),
        ({"dueComplete": False}, {"dueComplete": True}, "due_completed"),
        ({"name": "Old"}, {}, "card_updated"),
        ({"desc": ""}, {}, "card_updated"),
    ],
)
def test_update_card_classification(old, card_extra, expected):
    assert map_action(action("a1", "updateCard", card={**CARD, **card_extra}, old=old, list=ALEX_LIST), CTX)[0] == expected


def test_due_changed_attributes():
    _, attrs = map_action(
        action("a1", "updateCard", card={**CARD, "due": "2026-10-20T05:00:00.000Z"},
               old={"due": "2026-10-18T05:00:00.000Z"}, list=ALEX_LIST),
        CTX,
    )
    assert attrs["due"] == "2026-10-20T05:00:00.000Z"
    assert attrs["old_due"] == "2026-10-18T05:00:00.000Z"


def test_due_completed_attribute():
    _, attrs = map_action(
        action("a1", "updateCard", card={**CARD, "dueComplete": True}, old={"dueComplete": False}, list=ALEX_LIST), CTX
    )
    assert attrs["due_complete"] is True


def test_move_wins_over_other_changes():
    _, attrs = map_action(
        action("a1", "updateCard", card=CARD, old={"idList": "l_alex", "name": "x"}, listBefore=ALEX_LIST,
               listAfter=DONE_LIST),
        CTX,
    )
    assert "to_list" in attrs


def test_position_only_update_is_ignored():
    assert map_action(action("a1", "updateCard", card=CARD, old={"pos": 1}, list=ALEX_LIST), CTX) is None


def test_unknown_action_is_ignored():
    assert map_action(action("a1", "updateBoard", board={"id": "b1"}), CTX) is None


def test_card_deleted():
    event_type, attrs = map_action(action("a1", "deleteCard", card={"id": "c1", "idShort": 7}, list=ALEX_LIST), CTX)
    assert event_type == "card_deleted"
    assert attrs["card_name"] is None
    assert attrs["card_url"] is None


def test_comment_truncated():
    _, attrs = map_action(action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="x" * 1500), CTX)
    assert len(attrs["comment"]) == 1001
    assert attrs["comment"].endswith("…")


def test_short_comment_kept():
    _, attrs = map_action(action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="On it"), CTX)
    assert attrs["comment"] == "On it"


def test_member_added_for_me():
    event_type, attrs = map_action(
        action("a1", "addMemberToCard", card=CARD, list=ALEX_LIST, idMember="m1",
               member={"id": "m1", "fullName": "Sam"}),
        CTX,
    )
    assert event_type == "member_added"
    assert attrs["member"] == {"id": "m1", "name": "Sam"}
    assert attrs["for_me"] is True


def test_member_removed_uses_board_names():
    event_type, attrs = map_action(action("a1", "removeMemberFromCard", card=CARD, list=ALEX_LIST, idMember="m2"), CTX)
    assert event_type == "member_removed"
    assert attrs["member"] == {"id": "m2", "name": "Alex"}
    assert attrs["for_me"] is False


@pytest.mark.parametrize(("kind", "expected"), [("addLabelToCard", "label_added"), ("removeLabelFromCard", "label_removed")])
def test_labels(kind, expected):
    event_type, attrs = map_action(
        action("a1", kind, card=CARD, list=ALEX_LIST, label={"id": "lb1", "name": "Urgent", "color": "red"}), CTX
    )
    assert event_type == expected
    assert attrs["label"] == {"name": "Urgent", "color": "red"}


def test_checklist_item_checked():
    event_type, attrs = map_action(
        action("a1", "updateCheckItemStateOnCard", card=CARD, list=ALEX_LIST,
               checkItem={"id": "ci", "name": "Milk", "state": "complete"}, checklist={"id": "cl", "name": "Groceries"}),
        CTX,
    )
    assert event_type == "checklist_item_checked"
    assert (attrs["item"], attrs["checked"], attrs["checklist"]) == ("Milk", True, "Groceries")


def test_attachment_added():
    event_type, attrs = map_action(
        action("a1", "addAttachmentToCard", card=CARD, list=ALEX_LIST, attachment={"id": "at", "name": "photo.jpg"}), CTX
    )
    assert (event_type, attrs["attachment"]) == ("attachment_added", "photo.jpg")
