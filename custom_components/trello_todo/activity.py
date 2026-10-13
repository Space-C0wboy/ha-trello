"""Trello actions → Home Assistant events. No Home Assistant imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COMMENT_MAX = 1000

# Action types requested from the activity feed; webhooks deliver every type and the rest are ignored.
ACTION_TYPES: tuple[str, ...] = (
    "createCard",
    "copyCard",
    "convertToCardFromCheckItem",
    "moveCardToBoard",
    "updateCard",
    "deleteCard",
    "commentCard",
    "addMemberToCard",
    "removeMemberFromCard",
    "addLabelToCard",
    "removeLabelFromCard",
    "updateCheckItemStateOnCard",
    "addAttachmentToCard",
)

EVENT_TYPES: list[str] = [
    "card_created",
    "card_moved",
    "card_archived",
    "card_unarchived",
    "due_changed",
    "due_completed",
    "card_updated",
    "card_deleted",
    "comment_added",
    "member_added",
    "member_removed",
    "label_added",
    "label_removed",
    "checklist_item_checked",
    "attachment_added",
]

_SIMPLE = {
    "createCard": "card_created",
    "copyCard": "card_created",
    "convertToCardFromCheckItem": "card_created",
    "moveCardToBoard": "card_created",
    "deleteCard": "card_deleted",
    "commentCard": "comment_added",
    "addMemberToCard": "member_added",
    "removeMemberFromCard": "member_removed",
    "addLabelToCard": "label_added",
    "removeLabelFromCard": "label_removed",
    "updateCheckItemStateOnCard": "checklist_item_checked",
    "addAttachmentToCard": "attachment_added",
}


@dataclass(frozen=True)
class ActionContext:
    """What mapping needs from the board: who "me" is, the done list, and member names."""

    me: str | None
    done_list: str | None
    members: dict[str, str] = field(default_factory=dict)


def _ref(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"id": obj.get("id"), "name": obj.get("name")} if obj else None


def _classify_update(data: dict[str, Any]) -> str | None:
    """updateCard covers several changes; the first match wins (spec section 1)."""
    old = data.get("old") or {}
    card = data.get("card") or {}
    if data.get("listBefore") and data.get("listAfter"):
        return "card_moved"
    if "closed" in old:
        return "card_archived" if card.get("closed") else "card_unarchived"
    if "due" in old:
        return "due_changed"
    if "dueComplete" in old:
        return "due_completed"
    if "name" in old or "desc" in old:
        return "card_updated"
    return None


def map_action(action: dict[str, Any], ctx: ActionContext) -> tuple[str, dict[str, Any]] | None:
    """One Trello action → (event_type, attributes), or None if it isn't an event."""
    kind = action.get("type")
    data = action.get("data") or {}
    event_type = _classify_update(data) if kind == "updateCard" else _SIMPLE.get(kind or "")
    if event_type is None:
        return None
    card = data.get("card") or {}
    current_list = data.get("listAfter") or data.get("list") or {}
    creator = action.get("memberCreator") or {}
    creator_id = action.get("idMemberCreator") or creator.get("id")
    attrs: dict[str, Any] = {
        "card_id": card.get("id"),
        "card_name": card.get("name"),
        "card_url": f"https://trello.com/c/{card['shortLink']}" if card.get("shortLink") else None,
        "list_id": current_list.get("id"),
        "list_name": current_list.get("name"),
        "by": {
            "id": creator_id,
            "name": creator.get("fullName") or ctx.members.get(creator_id or ""),
            "initials": creator.get("initials"),
        },
        "by_me": bool(creator_id) and creator_id == ctx.me,
        "action_id": action.get("id"),
        "date": action.get("date"),
    }
    match event_type:
        case "card_moved":
            to_list = data.get("listAfter") or {}
            attrs |= {
                "from_list": _ref(data.get("listBefore")),
                "to_list": _ref(to_list),
                "to_done_list": ctx.done_list is not None and to_list.get("id") == ctx.done_list,
            }
        case "due_changed":
            attrs |= {"due": card.get("due"), "old_due": (data.get("old") or {}).get("due")}
        case "due_completed":
            attrs["due_complete"] = bool(card.get("dueComplete"))
        case "comment_added":
            text = data.get("text") or ""
            attrs["comment"] = text if len(text) <= COMMENT_MAX else text[:COMMENT_MAX] + "…"
        case "member_added" | "member_removed":
            member = action.get("member") or {}
            member_id = data.get("idMember") or member.get("id")
            attrs |= {
                "member": {"id": member_id, "name": member.get("fullName") or ctx.members.get(member_id or "")},
                "for_me": bool(member_id) and member_id == ctx.me,
            }
        case "label_added" | "label_removed":
            label = data.get("label") or {}
            attrs["label"] = {"name": label.get("name"), "color": label.get("color")}
        case "checklist_item_checked":
            item = data.get("checkItem") or {}
            attrs |= {
                "item": item.get("name"),
                "checked": item.get("state") == "complete",
                "checklist": (data.get("checklist") or {}).get("name"),
            }
        case "attachment_added":
            attrs["attachment"] = (data.get("attachment") or {}).get("name")
    return event_type, attrs
