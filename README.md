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
- **Dashboard-friendly attributes:** card names, due dates, labels and members are available in state attributes.
- **No extra dependencies:** uses only `aiohttp`, which is already bundled with Home Assistant.

---

## Entities

Each board is a device. Each open list on the board is a `todo` entity.

| Entity | Entity ID | State | Description |
|--------|-----------|-------|-------------|
| One per open list | `todo.<list name>` | Number of open items | Its cards, as to-do items in list order |
| The board's done list | `todo.<done list name>` | `0` | Its cards, shown as completed items |

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

- **Default:** a list whose name contains "done" (for example "Done & Archive") is picked as the done list, with **Move**. A board with no such list defaults to **Archive**.
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

## Data & Refresh

- **Update frequency:** each board is refreshed every 2 minutes by default, with one request per board.
- **Changes made in Home Assistant:** that board refreshes immediately.
- **Changes made in Trello:** show up within the refresh interval.
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

**Check logs**
- Settings → System → Logs, filter by `trello_todo`

---

## Limits

Instant updates (Trello webhooks) are planned. These are not supported yet:
- Checklists, comments and attachments
- Editing labels or members
- Moving cards between ordinary lists
- Creating boards or lists

---

## License

MIT
