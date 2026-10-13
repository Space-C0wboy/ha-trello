"""Constants for the Trello integration."""
from typing import Final

DOMAIN: Final = "trello_todo"
SUBENTRY_BOARD: Final = "board"

OAUTH2_AUTHORIZE: Final = "https://auth.atlassian.com/authorize"
OAUTH2_TOKEN: Final = "https://auth.atlassian.com/oauth/token"
OAUTH2_SCOPES: Final = ["read:member:trello", "read:board:trello", "write:board:trello", "offline_access"]
TRELLO_APPS_URL: Final = "https://trello.com/apps/admin"
MY_REDIRECT_URL: Final = "https://my.home-assistant.io/redirect/oauth"

CONF_BOARD_ID: Final = "board_id"
CONF_DONE_BEHAVIOR: Final = "done_behavior"
CONF_DONE_LIST: Final = "done_list"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DONE_MOVE: Final = "move"
DONE_ARCHIVE: Final = "archive"
DONE_MOVE_AND_ARCHIVE: Final = "move_and_archive"
DONE_BEHAVIORS: Final = [DONE_MOVE, DONE_ARCHIVE, DONE_MOVE_AND_ARCHIVE]

DEFAULT_SCAN_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 60
MAX_SCAN_INTERVAL: Final = 900

# Back off this long after a 429 that carries no Retry-After header.
DEFAULT_RATE_LIMIT_BACKOFF: Final = 60

# The `items` attribute is capped so state objects stay small on the Pi.
MAX_ITEMS_ATTRIBUTE: Final = 25

# Due-date sensors: the `cards` attribute cap, and how far ahead "due soon" looks.
MAX_CARDS_ATTRIBUTE: Final = 20
DUE_SOON_DAYS: Final = 7

MODE_WEBHOOK: Final = "webhook"
MODE_FEED: Final = "feed"
CONF_WEBHOOKS: Final = "webhooks"
CONF_RETIRED_WEBHOOKS: Final = "retired_webhooks"
FEED_INTERVAL_SECONDS: Final = 60
WEBHOOK_REFRESH_MINUTES: Final = 15
