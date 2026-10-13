> ⚠️ **Personal project — not a professional developer.** These integrations were built for personal use via AI-assisted "vibe coding" (Claude). They work for my setup but may have rough edges. Use at your own risk, PRs welcome, and please be kind in issues.

# Trello — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A [HACS](https://hacs.xyz/) custom integration for [Trello](https://trello.com/) that brings your boards into Home Assistant: every list on the boards you pick becomes a native **to-do list**. You can use it from the To-do panel, dashboards, automations and Assist.

---

## Features

- **Sign in with Atlassian (OAuth 2.0):** no API keys or tokens to paste. Home Assistant keeps the sign-in alive by itself.
- **Several boards per sign-in:** pick the boards you want, and add or remove boards later.
- **Every list is a to-do list:** new lists appear automatically, and archived lists disappear.
- **Check items off:** each board decides what checking off does: move the card to a done list, archive it, or both. Cards in the done list show as completed.
- **Edit from Home Assistant:** add cards, rename them, set due dates and descriptions, and re-order them.
- **Safe deletes:** deleting an item archives the card. Nothing is ever hard-deleted.
- **Actions for automations:** create, move, copy, archive, comment, label, assign, checklists and due dates, using names rather than ids.
- **Trello events for automations:** comments, moves, due dates, assignments and more fire events on an `event` entity per board — within seconds with Nabu Casa or a public HTTPS URL, within a minute otherwise.
- **Dashboard-friendly attributes:** card names, due dates, labels and members are available in state attributes.
- **No extra dependencies:** uses only `aiohttp`, which is already bundled with Home Assistant.

---

## Entities

Each board is a device. Each open list on the board is a `todo` entity, and each board has one `event` entity.

| Entity | Entity ID | State | Description |
|--------|-----------|-------|-------------|
| One per open list | `todo.<list name>` | Number of open items | Its cards, as to-do items in list order |
| The board's done list | `todo.<done list name>` | `0` | Its cards, shown as completed items |
| Board activity | `event.<board name>_activity` | Time of the last event | Fires an event for each Trello activity (see Events) |

If two boards have a list with the same name, the second one gets `_2`.

### Attributes

| Attribute | Description |
|-----------|-------------|
| `items` | The first 25 cards in list order. Each is `{uid, summary, due, labels: [{name, color}], members: [initials], url}`. `due` is an ISO 8601 UTC string, or `null`. |
| `items_total` | The true number of cards in the list. |

Neither attribute is recorded in history.

> **Note:** Entity IDs may vary if you have renamed entities. The names above are the defaults.

---

## Installation

### Via HACS (Recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/Space-C0wboy/ha-trello` as type **Integration**
3. Click **Download**
4. Restart Home Assistant

### Manual

1. Copy `custom_components/trello_todo/` to your HA `custom_components/` directory
2. Restart Home Assistant

Requires Home Assistant 2026.3 or newer.

---

## Configuration

### 1. Create a Trello OAuth app (once)

1. Open [trello.com/apps/admin](https://trello.com/apps/admin) and create a new app. Choose **"My app doesn't use Power-up capabilities"**.
   - A Power-Up app's tokens only reach boards in the app's own workspace.
   - Trello can't change the app type later.
2. On the app's **OAuth 2.0** tab:
   - **Client security type:** Confidential
   - **Callback URL:** `https://my.home-assistant.io/redirect/oauth`, then click **Add**
   - **Scopes:** Read boards, Write boards and Read members
   - Click **Save**
3. Copy the **client ID** and **client secret**.

### 2. Add the credentials to Home Assistant

1. Go to **Settings → Devices & Services → ⋮ → Application credentials → Add**
2. Pick **Trello** and paste the client ID and secret

### 3. Add the integration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Trello** and sign in with Atlassian
3. Approve the permissions. `offline_access` is what lets Home Assistant stay signed in.
4. Tick the boards you want

---

## Boards & Done Settings

Each board decides what checking an item off does:

| Setting | What happens to the card |
|---------|--------------------------|
| **Move to the done list** | Moves to the top of the done list |
| **Archive** | Archived where it is |
| **Move and archive** | Both, in one change |

- **Default:** a list whose name starts with "done" (for example "Done & Archive" or "✅ Done", but not "To be done") is picked as the done list, with **Move**. A board with no such list defaults to **Archive**.
- **Change a board's settings:** open the Trello integration, find the board, and choose **Change done settings**.
- **Add a board:** **+ Add board** on the Trello integration.
- **Remove a board:** delete it there. That removes its device and lists.
- **Refresh interval:** **Configure** on the integration (60–900 seconds, default 120).

---

## How Items Map to Cards

| To-do item | Trello card |
|------------|-------------|
| Summary | Card name |
| Description | Card description |
| Due | Due date. Exactly midnight local time shows as a date; anything else shows as a date and time. |
| Status | Open lists: "needs action". The done list: "completed". |
| Order | Card position in the list |

- **Checking off** an item applies the board's done setting.
- **Un-checking** an item in the done list moves the card back to the list it came from, at the top. If that list is gone, it goes to the board's first list.
- **Deleting** an item, including **Clear completed**, archives the card. To restore it, open the board menu in Trello → **Archived items**.
- **New items** are added at the bottom of the list.

To check an item off from a dashboard or automation:

```yaml
action: todo.update_item
data:
  entity_id: todo.chores
  item: <uid>
  status: completed
```

---

## Events

Each board also gets an event entity, `event.<board name>_activity` (for example `event.household_activity`). It fires one event per Trello activity on the board, including changes made by other people and from the Trello app.

| `event_type` | When |
|--------------|------|
| `card_created` | A card is created, copied, converted from a checklist item, or moved in from another board |
| `card_moved` | A card moves to another list |
| `card_archived` / `card_unarchived` | A card is archived or restored |
| `due_changed` | A due date is set, changed or removed |
| `due_completed` | A due date is marked complete or incomplete |
| `card_updated` | A card's name or description changes |
| `card_deleted` | A card is deleted |
| `comment_added` | Someone comments on a card |
| `member_added` / `member_removed` | Someone is added to or removed from a card |
| `label_added` / `label_removed` | A label is added or removed |
| `checklist_item_checked` | A checklist item is checked or unchecked |
| `attachment_added` | An attachment is added |

Every event carries `card_id`, `card_name`, `card_url`, `list_id`, `list_name`, `by` (`{id, name, initials}`), `by_me` (true when you did it), `action_id` and `date`. Some types add more:

| Event | Extra attributes |
|-------|------------------|
| `card_moved` | `from_list`, `to_list`, `to_done_list` (true when it landed in the board's done list) |
| `due_changed` | `due`, `old_due` |
| `due_completed` | `due_complete` |
| `comment_added` | `comment` (first 1,000 characters) |
| `member_added` / `member_removed` | `member`, `for_me` (true when it's you) |
| `label_added` / `label_removed` | `label` (`{name, color}`) |
| `checklist_item_checked` | `item`, `checked`, `checklist` |
| `attachment_added` | `attachment` |

### Example automations

Notify when someone else comments:

```yaml
triggers:
  - trigger: state
    entity_id: event.household_activity
conditions:
  - condition: state
    entity_id: event.household_activity
    attribute: event_type
    state: comment_added
  - condition: state
    entity_id: event.household_activity
    attribute: by_me
    state: false
actions:
  - action: notify.notify
    data:
      message: "{{ trigger.to_state.attributes.by.name }} commented on {{ trigger.to_state.attributes.card_name }}: {{ trigger.to_state.attributes.comment }}"
```

A card was assigned to me: use `event_type` `member_added` with `for_me` `true`.

A card landed in the done list: use `event_type` `card_moved` with `to_done_list` `true`.

---

## Actions

Automations and scripts can change Trello with these actions. You'll find them under **Developer tools → Actions** and in the automation editor.

| Action | What it does |
|--------|--------------|
| `trello_todo.create_card` | Create a card, with an optional description, due date, labels, members and position |
| `trello_todo.copy_card` | Copy a card to a list, optionally keeping only some of its parts (`keep`) |
| `trello_todo.move_card` | Move a card to another list, or to another board |
| `trello_todo.archive_card` / `restore_card` | Archive or restore a card |
| `trello_todo.add_comment` | Comment on a card |
| `trello_todo.add_label` / `remove_label` | Add or remove a label |
| `trello_todo.assign_member` / `unassign_member` | Add or remove a member |
| `trello_todo.add_checklist_item` | Add an item to a checklist; the checklist is created if it doesn't exist |
| `trello_todo.set_checklist_item` | Check or uncheck a checklist item |
| `trello_todo.set_due` | Set, clear or complete a due date |

**Which card.** Every action except `create_card` needs one of these:
- `card`: the card's id, short link or URL. Trello events provide it as `card_id` and `card_url`.
- `entity_id` together with `card_name`: a Trello to-do list and the card's name in it. This only finds open cards.

**Names, not ids.** Give boards, lists, labels and members by name, matched case-insensitively. Their Trello ids work too.
- A label without a name can be given by its colour, for example `green`.
- A member can be given by full name or username.
- If a name matches more than one thing, the action fails with a clear error rather than guessing.

**Safe to repeat.** Adding a label or member that's already on the card, or removing one that isn't, counts as success.

**Chaining actions.** To act again on a card you just created or moved, pass `card: "{{ result.card_id }}"` from the earlier step's response rather than looking it up by `card_name`.

**Results.** Add `response_variable` to use the card's details in later steps: `card_id`, `card_url`, `card_name`, `list_name` and `board_name`. `add_comment` also returns `comment_id`.

### Example: comment when a card reaches the done list

```yaml
triggers:
  - trigger: state
    entity_id: event.household_activity
conditions:
  - condition: state
    entity_id: event.household_activity
    attribute: event_type
    state: card_moved
  - condition: state
    entity_id: event.household_activity
    attribute: to_done_list
    state: true
actions:
  - action: trello_todo.add_comment
    data:
      card: "{{ trigger.to_state.attributes.card_id }}"
      text: "Done ✅"
```

### Example: check off a checklist item when a package arrives

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.package_delivered
    to: "on"
actions:
  - action: trello_todo.set_checklist_item
    data:
      entity_id: todo.chores
      card_name: Order furnace filters
      item: Delivered
      checked: true
```

### Example: create a card from a recurring calendar event

Add a recurring event, such as "Replace furnace filter" every 90 days, to a local calendar. Then:

```yaml
triggers:
  - trigger: calendar
    entity_id: calendar.home_maintenance
    event: start
actions:
  - action: trello_todo.create_card
    data:
      entity_id: todo.chores
      name: "{{ trigger.calendar_event.summary }}"
      due: "{{ now().date() + timedelta(days=7) }}"
    response_variable: created
  - action: notify.notify
    data:
      message: "Added {{ created.card_name }}: {{ created.card_url }}"
```

---

## Data & Refresh

Each board runs in one of two modes, shown in the event entity's `source` attribute:

| Mode | When | Events | Board refresh |
|------|------|--------|---------------|
| `webhook` | Nabu Casa is active, or Home Assistant has an HTTPS external URL | Within seconds | Every 15 minutes (safety net) |
| `feed` | No public URL, or Trello couldn't reach it | Within about a minute | Your refresh interval (default 2 minutes) |

- **Changes made in Home Assistant** show immediately, in both modes.
- **Events from before a restart** are not replayed.
- **Webhook health** is checked every 15 minutes; a broken webhook is recreated, and the board uses the feed until it works.
- **Rate limits:** if Trello rate-limits a request, that board backs off and retries.

---

## Troubleshooting

**Atlassian shows an error about the redirect URI or callback URL**
- Check that the app's callback URL is exactly `https://my.home-assistant.io/redirect/oauth`

**A board in someone else's workspace is missing or unreadable**
- The Trello app was probably created with Power-up capabilities. Create a new app without them (see Configuration).

**Home Assistant asks you to sign in again**
- Trello stopped accepting the sign-in. You revoked access, or it went unused for 90 days. Sign in again with the same Trello account.

**A repair says a board is no longer accessible**
- The board was deleted in Trello, or your account lost access to it. Its lists go unavailable while other boards keep working. Regain access, or delete the board from the integration.

**Checking off fails with "The done list … no longer exists"**
- The board's done list was archived or deleted in Trello. Use **Change done settings** on that board.

**Events arrive about a minute late**
- The board is on the activity feed. Check the event entity's `source` attribute, and the reason under `webhook_failure` in the integration's diagnostics. Trello can only reach Home Assistant through Nabu Casa or an HTTPS external URL.

**Check logs**
- Settings → System → Logs, filter by `trello_todo`

---

## Limits

These are not supported yet:
- Attachments
- Creating or editing labels (adding existing labels to cards works)
- Creating boards or lists
- Moving cards between lists from the to-do panel (use `trello_todo.move_card` instead)

---

## License

MIT
