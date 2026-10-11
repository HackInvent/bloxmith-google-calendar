"""Pure Google Calendar API v3 contracts: settings, requests, times, event bodies, resources, watch state and errors.

No network, filesystem, framework or third-party import happens here. The clock
is always a parameter, so every function is deterministic and the policy can be
checked directly by the tests.
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OFFICIAL_API = "https://www.googleapis.com"
OFFICIAL_OAUTH = "https://oauth2.googleapis.com"
API_ROOT = "/calendar/v3"
OPERATIONS = ("list_calendars", "list_events", "get_event", "free_busy", "watch", "create_event", "update_event",
              "delete_event")
WRITES = frozenset(("create_event", "update_event"))
DELETES = frozenset(("delete_event",))
CALENDAR_WRITES = WRITES | DELETES
SEND_UPDATES = ("none", "externalOnly", "all")
ACCESS_ROLES = ("freeBusyReader", "reader", "writer", "owner")
ORDERS = ("startTime", "updated")
REMINDER_METHODS = ("email", "popup")
TRANSPARENCIES = ("opaque", "transparent")
VISIBILITIES = ("default", "public", "private", "confidential")
CHANGES = ("created", "updated", "cancelled", "existing")
PRIMARY = "primary"
CALENDAR_LIST_PAGE = 250
FREE_BUSY_LIMIT = 50
ATTENDEE_LIMIT = 100
REMINDER_LIMIT = 5
RECURRENCE_LIMIT = 10
OUTPUT_LIMIT = 16777216
# A listener result is limited to 1 MiB of ASCII JSON; one watch batch stays well below it.
BATCH_LIMIT = 900000
PENDING_LIMIT = 1000
RECENT_LIMIT = 1000
EXISTING_LIMIT = 100
WATCH_LIMIT = 8
MARGIN_SEC = 120
MIN_POLL_SEC = 60

DEFAULTS = {
    "enabled": False, "secret_ref": "", "calendar_id": PRIMARY, "operation": "list_events",
    "allowed_operations": ["list_calendars", "list_events", "get_event", "free_busy"], "default_time_zone": "",
    "single_events": True, "list_limit": 250, "allow_writes": False, "allow_delete": False,
    "allow_request_calendar": False, "send_updates": "none", "watch_enabled": False, "watch_interval_sec": 300,
    "watch_max_changes": 50, "emit_existing": False, "page_size": 250, "max_pages": 10, "timeout_sec": 60,
    "max_request_bytes": 65536, "max_response_bytes": 4194304, "max_retries": 2, "max_retry_wait_sec": 20,
    "api_base_url": OFFICIAL_API, "oauth_base_url": OFFICIAL_OAUTH, "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "list_limit": (1, 10000), "page_size": (1, 2500), "max_pages": (1, 100), "watch_interval_sec": (MIN_POLL_SEC, 86400),
    "watch_max_changes": (1, 100), "timeout_sec": (5, 900), "max_request_bytes": (1024, 1048576),
    "max_response_bytes": (65536, 33554432), "max_retries": (0, 5), "max_retry_wait_sec": (1, 120),
}
SWITCHES = ("enabled", "single_events", "allow_writes", "allow_delete", "allow_request_calendar", "watch_enabled",
            "emit_existing")
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
CALENDAR_ID = re.compile(r"[A-Za-z0-9._%+#=-]{1,200}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
# Client-chosen event IDs: base32hex characters (a-v, 0-9), 5 to 1024 of them.
NEW_EVENT_ID = re.compile(r"[a-v0-9]{5,1024}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TOKEN_TEXT = re.compile(r"[\x21-\x7e]{1,8192}\Z")
PAGE_TOKEN = re.compile(r"[\x21-\x7e]{1,8192}\Z")
ETAG = re.compile(r'"?[\x21\x23-\x7e]{1,128}"?\Z')
TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,40}(?:/[A-Za-z0-9_+-]{1,40}){0,3}\Z")
EMAIL = re.compile(r"[^@\s<>\"(),;:\\\[\]]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
DATE_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2})(:[0-9]{2}(?:\.[0-9]{1,6})?)?(Z|[+-][0-9]{2}:[0-9]{2})?\Z")
RECURRENCE = re.compile(r"(?:RRULE|EXRULE|RDATE|EXDATE)[:;][\x20-\x7e]{1,500}\Z")
SUMMARY_LIMIT = 1024
TEXT_LIMIT = 8192
LOCATION_LIMIT = 1024
QUERY_LIMIT = 1024
REQUEST_KEYS = {
    "list_calendars": {"limit", "page_token", "min_access_role", "show_hidden"},
    "list_events": {"calendar_id", "time_min", "time_max", "query", "single_events", "order_by", "show_deleted", "limit",
                    "page_token", "time_zone"},
    "get_event": {"calendar_id", "event_id", "time_zone"},
    "free_busy": {"calendars", "time_min", "time_max", "time_zone"},
    "watch": set(),
    "create_event": {"calendar_id", "event_id", "summary", "description", "location", "start", "end", "duration_minutes",
                     "time_zone", "attendees", "reminders", "recurrence", "transparency", "visibility", "send_updates"},
    "update_event": {"calendar_id", "event_id", "etag", "summary", "description", "location", "start", "end",
                     "duration_minutes", "time_zone", "attendees", "reminders", "recurrence", "transparency",
                     "visibility", "send_updates"},
    "delete_event": {"calendar_id", "event_id", "etag", "send_updates"},
}
PATCH_FIELDS = ("summary", "description", "location", "start", "end", "duration_minutes", "attendees", "reminders",
                "recurrence", "transparency", "visibility")
REASONS = {"rateLimitExceeded": "rate_limited", "userRateLimitExceeded": "rate_limited",
           "quotaExceeded": "quota_exceeded", "dailyLimitExceeded": "quota_exceeded",
           "fullSyncRequired": "sync_expired", "duplicate": "duplicate", "conditionNotMet": "conflict"}

MESSAGES = {
    "config": "Invalid Google Calendar settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before contacting Google Calendar.",
    "secret": "Unlock the wallet and store an OAuth access token, or client_id/client_secret/refresh_token JSON, under secret_ref.",
    "invalid_input": "The request does not follow the documented contract; nothing was sent.",
    "request_too_large": "The request exceeds max_request_bytes; nothing was sent.",
    "operation_not_allowed": "This operation is not listed in allowed_operations; nothing was sent.",
    "writes_disabled": "Writes are disabled; enable allow_writes in the properties; nothing was created or changed.",
    "delete_disabled": "Deletion is disabled; enable allow_delete in the properties; nothing was deleted.",
    "calendar_not_allowed": "A request may not choose the calendar of a change unless allow_request_calendar is on; nothing was sent.",
    "storage": "Block storage is unavailable; watch needs a project-scoped run.",
    "state_invalid": "The stored watch position is unreadable; nothing was polled. Remove the node's watch state to start again.",
    "busy": "Another poll of this node is running (the Run listener or another request); nothing was polled.",
    "too_soon": "The previous poll of this node was less than 60 seconds ago; nothing was polled.",
    "overflow": "More than 1000 changes were waiting; the oldest extra changes were dropped. Raise watch_max_changes or poll more often.",
    "auth_failed": "Google refused to issue an access token from the wallet grant; check the refresh token and its scopes.",
    "bad_request": "Google Calendar rejected the request (HTTP 400); nothing was changed.",
    "unauthorized": "Google Calendar rejected the access token (HTTP 401); refresh or replace the wallet secret.",
    "forbidden": "The account has no access to this calendar or event, lacks a Calendar scope, or the API is not enabled (HTTP 403).",
    "not_found": "The calendar or event was not found (HTTP 404); check the ID and its sharing.",
    "gone": "The event was deleted (HTTP 410).",
    "duplicate": "An event with this event_id already exists in the calendar (HTTP 409); nothing was created again.",
    "conflict": "The event changed since the given etag, or Google reported a conflict (HTTP 409/412); nothing was changed.",
    "sync_expired": "Google invalidated the stored sync token (HTTP 410); a full resynchronization is needed.",
    "rate_limited": "Google Calendar quota exceeded (HTTP 429 or 403 rate limit); retry after the indicated delay.",
    "quota_exceeded": "The Google Calendar usage limit of the account or project is exhausted (HTTP 403); retry much later.",
    "provider_error": "Google Calendar returned an unexpected error status; nothing is assumed changed.",
    "provider_unavailable": "Google Calendar is temporarily unavailable (HTTP 5xx); nothing was changed.",
    "provider_unavailable_uncertain": "Google Calendar answered HTTP 5xx after the change was sent; the change is uncertain.",
    "network": "The connection to Google failed before the request was complete; nothing was changed.",
    "network_uncertain": "The connection failed after the change was sent; the change is uncertain.",
    "tls": "TLS verification failed for the Google API origin; nothing was sent.",
    "timeout": "The operation exceeded timeout_sec before any change was complete; nothing was changed.",
    "timeout_uncertain": "The operation exceeded timeout_sec after the change was sent; the change is uncertain.",
    "cancelled": "The operation was cancelled after the change was sent; the change is uncertain.",
    "cancelled_before_send": "The operation was cancelled before any change was sent; nothing was changed.",
    "protocol": "Google returned an invalid, compressed or oversized response.",
    "internal": "The Google Calendar operation failed without a complete result.",
}


class CalendarError(ValueError):
    """A content-free diagnostic with a stable machine code and an English message."""

    def __init__(self, code, detail=None, *, uncertain=False, http_status=None, retry_after=None,
                 provider_status=None, provider_reason=None, provider_message=None, retryable=False):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code, self.uncertain, self.retryable = code, bool(uncertain), bool(retryable)
        self.http_status, self.retry_after = http_status, retry_after
        self.provider_status, self.provider_reason, self.provider_message = provider_status, provider_reason, provider_message
        self.attempts = 0

    def envelope(self, **context):
        """Return the public error document; callers add identifiers only, never event content or tokens."""
        result = {"code": self.code, "message": str(self), "uncertain": self.uncertain}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("provider_status", self.provider_status), ("provider_reason", self.provider_reason),
                           ("provider_message", self.provider_message)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure."""
    raise CalendarError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 24 or count > 200000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail("request_too_large" if code == "invalid_input" else code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeError:
            fail(code, "UTF-8 JSON is required.")
    if not isinstance(text, str):
        fail(code, "JSON text is required.")
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail("request_too_large" if code == "invalid_input" else code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except CalendarError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name, code="config"):
    if type(value) is not bool:
        fail(code, f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def path_text(value, name):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail("config", f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail("config", f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def api_origin(value, name):
    """Accept an HTTPS origin; plain HTTP only for a loopback host such as a local test double."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail("config", f"{name} must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail("config", f"{name} has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/") or "\\" in value or "%" in host):
        fail("config", f"{name} must be a bare https:// origin without credentials, path, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail("config", f"{name} has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail("config", f"{name} has an invalid port.")
    if parsed.scheme == "http" and not is_loopback(host):
        fail("config", f"Plain HTTP is accepted for {name} only with a loopback test host; use HTTPS.")
    netloc = "[" + host + "]" if ":" in host else host
    return parsed.scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def calendar_id(value, name="calendar_id", code="invalid_input"):
    """primary, or a calendar ID such as team@group.calendar.google.com (never a calendar URL)."""
    if not isinstance(value, str):
        fail(code, f"{name} must be primary or a calendar ID.")
    value = value.strip()
    if value == PRIMARY:
        return value
    if len(value) > 254 or not CALENDAR_ID.fullmatch(value):
        fail(code, f"{name} must be primary or a calendar ID such as name@group.calendar.google.com, not a URL.")
    return value


def event_id(value, name="event_id"):
    if not isinstance(value, str) or not EVENT_ID.fullmatch(value.strip()):
        fail("invalid_input", f"{name} must be a Google Calendar event ID (letters, digits, _ or -), not a URL.")
    return value.strip()


def zone_known(name):
    """True when the time zone database knows name, or when no database is available to check it."""
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    try:
        ZoneInfo("Etc/UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return True  # No local database: Google validates the name.
    return False


def time_zone(value, name="time_zone", code="invalid_input"):
    """An IANA time zone name such as Europe/Paris."""
    if not isinstance(value, str) or not TIME_ZONE.fullmatch(value.strip()) or not zone_known(value.strip()):
        fail(code, f"{name} must be an IANA time zone such as Europe/Paris or UTC.")
    return value.strip()


def operations_list(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= len(OPERATIONS)
            or any(item not in OPERATIONS for item in value) or len(set(value)) != len(value)):
        fail("config", "allowed_operations must list distinct values among " + ", ".join(OPERATIONS) + ".")
    return value


def configuration(raw):
    """Pure settings validation; no wallet, filesystem or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0][:64] + ". Credentials belong in the wallet.")
    config = {**{key: thaw(value) for key, value in DEFAULTS.items()},
              **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for key in SWITCHES:
        boolean(config[key], key)
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    if config["send_updates"] not in SEND_UPDATES:
        fail("config", "send_updates must be one of: " + ", ".join(SEND_UPDATES) + ".")
    ref = config["secret_ref"]
    if not isinstance(ref, str) or len(ref) > 256:
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/google_calendar.")
    ref = ref.strip()
    if ref and not SECRET_REF.fullmatch(ref):
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/google_calendar, never a token.")
    config["secret_ref"] = ref
    config["calendar_id"] = calendar_id(config["calendar_id"], "calendar_id", "config")
    zone = config["default_time_zone"]
    if not isinstance(zone, str):
        fail("config", "default_time_zone must be empty or an IANA time zone such as Europe/Paris.")
    config["default_time_zone"] = time_zone(zone, "default_time_zone", "config") if zone.strip() else ""
    if config["operation"] not in OPERATIONS:
        fail("config", "operation must be one of: " + ", ".join(OPERATIONS) + ".")
    config["allowed_operations"] = operations_list(config["allowed_operations"])
    selectable = set(config["allowed_operations"]) | {config["operation"]}
    if selectable & WRITES and not config["allow_writes"]:
        fail("config", "create_event and update_event need allow_writes; enable it or remove them.")
    if selectable & DELETES and not config["allow_delete"]:
        fail("config", "delete_event needs allow_delete; enable it or remove it.")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    config["api_base_url"] = api_origin(config["api_base_url"], "api_base_url")
    config["oauth_base_url"] = api_origin(config["oauth_base_url"], "oauth_base_url")
    if config["enabled"] and not config["secret_ref"]:
        fail("config", "Remote access requires secret_ref, a wallet reference holding the Google OAuth grant.")
    return config


def selectable_operations(config):
    return [name for name in OPERATIONS if name == config["operation"] or name in config["allowed_operations"]]


def credentials(value):
    """Validate the wallet value: a bare access token, {access_token} or a refresh-token grant."""
    secret = value
    if isinstance(secret, str):
        text = secret.strip()
        if text.startswith("{"):
            try:
                secret = strict_json(text, 32768, "secret")
            except CalendarError:
                fail("secret")
        else:
            secret = {"access_token": text}
    if not isinstance(secret, Mapping):
        fail("secret")
    keys = set(secret)
    if keys not in ({"access_token"}, {"client_id", "client_secret", "refresh_token"}):
        fail("secret")
    for item in secret.values():
        if not isinstance(item, str) or not TOKEN_TEXT.fullmatch(item):
            fail("secret")
    return dict(secret)


def redact(text, secrets):
    """Mask every credential value wherever it could appear."""
    text = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def clean(value, limit):
    """Bounded single-line provider text with control characters removed."""
    if not isinstance(value, str):
        return None
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    return value[:limit] if value else None


# ---------------------------------------------------------------------------
# Times


def utc_text(moment):
    """Canonical RFC 3339 UTC text with whole seconds."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def offset_text(moment):
    """RFC 3339 text that keeps the given offset (Z for UTC)."""
    text = moment.isoformat(timespec="seconds")
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def aware(naive, zone_name):
    """Attach an IANA zone to a local date-time; None when no database is available."""
    try:
        return naive.replace(tzinfo=ZoneInfo(zone_name))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def parse_local(text, name):
    """Split an RFC 3339 date-time; return (naive datetime, offset text or None)."""
    match = DATE_TIME.fullmatch(text)
    if not match:
        fail("invalid_input", f"{name} must be a date (2026-11-02) or a date-time (2026-11-02T10:00:00+01:00).")
    seconds = match.group(3) or ":00"
    try:
        naive = datetime.fromisoformat(match.group(1) + "T" + match.group(2) + seconds)
    except ValueError:
        fail("invalid_input", f"{name} is not a valid calendar date-time.")
    return naive, match.group(4)


def with_offset(naive, offset):
    """Apply an RFC 3339 offset (Z or +HH:MM) to a naive date-time."""
    if offset == "Z":
        return naive.replace(tzinfo=timezone.utc)
    sign = 1 if offset[0] == "+" else -1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    if hours > 23 or minutes > 59:
        fail("invalid_input", "The time zone offset is invalid.")
    return naive.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))


def parse_date(text, name):
    try:
        return date.fromisoformat(text)
    except ValueError:
        fail("invalid_input", f"{name} is not a valid calendar date.")


def instant(value, name, zone_name, now):
    """A window bound: now, a date (midnight in the zone, else UTC) or an RFC 3339 date-time; UTC text."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        fail("invalid_input", f"{name} must be now, a date such as 2026-11-02 or an RFC 3339 date-time.")
    text = value.strip()
    if text == "now":
        return utc_text(now)
    if DATE_ONLY.fullmatch(text):
        midnight = datetime.combine(parse_date(text, name), datetime.min.time())
        moment = aware(midnight, zone_name) if zone_name else midnight.replace(tzinfo=timezone.utc)
        return utc_text(moment if moment is not None else midnight.replace(tzinfo=timezone.utc))
    naive, offset = parse_local(text, name)
    if offset is None:
        if not zone_name:
            fail("invalid_input", f"{name} needs an offset (Z or +01:00) or a time_zone.")
        moment = aware(naive, zone_name)
        if moment is None:
            fail("invalid_input", f"{name} needs an offset (Z or +01:00) on this host.")
        return utc_text(moment)
    return utc_text(with_offset(naive, offset))


def event_time(value, name, zone_name):
    """Build a Google start/end object; return (body, kind, aware instant or None, naive local)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        fail("invalid_input", f"{name} must be a date (all-day event) or a date-time.")
    text = value.strip()
    if DATE_ONLY.fullmatch(text):
        day = parse_date(text, name)
        return {"date": day.isoformat()}, "date", None, datetime.combine(day, datetime.min.time())
    naive, offset = parse_local(text, name)
    if offset is None:
        if not zone_name:
            fail("invalid_input", f"{name} needs an offset (Z or +01:00) or a time_zone.")
        body = {"dateTime": naive.isoformat(timespec="seconds"), "timeZone": zone_name}
        return body, "local", aware(naive, zone_name), naive
    moment = with_offset(naive, offset)
    body = {"dateTime": offset_text(moment)}
    if zone_name:
        body["timeZone"] = zone_name
    return body, "offset", moment, naive


def shifted(start, minutes, zone_name):
    """The end of a timed event that lasts minutes (elapsed time, so DST changes are respected)."""
    body, kind, moment, naive = start
    delta = timedelta(minutes=minutes)
    if kind == "offset":
        end = moment + delta
        result = {"dateTime": offset_text(end)}
        if "timeZone" in body:
            result["timeZone"] = body["timeZone"]
        return result, kind, end, naive + delta
    if moment is not None:
        end = (moment.astimezone(timezone.utc) + delta).astimezone(moment.tzinfo)
        local = end.replace(tzinfo=None)
    else:
        end, local = None, naive + delta
    return {"dateTime": local.isoformat(timespec="seconds"), "timeZone": zone_name}, kind, end, local


def ordered(start, end, name_start="start", name_end="end"):
    """The end must follow the start; both all-day or both timed."""
    if (start[1] == "date") != (end[1] == "date"):
        fail("invalid_input", f"{name_start} and {name_end} must both be dates (all-day) or both date-times.")
    if start[1] == "date":
        if end[3] <= start[3]:
            fail("invalid_input", f"{name_end} must be after {name_start} (an all-day end date is exclusive).")
        return
    if start[2] is not None and end[2] is not None:
        if end[2] <= start[2]:
            fail("invalid_input", f"{name_end} must be after {name_start}.")
    elif start[1] == end[1] == "local" and start[0].get("timeZone") == end[0].get("timeZone") and end[3] <= start[3]:
        fail("invalid_input", f"{name_end} must be after {name_start}.")


# ---------------------------------------------------------------------------
# Requests


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def document(value, config):
    """Read a JSON object; other text is a bare trigger for the configured operation."""
    if isinstance(value, Mapping):
        result = thaw(value)
        encoded(result, config["max_request_bytes"])
        return result
    if isinstance(value, (list, tuple)):
        fail("invalid_input", "The request must be one JSON object, not an array.")
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "The request must be UTF-8 text or JSON.")
    if not isinstance(value, str):
        fail("invalid_input", "The request must be UTF-8 text or JSON.")
    text = value.strip()
    if text.startswith("{") or text.startswith("["):
        parsed = strict_json(text, config["max_request_bytes"])
        if not isinstance(parsed, dict):
            fail("invalid_input", "The request must be one JSON object, not an array.")
        return parsed
    if len(value.encode("utf-8", errors="replace")) > config["max_request_bytes"]:
        fail("request_too_large")
    return {}


def text_field(value, name, limit, *, empty=False):
    """Free text: bounded, no control characters except tab and line breaks in long fields."""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        fail("invalid_input", f"{name} must be {'up to' if empty else '1 to'} {limit} characters.")
    multiline = limit > LOCATION_LIMIT
    for char in value:
        if (ord(char) < 32 and not (multiline and char in "\t\n\r")) or ord(char) == 127:
            fail("invalid_input", f"{name} cannot contain control characters.")
    return value if multiline else value.strip()


def flag(data, key, default):
    value = data.get(key, default)
    return boolean(value, key, "invalid_input")


def page_token(data):
    token = data.get("page_token")
    if token is not None and (not isinstance(token, str) or not PAGE_TOKEN.fullmatch(token)):
        fail("invalid_input", "page_token must be the next_page_token text of a previous result.")
    return token


def request_zone(data, config):
    zone = data.get("time_zone")
    if zone is None:
        return config["default_time_zone"]
    return time_zone(zone)


def target_calendar(data, config, operation):
    """Reads may address any calendar; changes go to calendar_id unless allow_request_calendar is on."""
    if data.get("calendar_id") is None:
        return config["calendar_id"]
    target = calendar_id(data["calendar_id"])
    if operation in CALENDAR_WRITES and target != config["calendar_id"] and not config["allow_request_calendar"]:
        fail("calendar_not_allowed")
    return target


def send_updates(data, config):
    """A request may lower the guest-notification policy, never raise it above the setting."""
    value = data.get("send_updates", config["send_updates"])
    if value not in SEND_UPDATES:
        fail("invalid_input", "send_updates must be one of: " + ", ".join(SEND_UPDATES) + ".")
    if SEND_UPDATES.index(value) > SEND_UPDATES.index(config["send_updates"]):
        fail("invalid_input", "send_updates cannot notify more guests than the send_updates setting ("
             + config["send_updates"] + ").")
    return value


def etag(data):
    value = data.get("etag")
    if value is None:
        return None
    if not isinstance(value, str) or not ETAG.fullmatch(value):
        fail("invalid_input", "etag must be the etag text of a previous event result.")
    return value


def attendees(value):
    """Up to 100 guests: an email text, or {email, display_name?, optional?}."""
    if not isinstance(value, list) or len(value) > ATTENDEE_LIMIT:
        fail("invalid_input", f"attendees must be an array of up to {ATTENDEE_LIMIT} guests.")
    result, seen = [], set()
    for item in value:
        entry = {"email": item} if isinstance(item, str) else item
        if not isinstance(entry, dict) or set(entry) - {"email", "display_name", "optional"} or "email" not in entry:
            fail("invalid_input", "Each attendee is an email or {email, display_name, optional}.")
        email = entry["email"].strip() if isinstance(entry["email"], str) else None
        if not email or len(email) > 254 or not EMAIL.fullmatch(email):
            fail("invalid_input", "Each attendee needs a valid email address.")
        if email.lower() in seen:
            fail("invalid_input", "An attendee is listed twice.")
        seen.add(email.lower())
        guest = {"email": email}
        if entry.get("display_name") is not None:
            guest["displayName"] = text_field(entry["display_name"], "display_name", 256)
        if entry.get("optional") is not None:
            guest["optional"] = boolean(entry["optional"], "optional", "invalid_input")
        result.append(guest)
    return result


def reminders(value):
    """{use_default?, overrides?: [{method, minutes}]}: the calendar default or up to five explicit reminders."""
    if not isinstance(value, dict) or set(value) - {"use_default", "overrides"} or not value:
        fail("invalid_input", "reminders must be {\"use_default\": true} or {\"overrides\": [{\"method\", \"minutes\"}]}.")
    overrides = value.get("overrides", [])
    if not isinstance(overrides, list) or len(overrides) > REMINDER_LIMIT:
        fail("invalid_input", f"reminders.overrides must be an array of up to {REMINDER_LIMIT} reminders.")
    use_default = value.get("use_default", not overrides)
    boolean(use_default, "reminders.use_default", "invalid_input")
    if use_default and overrides:
        fail("invalid_input", "reminders cannot combine use_default with overrides.")
    result = []
    for item in overrides:
        if not isinstance(item, dict) or set(item) != {"method", "minutes"} or item["method"] not in REMINDER_METHODS:
            fail("invalid_input", "Each reminder is {\"method\": \"email\"|\"popup\", \"minutes\": 0-40320}.")
        result.append({"method": item["method"],
                       "minutes": integer(item["minutes"], "reminders minutes", 0, 40320, "invalid_input")})
    return {"useDefault": use_default, **({"overrides": result} if not use_default else {})}


def recurrence(value):
    if not isinstance(value, list) or not 1 <= len(value) <= RECURRENCE_LIMIT:
        fail("invalid_input", f"recurrence must be an array of 1 to {RECURRENCE_LIMIT} RRULE, EXRULE, RDATE or EXDATE lines.")
    for line in value:
        if not isinstance(line, str) or not RECURRENCE.fullmatch(line):
            fail("invalid_input", "Each recurrence line starts with RRULE:, EXRULE:, RDATE or EXDATE (RFC 5545), one line each.")
    return list(value)


def event_fields(data, zone, *, patch):
    """Translate request fields into a Google event body; validate before anything is sent."""
    body = {}
    if data.get("summary") is not None:
        body["summary"] = text_field(data["summary"], "summary", SUMMARY_LIMIT)
    elif not patch:
        fail("invalid_input", "create_event needs summary, the event title.")
    if data.get("description") is not None:
        body["description"] = text_field(data["description"], "description", TEXT_LIMIT, empty=True)
    if data.get("location") is not None:
        body["location"] = text_field(data["location"], "location", LOCATION_LIMIT, empty=True)
    if data.get("start") is None and not patch:
        fail("invalid_input", "create_event needs start (a date for an all-day event, or a date-time).")
    start = event_time(data["start"], "start", zone) if data.get("start") is not None else None
    end = event_time(data["end"], "end", zone) if data.get("end") is not None else None
    minutes = data.get("duration_minutes")
    if minutes is not None:
        integer(minutes, "duration_minutes", 1, 525600, "invalid_input")
        if end is not None or start is None:
            fail("invalid_input", "duration_minutes replaces end and needs start in the same request.")
        if start[1] == "date":
            fail("invalid_input", "An all-day event takes an end date, not duration_minutes.")
        end = shifted(start, minutes, zone)
    if start is not None and end is None and not patch:
        if start[1] != "date":
            fail("invalid_input", "A timed event needs end or duration_minutes.")
        day = start[3] + timedelta(days=1)
        end = ({"date": day.date().isoformat()}, "date", None, day)
    if start is not None and end is not None:
        ordered(start, end)
    if start is not None:
        body["start"] = start[0]
    if end is not None:
        body["end"] = end[0]
    if data.get("attendees") is not None:
        body["attendees"] = attendees(data["attendees"])
    if data.get("reminders") is not None:
        body["reminders"] = reminders(data["reminders"])
    if data.get("recurrence") is not None:
        if (start or (None, None))[1] == "offset" and "timeZone" not in body.get("start", {}):
            fail("invalid_input", "A recurring event needs time_zone so Google can expand it.")
        body["recurrence"] = recurrence(data["recurrence"])
    for key, allowed in (("transparency", TRANSPARENCIES), ("visibility", VISIBILITIES)):
        if data.get(key) is not None:
            if data[key] not in allowed:
                fail("invalid_input", f"{key} must be one of: " + ", ".join(allowed) + ".")
            body[key] = data[key]
    return body


def window(data, zone, now, *, required):
    found = {}
    for key in ("time_min", "time_max"):
        if data.get(key) is not None:
            found[key] = instant(data[key], key, zone, now)
        elif required:
            fail("invalid_input", "free_busy needs time_min and time_max.")
    if "time_min" in found and "time_max" in found and found["time_max"] <= found["time_min"]:
        fail("invalid_input", "time_max must be after time_min.")
    return found


def parse_request(value, config, now):
    """Return one normalized operation request; enforce the allowlist and the change gates before any wallet access."""
    data = document(value, config)
    operation = data.get("operation", config["operation"])
    if operation not in OPERATIONS:
        fail("invalid_input", "operation must be one of: " + ", ".join(OPERATIONS) + ".")
    request = {"operation": operation, "request_id": identifier(data.get("request_id"))}
    if operation not in selectable_operations(config):
        fail("operation_not_allowed")
    if operation in WRITES and not config["allow_writes"]:
        fail("writes_disabled")
    if operation in DELETES and not config["allow_delete"]:
        fail("delete_disabled")
    unknown = set(data) - REQUEST_KEYS[operation] - {"operation", "request_id"}
    if unknown:
        fail("invalid_input", "Unknown field for " + operation + ": " + sorted(unknown)[0][:64] + ".")
    zone = request_zone(data, config)
    if operation == "list_calendars":
        request["limit"] = integer(data.get("limit", config["list_limit"]), "limit", 1, config["list_limit"], "invalid_input")
        request["page_token"] = page_token(data)
        role = data.get("min_access_role")
        if role is not None and role not in ACCESS_ROLES:
            fail("invalid_input", "min_access_role must be one of: " + ", ".join(ACCESS_ROLES) + ".")
        request["min_access_role"] = role
        request["show_hidden"] = flag(data, "show_hidden", False)
        return request
    if operation == "watch":
        return request
    if operation == "free_busy":
        calendars = data.get("calendars", [config["calendar_id"]])
        if not isinstance(calendars, list) or not 1 <= len(calendars) <= FREE_BUSY_LIMIT:
            fail("invalid_input", f"calendars must be an array of 1 to {FREE_BUSY_LIMIT} calendar IDs.")
        ids = [calendar_id(item, "calendars") for item in calendars]
        if len(set(ids)) != len(ids):
            fail("invalid_input", "calendars lists the same calendar twice.")
        request.update(calendars=ids, time_zone=zone or None, **window(data, zone, now, required=True))
        return request
    request["calendar_id"] = target_calendar(data, config, operation)
    if operation == "list_events":
        request["limit"] = integer(data.get("limit", config["list_limit"]), "limit", 1, config["list_limit"], "invalid_input")
        request["page_token"] = page_token(data)
        request["single_events"] = flag(data, "single_events", config["single_events"])
        request["show_deleted"] = flag(data, "show_deleted", False)
        order = data.get("order_by")
        if order is not None and order not in ORDERS:
            fail("invalid_input", "order_by must be startTime or updated.")
        if order == "startTime" and not request["single_events"]:
            fail("invalid_input", "order_by startTime needs single_events (recurring events expanded).")
        request["order_by"] = order
        request["query"] = text_field(data["query"], "query", QUERY_LIMIT) if data.get("query") is not None else None
        request["time_zone"] = zone or None
        request.update(window(data, zone, now, required=False))
        return request
    if operation == "create_event":
        if data.get("event_id") is not None:
            if not isinstance(data["event_id"], str) or not NEW_EVENT_ID.fullmatch(data["event_id"]):
                fail("invalid_input", "A new event_id needs 5-1024 characters among a-v and 0-9 (base32hex).")
            request["event_id"] = data["event_id"]
        request["send_updates"] = send_updates(data, config)
        request["body"] = event_fields(data, zone, patch=False)
        if "event_id" in request:
            request["body"]["id"] = request["event_id"]
        return request
    if data.get("event_id") is None:
        fail("invalid_input", operation + " needs event_id.")
    request["event_id"] = event_id(data["event_id"])
    if operation == "get_event":
        request["time_zone"] = zone or None
        return request
    request["etag"] = etag(data)
    request["send_updates"] = send_updates(data, config)
    if operation == "update_event":
        if not any(data.get(key) is not None for key in PATCH_FIELDS):
            fail("invalid_input", "update_event needs at least one field to change: " + ", ".join(PATCH_FIELDS) + ".")
        request["body"] = event_fields(data, zone, patch=True)
    return request


# ---------------------------------------------------------------------------
# Calendar resources


def optional_text(item, key, limit=1048576):
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        fail("protocol")
    return value


def optional_bool(item, key, default=False):
    value = item.get(key, default)
    if type(value) is not bool:
        fail("protocol")
    return value


def person(value, keys=("email", "displayName", "self")):
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("protocol")
    result = {"email": optional_text(value, "email", 512), "display_name": optional_text(value, "displayName", 1024),
              "self": optional_bool(value, "self")}
    if "responseStatus" in keys:
        result.update(response_status=optional_text(value, "responseStatus", 64), optional=optional_bool(value, "optional"),
                      organizer=optional_bool(value, "organizer"), resource=optional_bool(value, "resource"))
    return result


def moment_out(value):
    """A Google start/end/originalStartTime object in the public shape."""
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("protocol")
    return {"date": optional_text(value, "date", 64), "date_time": optional_text(value, "dateTime", 64),
            "time_zone": optional_text(value, "timeZone", 64)}


def normalize_event(item):
    """Validate one event resource and return the public, token-free JSON shape."""
    if not isinstance(item, dict):
        fail("protocol")
    ident = item.get("id")
    if not isinstance(ident, str) or not EVENT_ID.fullmatch(ident):
        fail("protocol")
    attendees_raw = item.get("attendees", [])
    recurrence_raw = item.get("recurrence")
    if not isinstance(attendees_raw, list) or len(attendees_raw) > 10000:
        fail("protocol")
    if recurrence_raw is not None and (not isinstance(recurrence_raw, list) or len(recurrence_raw) > 1000
                                       or any(not isinstance(line, str) for line in recurrence_raw)):
        fail("protocol")
    raw_reminders = item.get("reminders")
    reminder = None
    if raw_reminders is not None:
        if not isinstance(raw_reminders, dict) or not isinstance(raw_reminders.get("overrides", []), list):
            fail("protocol")
        overrides = []
        for entry in raw_reminders.get("overrides", []):
            if not isinstance(entry, dict) or type(entry.get("minutes")) is not int:
                fail("protocol")
            overrides.append({"method": optional_text(entry, "method", 32), "minutes": entry["minutes"]})
        reminder = {"use_default": optional_bool(raw_reminders, "useDefault"), "overrides": overrides}
    start = moment_out(item.get("start"))
    return {
        "id": ident, "status": optional_text(item, "status", 32), "summary": optional_text(item, "summary"),
        "description": optional_text(item, "description"), "location": optional_text(item, "location"),
        "start": start, "end": moment_out(item.get("end")),
        "all_day": bool(start and start["date"] and not start["date_time"]),
        "created": optional_text(item, "created", 64), "updated": optional_text(item, "updated", 64),
        "html_link": optional_text(item, "htmlLink", 4096), "hangout_link": optional_text(item, "hangoutLink", 4096),
        "recurrence": list(recurrence_raw) if recurrence_raw is not None else None,
        "recurring_event_id": optional_text(item, "recurringEventId", 1024),
        "original_start": moment_out(item.get("originalStartTime")),
        "organizer": person(item.get("organizer")), "creator": person(item.get("creator")),
        "attendees": [person(entry, ("email", "displayName", "self", "responseStatus")) for entry in attendees_raw],
        "reminders": reminder, "transparency": optional_text(item, "transparency", 32),
        "visibility": optional_text(item, "visibility", 32), "event_type": optional_text(item, "eventType", 64),
        "ical_uid": optional_text(item, "iCalUID", 2048), "etag": optional_text(item, "etag", 256),
    }


def event_page(body, size):
    """Validate one events.list page; return (raw items, next page token, next sync token, calendar time zone)."""
    if not isinstance(body, dict):
        fail("protocol")
    items = body.get("items", [])
    following, sync = body.get("nextPageToken"), body.get("nextSyncToken")
    if not isinstance(items, list) or len(items) > size:
        fail("protocol")
    for token in (following, sync):
        if token is not None and (not isinstance(token, str) or not PAGE_TOKEN.fullmatch(token)):
            fail("protocol")
    return items, following or None, sync or None, optional_text(body, "timeZone", 64)


def normalize_calendar(item):
    """Validate one calendarList entry and return its public shape."""
    if not isinstance(item, dict):
        fail("protocol")
    ident = item.get("id")
    if not isinstance(ident, str) or not 1 <= len(ident) <= 1024:
        fail("protocol")
    return {"id": ident, "summary": optional_text(item, "summary", 4096),
            "summary_override": optional_text(item, "summaryOverride", 4096),
            "description": optional_text(item, "description"), "time_zone": optional_text(item, "timeZone", 64),
            "access_role": optional_text(item, "accessRole", 32), "primary": optional_bool(item, "primary"),
            "selected": optional_bool(item, "selected"), "hidden": optional_bool(item, "hidden"),
            "color_id": optional_text(item, "colorId", 16)}


def calendar_page(body, size):
    if not isinstance(body, dict):
        fail("protocol")
    items, following = body.get("items", []), body.get("nextPageToken")
    if (not isinstance(items, list) or len(items) > size
            or (following is not None and (not isinstance(following, str) or not PAGE_TOKEN.fullmatch(following)))):
        fail("protocol")
    return items, following or None


def free_busy(body, ids):
    """Normalize a freeBusy response into one entry per requested calendar, in request order."""
    if not isinstance(body, dict) or not isinstance(body.get("calendars"), dict):
        fail("protocol")
    result = []
    for ident in ids:
        entry = body["calendars"].get(ident)
        if entry is None:
            result.append({"id": ident, "busy": [], "errors": ["notFound"]})
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("busy", []), list) \
                or not isinstance(entry.get("errors", []), list):
            fail("protocol")
        busy = []
        for slot in entry.get("busy", []):
            if not isinstance(slot, dict) or not isinstance(slot.get("start"), str) or not isinstance(slot.get("end"), str):
                fail("protocol")
            busy.append({"start": slot["start"][:64], "end": slot["end"][:64]})
        errors = [clean(item.get("reason"), 64) or "unknown" for item in entry.get("errors", []) if isinstance(item, dict)]
        result.append({"id": ident, "busy": busy, "errors": errors})
    return result


def calendar_resource(body):
    """calendars.get: the resolved calendar ID (primary becomes the account address) and its time zone."""
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not 1 <= len(body["id"]) <= 1024:
        fail("protocol")
    return {"id": body["id"], "time_zone": optional_text(body, "timeZone", 64)}


# ---------------------------------------------------------------------------
# Provider responses


def map_status(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            410: "gone", 412: "conflict", 429: "rate_limited"}.get(status, "provider_unavailable" if status >= 500
                                                                      else "provider_error")


def provider_failure(status, body, secrets, *, mutation, retry_after=None):
    """Raise the coded error for a non-2xx Calendar response ({"error": {code, message, errors, status}})."""
    detail = body.get("error") if isinstance(body, dict) else None
    provider_status = clean(detail.get("status"), 64) if isinstance(detail, dict) else None
    errors = detail.get("errors") if isinstance(detail, dict) else None
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    reason = clean(first.get("reason"), 64)
    message = clean(redact(detail.get("message"), secrets), 200) if isinstance(detail, dict) else None
    code = map_status(status)
    if status in (403, 429) and reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                                           "dailyLimitExceeded"):
        code = REASONS[reason]
    elif status == 410 and reason == "fullSyncRequired":
        code = "sync_expired"
    elif status == 409 and reason == "duplicate":
        code = "duplicate"
    uncertain = mutation and code == "provider_unavailable"
    fail(code, MESSAGES["provider_unavailable_uncertain"] if uncertain else None, uncertain=uncertain, http_status=status,
         retry_after=retry_after if code == "rate_limited" else None, provider_status=provider_status,
         provider_reason=reason, provider_message=message)


# ---------------------------------------------------------------------------
# Watch state and change classification


def server_time(date_header, fallback):
    """The provider clock from an HTTP Date header, else the local UTC clock; canonical UTC text."""
    if isinstance(date_header, str) and date_header.strip():
        try:
            moment = parsedate_to_datetime(date_header)
            if moment.tzinfo is not None:
                return utc_text(moment)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return utc_text(fallback)


def parse_stamp(value):
    """An RFC 3339 provider timestamp as an aware datetime, or None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def start_key(item):
    """Sortable UTC text for an event start (all-day events at midnight UTC); None when unknown."""
    start = item.get("start") if isinstance(item, dict) else None
    if not isinstance(start, dict):
        return None
    if isinstance(start.get("dateTime"), str):
        moment = parse_stamp(start["dateTime"])
        return utc_text(moment) if moment else None
    if isinstance(start.get("date"), str) and DATE_ONLY.fullmatch(start["date"]):
        return start["date"] + "T00:00:00Z"
    return None


def end_key(item):
    end = item.get("end") if isinstance(item, dict) else None
    if not isinstance(end, dict):
        return None
    return start_key({"start": end})


def watch_digest(resolved_id):
    """Watch positions are keyed by a digest: the calendar address itself is never stored."""
    return hashlib.sha256(("google-calendar-watch-v1|" + resolved_id).encode("utf-8")).hexdigest()


def new_entry():
    return {"phase": "baseline", "sync_token": None, "page_token": None, "since": None, "pass_started": None,
            "recent": [], "pending": [], "updated_at": 0}


def change(item, kind):
    """A pending change: identifiers and timestamps only, never titles, guests or descriptions."""
    original = item.get("originalStartTime") if isinstance(item.get("originalStartTime"), dict) else {}
    original_text = original.get("dateTime") or original.get("date")
    recurring = item.get("recurringEventId")
    return {"id": item["id"], "change": kind, "start": start_key(item),
            "recurring_event_id": recurring if isinstance(recurring, str) and EVENT_ID.fullmatch(recurring) else None,
            "original_start": original_text if isinstance(original_text, str) and len(original_text) <= 64 else None,
            "updated": item.get("updated") if isinstance(item.get("updated"), str) and len(item["updated"]) <= 64 else None}


def classify(item, entry, *, emit_existing):
    """Decide what one listed item means for this pass: a change kind, or None when nothing is reported."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not EVENT_ID.fullmatch(item["id"]):
        fail("protocol")
    phase, cancelled = entry["phase"], item.get("status") == "cancelled"
    started = parse_stamp(entry["pass_started"])
    created = parse_stamp(item.get("created"))
    updated = parse_stamp(item.get("updated"))
    since = parse_stamp(entry["since"])
    margin = timedelta(seconds=MARGIN_SEC)
    if phase == "baseline":
        if not emit_existing or cancelled:
            return None
        ending = parse_stamp(end_key(item))
        return "existing" if ending is None or started is None or ending > started else None
    if phase == "resync" and since is not None and (updated is None or updated < since - margin):
        return None  # Unchanged since the last completed pass: a full listing is not a change.
    if cancelled:
        return "cancelled"
    if created is not None and since is not None and created >= since - margin and item["id"] not in entry["recent"]:
        return "created"
    return "updated"


def remember(entry, items):
    """Record IDs of events created around this pass, so the next pass does not call them created again."""
    started = parse_stamp(entry["pass_started"])
    if started is None:
        return entry
    margin = timedelta(seconds=MARGIN_SEC)
    recent = list(entry["recent"])
    for item in items:
        created = parse_stamp(item.get("created")) if isinstance(item, dict) else None
        if created is not None and created >= started - margin and item["id"] not in recent:
            recent.append(item["id"])
    return {**entry, "recent": recent[-RECENT_LIMIT:]}


def enqueue(pending, additions):
    """Merge new changes into the pending queue: one entry per event, the newest decision wins (created stays created)."""
    queue = [dict(item) for item in pending]
    index = {item["id"]: position for position, item in enumerate(queue)}
    for item in additions:
        position = index.get(item["id"])
        if position is None:
            index[item["id"]] = len(queue)
            queue.append(item)
            continue
        previous = queue[position]
        kind = "created" if previous["change"] in ("created", "existing") and item["change"] == "updated" \
            else item["change"]
        queue[position] = {**item, "change": kind}
    return queue


def trim_existing(pending):
    """emit_existing is bounded: only the EXISTING_LIMIT soonest upcoming events are kept."""
    existing = [item for item in pending if item["change"] == "existing"]
    if len(existing) <= EXISTING_LIMIT:
        return pending
    kept = {item["id"] for item in sorted(existing, key=lambda item: (item["start"] is None, item["start"] or ""))
            [:EXISTING_LIMIT]}
    return [item for item in pending if item["change"] != "existing" or item["id"] in kept]


def watch_document(value):
    """Validate the stored watch document strictly; anything unexpected is state_invalid."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "last_poll", "watches"} \
            or value["schema_version"] != 1 or type(value["last_poll"]) is not int or not isinstance(value["watches"], dict) \
            or len(value["watches"]) > WATCH_LIMIT:
        fail("state_invalid")
    for digest, entry in value["watches"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(entry, dict) or set(entry) != set(new_entry()):
            fail("state_invalid")
        if entry["phase"] not in ("baseline", "incremental", "resync") or type(entry["updated_at"]) is not int:
            fail("state_invalid")
        for key in ("sync_token", "page_token"):
            if entry[key] is not None and (not isinstance(entry[key], str) or not PAGE_TOKEN.fullmatch(entry[key])):
                fail("state_invalid")
        for key in ("since", "pass_started"):
            if entry[key] is not None and parse_stamp(entry[key]) is None:
                fail("state_invalid")
        if entry["phase"] == "incremental" and entry["sync_token"] is None:
            fail("state_invalid")
        if (not isinstance(entry["recent"], list) or len(entry["recent"]) > RECENT_LIMIT
                or any(not isinstance(item, str) or not EVENT_ID.fullmatch(item) for item in entry["recent"])):
            fail("state_invalid")
        if not isinstance(entry["pending"], list) or len(entry["pending"]) > PENDING_LIMIT:
            fail("state_invalid")
        for item in entry["pending"]:
            if (not isinstance(item, dict) or set(item) != {"id", "change", "start", "recurring_event_id", "original_start",
                                                             "updated"}
                    or not isinstance(item["id"], str) or not EVENT_ID.fullmatch(item["id"]) or item["change"] not in CHANGES
                    or any(item[key] is not None and (not isinstance(item[key], str) or len(item[key]) > 1024)
                           for key in ("start", "recurring_event_id", "original_start", "updated"))):
                fail("state_invalid")
    return value


def empty_document():
    return {"schema_version": 1, "last_poll": 0, "watches": {}}


def store_entry(document, digest, entry, now):
    """Return a new document with this watch entry; at most WATCH_LIMIT calendars are retained."""
    watches = dict(document["watches"])
    watches[digest] = {**entry, "updated_at": int(now)}
    if len(watches) > WATCH_LIMIT:
        for key in sorted(watches, key=lambda item: watches[item]["updated_at"])[:len(watches) - WATCH_LIMIT]:
            watches.pop(key)
    return {"schema_version": 1, "last_poll": document["last_poll"], "watches": watches}


def poll_wait(document, now):
    """Seconds before this node may poll again (0 when due); a clock moved backwards does not block polling."""
    elapsed = int(now) - document["last_poll"]
    if elapsed < 0 and -elapsed > MIN_POLL_SEC:
        return 0
    return max(0, MIN_POLL_SEC - elapsed)


def cancelled_event(item):
    """The public object of a cancelled event: Google keeps only identifiers for a deleted event."""
    return {"id": item["id"], "status": "cancelled", "summary": None, "description": None, "location": None,
            "start": None, "end": None, "all_day": False, "created": None, "updated": item["updated"],
            "html_link": None, "hangout_link": None, "recurrence": None,
            "recurring_event_id": item["recurring_event_id"],
            "original_start": ({"date": item["original_start"], "date_time": None, "time_zone": None}
                               if item["original_start"] and DATE_ONLY.fullmatch(item["original_start"]) else
                               {"date": None, "date_time": item["original_start"], "time_zone": None}
                               if item["original_start"] else None),
            "organizer": None, "creator": None, "attendees": [], "reminders": None, "transparency": None,
            "visibility": None, "event_type": None, "ical_uid": None, "etag": None}


def weight(value):
    """Bytes the value adds to the runtime's ASCII JSON snapshot, where the output text is itself a JSON string."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)  # The same text the block publishes.
    return len(json.dumps(text, ensure_ascii=True))


def reduced(change_object):
    """Keep identifiers only when one event alone would not fit a listener result."""
    event = change_object["event"]
    keep = {key: event.get(key) for key in ("id", "status", "start", "end", "all_day", "created", "updated", "html_link",
                                           "recurring_event_id", "etag")}
    return {**change_object, "event": keep, "oversized": True}
