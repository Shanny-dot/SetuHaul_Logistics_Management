"""Policy parameters in one place.

Every number here is a business decision, not a technical one. Each is listed
in the Decision Register (deliverable 2) with the reason and who must sign it
off. Change a value here and the whole system follows; nothing is hard-coded
elsewhere.
"""

# --- Time on the ground (ASSUMPTIONS until the client confirms) -------------
GATE_TO_DOCK_MIN = 15        # gate security + papers + yard drive, for a truck arriving from the road
YARD_TO_DOCK_MIN = 10        # a truck already parked in the yard, once called to a dock
DOCK_CHANGEOVER_MIN = 10     # previous truck pulls out, chocks/leveller reset, next truck reverses in
OVERRUN_BUFFER_MIN = 10      # once an unload passes its expected end, assume it needs this much more

# --- ETA planning buffers by confidence -------------------------------------
# HIGH: driver gave a clock time. MEDIUM: "around", or an unconfirmed plan.
# LOW: "late by an hour", a range, or a failed plausibility check.
ETA_BUFFER_MIN = {"HIGH": 0, "MEDIUM": 15, "LOW": 30}
ETA_OPTIMISTIC_MIN = {"HIGH": 0, "MEDIUM": 10, "LOW": 15}   # how much earlier the truck *might* arrive

# --- Capacity layer ----------------------------------------------------------
OFFER_VALID_MIN = 15          # an option shown to a driver is re-checked if picked after this
APPROVAL_DEADLINE_MAX_MIN = 15   # a hold awaiting a coordinator lasts at most this long ...
APPROVAL_LEAD_BEFORE_START_MIN = 30   # ... and must be decided this long before the planned start
EARMARK_TTL_MIN = 20          # a planner earmark (soft protection, not a promise) expires after this
MAX_OPTIONS_SHOWN = 3         # WhatsApp reply buttons allow 3; a list allows 10
MAX_CLARIFICATIONS = 2        # after two unanswered questions a person takes over

# --- Priority ----------------------------------------------------------------
PRIORITY_RANK = {"CRITICAL": 4, "HIGH": 3, "NORMAL": 2, "LOW": 1}
# Cost of one minute of waiting, by priority. Doubling ladder (8:4:2:1).
# Doc A used 100:40:15:5 and Doc B used 4:2:1:0.5; the planner report shows
# whether the plan changes under each (sensitivity), so the client can choose.
WAIT_WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}

# --- Dock compatibility policy -----------------------------------------------
ALLOW_STANDARD_ON_HEAVY_DOCK = False   # "flex dock": needs client decision; planner can show the gain
ALLOW_STANDARD_ON_REEFER_DOCK = False

# --- Duplicate detection -----------------------------------------------------
DUPLICATE_WINDOW_MIN = 10
DUPLICATE_MIN_WORDS = 4      # never deduplicate short answers such as "1" or "yes"

# --- Safety words: these stop all scheduling talk and page the shift manager -
SAFETY_WORDS = ["accident", "injur", "injured", "hurt", "fire", "police", "hospital", "chot", "durghatna",
                "दुर्घटना", "चोट", "आग", "pulis", "aag lag"]

# --- WhatsApp channel (Twilio) ----------------------------------------------
# Provider limits, checked against Twilio's docs on 2026-11-19. Each line says where.
# twilio/quick-reply (https://www.twilio.com/docs/content/twilio-quick-reply):
#   "only three buttons are allowed" for an in-session WhatsApp message without approval;
#   button title max 20 chars; button id max 200 chars; body max 1,024 chars.
#   The tapped button comes back in the inbound ButtonPayload (id) and ButtonText/Body (title).
TWILIO_QR_MAX_BUTTONS = 3
TWILIO_QR_TITLE_MAX = 20
TWILIO_QR_ID_MAX = 200
TWILIO_BODY_MAX = 1024
# twilio/list-picker (https://www.twilio.com/docs/content/twiliolist-picker):
#   1-10 items; item max 24 chars; id max 200; description max 72; body max 1,024.
#   "only available once the end user is in a 24 hour session"; cannot be submitted for approval.
#   NOT VERIFIED: the inbound field for a list selection. Twilio's webhook reference lists
#   ButtonPayload and InteractiveData but not ListId; inbound.py reads all three.
TWILIO_LIST_MAX_ITEMS = 10
TWILIO_LIST_ITEM_MAX = 24
TWILIO_LIST_DESC_MAX = 72
# WhatsApp Sandbox (https://www.twilio.com/docs/whatsapp/sandbox):
#   "The Sandbox number can only send one message every three seconds."
#   "The Sandbox session expires three days after joining."
#   Business-initiated messages: "you can use only pre-approved templates" (three test templates);
#   "You can't use custom message templates with the Sandbox."
TWILIO_SANDBOX_SEND_INTERVAL_S = 3
TWILIO_SANDBOX_SESSION_DAYS = 3
# Inbound webhook (https://www.twilio.com/docs/messaging/guides/webhook-request): no timestamp
# parameter, so replay protection is MessageSid dedupe (DEDUPE_TTL in session_store.py).
WHATSAPP_SESSION_WINDOW_H = 24          # free-form replies only within 24 h of the driver's last message

# --- Channel policy (business choices; Decision Register) --------------------
UNSUPPORTED_BEFORE_HANDOFF = 2          # two voice notes / images in a row -> a person takes over
DELIVERY_ALARM_MIN = 5                  # a message not delivered after this raises the reply-time alarm (F27/F28)
SEND_MAX_ATTEMPTS = 6                   # retries on 429 / 5xx / network, exponential backoff with jitter
SEND_BACKOFF_BASE_S = 2
SEND_BACKOFF_CAP_S = 120
MESSAGE_BODY_RETENTION_DAYS = 90        # then bodies are purged and only facts remain (DPDP)
UNKNOWN_SENDER_REFUSAL_EVERY_H = 24     # one polite refusal per unknown number per day
LOCK_WAIT_S = 25                        # how long a second message waits for the driver's previous turn
SAFETY_DUTY_HEAD_ROLE = "OPS_DUTY_HEAD" # last step of the missed-SLA chain (phone from DUTY_HEAD_PHONE)
