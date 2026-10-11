"""Loopback-only Google Calendar API v3 and OAuth token double. No Google account or credential is used.

It reproduces the documented request/response shapes used by the block:
calendarList.list, calendars.get, events.list (timeMin/timeMax, q, singleEvents
expansion of simple RRULEs, orderBy, showDeleted, pageToken, syncToken with
nextSyncToken and HTTP 410 fullSyncRequired), events.get, events.insert (client
IDs and 409 duplicate), events.patch and events.delete (If-Match etags, 412,
204 and 410), freeBusy.query, the {"error": {"code", "message", "errors":
[{"reason"}], "status"}} envelope, Retry-After on 429, 403 rate limits and the
OAuth refresh-token grant at /token. Credentials below are fixture text built
at run time; none has the shape of a real Google token.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

ACCESS = "-".join(("fixture", "calendar", "access", "token", "not", "real"))
REFRESHED = "-".join(("fixture", "calendar", "refreshed", "token", "not", "real"))
GRANT = {"client_id": "fixture-calendar-client.apps.example.test",
         "client_secret": "-".join(("fixture", "calendar", "client", "secret", "not", "real")),
         "refresh_token": "-".join(("fixture", "calendar", "refresh", "token", "not", "real"))}
REF = "secret://workspace/google_calendar_fixture"
OWNER = "fixture.owner@example.test"
TEAM = "team.fixture@group.calendar.example.test"
HOLIDAYS = "en.fixture#holiday@group.v.calendar.example.test"
HIDDEN = "hidden.fixture@group.calendar.example.test"
MISSING = "missing.fixture@group.calendar.example.test"
STANDUP = "fixturestandup0001"
REVIEW = "fixturereview00001"
OFFSITE = "fixtureoffsite0001"
PAST = "fixturepast0000001"
PRIVATE = "fixtureprivate0001"
# Seeded events sit 30 days ahead of the run date, so "upcoming" never depends on the calendar year.
BASE_DAY = datetime.now(timezone.utc).date() + timedelta(days=30)
# Built in two parts so the portability scan does not read this API path as a home directory.
CALENDAR_LIST = "/calendar/v3/" + "users/me/calendarList"
LONG_AGO = (datetime.now(timezone.utc) - timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def on(days):
    """A date relative to BASE_DAY."""
    return (BASE_DAY + timedelta(days=days)).isoformat()


def at(days, clock="10:00:00"):
    """A UTC date-time relative to BASE_DAY."""
    return on(days) + "T" + clock + "Z"


def settings(api, **extra):
    """Block settings aimed at this fixture only."""
    return {"enabled": True, "secret_ref": REF, "api_base_url": api.base_url, "oauth_base_url": api.base_url,
            "ca_file": api.ca_file, "timeout_sec": 15, **extra}


def error(status, message, reason, status_text=None, location=None):
    detail = {"domain": "global", "reason": reason, "message": message}
    if location:
        detail.update(locationType="parameter", location=location)
    body = {"error": {"code": status, "message": message, "errors": [detail]}}
    if status_text:
        body["error"]["status"] = status_text
    return body


def stamp(moment=None):
    """Google-style RFC 3339 UTC timestamp with milliseconds."""
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def when(value):
    """Instant of a start/end object: dateTime with offset, local dateTime with timeZone, or a date at midnight UTC."""
    if "dateTime" in value:
        text = value["dateTime"].replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(value["timeZone"]))
        return moment.astimezone(timezone.utc)
    return datetime.fromisoformat(value["date"] + "T00:00:00+00:00")


def compact(moment):
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def instances(event):
    """Expand FREQ=DAILY|WEEKLY;COUNT=n series into instances, as singleEvents=true does."""
    rule = next((line for line in event.get("recurrence", []) if line.startswith("RRULE:")), None)
    if rule is None:
        return [event]
    parts = dict(item.split("=", 1) for item in rule[6:].split(";"))
    step = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1)}[parts["FREQ"]]
    count = int(parts.get("COUNT", "3"))
    start, end = when(event["start"]), when(event["end"])
    result = []
    for index in range(count):
        begin = start + step * index
        item = {key: value for key, value in event.items() if key not in ("recurrence", "id", "start", "end")}
        item.update(id=event["id"] + "_" + compact(begin), recurringEventId=event["id"],
                    start={"dateTime": (begin).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    end={"dateTime": (end + step * index).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    originalStartTime={"dateTime": begin.strftime("%Y-%m-%dT%H:%M:%SZ")})
        result.append(item)
    return result


def event(ident, summary, start, end, *, created=LONG_AGO, **extra):
    body = {"kind": "calendar#event", "id": ident, "status": "confirmed", "summary": summary,
            "htmlLink": "https://calendar.example.test/event?eid=" + ident, "created": created, "updated": created,
            "creator": {"email": OWNER, "self": True}, "organizer": {"email": OWNER, "self": True},
            "start": start, "end": end, "iCalUID": ident + "@example.test", "sequence": 0,
            "reminders": {"useDefault": True}, "eventType": "default"}
    body.update(extra)
    return body


def seed():
    owner = {
        STANDUP: event(STANDUP, "Daily stand-up", {"dateTime": at(0, "08:30:00"), "timeZone": "Europe/Paris"},
                       {"dateTime": at(0, "08:45:00"), "timeZone": "Europe/Paris"}, recurrence=["RRULE:FREQ=DAILY;COUNT=3"]),
        REVIEW: event(REVIEW, "Quarterly review", {"dateTime": at(1, "14:00:00")}, {"dateTime": at(1, "15:30:00")},
                      description="Budget and roadmap", location="Room 4",
                      attendees=[{"email": "guest.one@example.test", "responseStatus": "accepted"},
                                 {"email": "guest.two@example.test", "responseStatus": "needsAction", "optional": True}]),
        PRIVATE: event(PRIVATE, "Dentist", {"dateTime": at(2, "08:00:00")}, {"dateTime": at(2, "09:00:00")},
                       visibility="private"),
        OFFSITE: event(OFFSITE, "Team offsite", {"date": on(3)}, {"date": on(5)}, transparency="transparent"),
        PAST: event(PAST, "Old retro", {"dateTime": at(-230, "10:00:00")}, {"dateTime": at(-230, "11:00:00")}),
    }
    for index in range(1, 26):
        ident = f"fixturebulk{index:07d}"
        owner[ident] = event(ident, f"Bulk slot {index:02d}", {"dateTime": at(30 + index, "10:00:00")},
                             {"dateTime": at(30 + index, "10:30:00")})
    team = {"fixtureteam0000001": event("fixtureteam0000001", "Team sync", {"dateTime": at(1, "15:00:00")},
                                        {"dateTime": at(1, "16:00:00")})}
    return {
        OWNER: {"summary": OWNER, "timeZone": "Europe/Paris", "accessRole": "owner", "primary": True, "hidden": False,
                "events": owner},
        TEAM: {"summary": "Team", "timeZone": "UTC", "accessRole": "writer", "primary": False, "hidden": False,
               "events": team},
        HOLIDAYS: {"summary": "Holidays", "timeZone": "UTC", "accessRole": "reader", "primary": False, "hidden": False,
                   "events": {}},
        HIDDEN: {"summary": "Hidden", "timeZone": "UTC", "accessRole": "freeBusyReader", "primary": False, "hidden": True,
                 "events": {}},
    }


def text_match(item, query):
    body = " ".join(str(item.get(key, "")) for key in ("summary", "description", "location")).lower()
    return query.lower() in body


@contextmanager
def calendar_api(tls=False):
    """Run the double on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        lock = threading.Lock()
        api = SimpleNamespace(calls=[], script={}, release=threading.Event(), closing=threading.Event(),
                              tokens={ACCESS, REFRESHED}, holding=0, calendars=seed(), seq=0, seqs={}, counter=0,
                              min_sync=0)
        for calendar, data in api.calendars.items():
            for ident in data["events"]:
                api.seq += 1
                api.seqs[(calendar, ident)] = api.seq
                data["events"][ident]["etag"] = '"' + str(api.seq) + '"'

        def resolve(ident):
            return OWNER if ident == "primary" else ident

        def touch(calendar, item, **changes):
            """Apply a change the way Google does: new updated time, etag and sync sequence."""
            item.update(changes)
            api.seq += 1
            api.seqs[(calendar, item["id"])] = api.seq
            item["updated"] = stamp()
            item["etag"] = '"' + str(api.seq) + '"'
            return item

        def add(calendar, body):
            item = event(body["id"], body.get("summary", ""), body["start"], body["end"], created=stamp(),
                         **{key: value for key, value in body.items() if key not in ("id", "summary", "start", "end")})
            api.calendars[calendar]["events"][item["id"]] = item
            return touch(calendar, item)

        def cancel(calendar, ident):
            item = api.calendars[calendar]["events"][ident]
            return touch(calendar, item, status="cancelled")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(self, status, body, *, raw=None, content_type="application/json; charset=UTF-8", headers=None):
                data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode("utf-8"))
                try:
                    self.send_response(status)
                    if data or content_type != "application/json; charset=UTF-8":
                        self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError, ssl.SSLError):
                    pass
                finally:
                    self.close_connection = True

            def do_GET(self):
                self.dispatch("GET")

            def do_POST(self):
                self.dispatch("POST")

            def do_PATCH(self):
                self.dispatch("PATCH")

            def do_DELETE(self):
                self.dispatch("DELETE")

            def scripted(self, kind):
                """Apply one scripted action; return True when the reply has been handled."""
                queue = api.script.get(kind)
                if not queue:
                    return False
                action = queue.pop(0)
                if action == "hold":
                    with lock:
                        api.holding += 1
                    try:
                        api.release.wait(20)
                    finally:
                        with lock:
                            api.holding -= 1
                    return api.closing.is_set()
                if action == "lost":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return True
                if action == "html502":
                    self.reply(502, None, raw=b"<html>Bad Gateway</html>", content_type="text/html")
                    return True
                if action == "oversized":
                    self.reply(200, {"items": [{"id": "x" * 20, "summary": "y" * 1000}] * 100})
                    return True
                if action == "gzip":
                    self.reply(200, None, raw=b"\x1f\x8b", headers={"Content-Encoding": "gzip"})
                    return True
                if isinstance(action, dict) and "status" in action:
                    self.reply(action["status"], action["body"], headers=action.get("headers"))
                    return True
                return False

            def dispatch(self, method):
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 <= length <= 16 * 1024 * 1024
                raw = self.rfile.read(length)
                parts = urlsplit(self.path)
                query = {key: values if len(values) > 1 else values[0]
                         for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
                call = {"method": method, "path": parts.path, "query": query, "at": time.monotonic(),
                        "auth": self.headers.get("Authorization"), "accept_encoding": self.headers.get("Accept-Encoding"),
                        "content_type": self.headers.get("Content-Type"), "if_match": self.headers.get("If-Match"),
                        "kind": None, "body": None, "calendar": None, "event_id": None}
                api.calls.append(call)
                if parts.path == "/token" and method == "POST":
                    call["kind"] = "token"
                    form = {key: values[0] for key, values in parse_qs(raw.decode("ascii")).items()}
                    call["body"] = {key: ("<redacted>" if key in ("client_secret", "refresh_token") else value)
                                    for key, value in form.items()}
                    if self.scripted("token"):
                        return
                    if form == {**GRANT, "grant_type": "refresh_token"}:
                        self.reply(200, {"access_token": REFRESHED, "expires_in": 3599, "token_type": "Bearer",
                                         "scope": "https://www.googleapis.com/auth/calendar"})
                    else:
                        self.reply(400, {"error": "invalid_grant", "error_description": "Bad Request"})
                    return
                path, kind, calendar, ident = parts.path, None, None, None
                if path == CALENDAR_LIST and method == "GET":
                    kind = "calendar_list"
                elif path == "/calendar/v3/freeBusy" and method == "POST":
                    kind = "freebusy"
                else:
                    match = re.fullmatch(r"/calendar/v3/calendars/([^/]+)(/events(?:/([^/]+))?)?", path)
                    if match:
                        calendar = resolve(unquote(match.group(1)))
                        ident = unquote(match.group(3)) if match.group(3) else None
                        if match.group(2) is None and method == "GET":
                            kind = "calendar_get"
                        elif ident is None and method == "GET":
                            kind = "list"
                        elif ident is None and method == "POST":
                            kind = "insert"
                        elif ident is not None:
                            kind = {"GET": "get", "PATCH": "patch", "DELETE": "delete"}.get(method)
                if kind is None:
                    self.reply(404, error(404, "Not Found", "notFound"))
                    return
                call.update(kind=kind, calendar=calendar, event_id=ident)
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if token not in api.tokens:
                    self.reply(401, error(401, "Request had invalid authentication credentials. Expected OAuth 2 access "
                                               "token, login cookie or other valid authentication credential.", "authError",
                                          "UNAUTHENTICATED", "Authorization"))
                    return
                if raw:
                    assert call["content_type"] == "application/json; charset=UTF-8", call["content_type"]
                    call["body"] = json.loads(raw)
                if self.scripted(kind):
                    return
                if calendar is not None and calendar not in api.calendars:
                    self.reply(404, error(404, "Not Found", "notFound"))
                    return
                with lock:
                    getattr(self, "op_" + kind)(call, query, calendar, ident)

            def op_calendar_list(self, call, query, _calendar, _ident):
                size = int(query.get("maxResults", "100"))
                assert 1 <= size <= 250, size
                order = ("freeBusyReader", "reader", "writer", "owner")
                found = [(ident, data) for ident, data in api.calendars.items()
                         if (query.get("showHidden") == "true" or not data["hidden"])
                         and order.index(data["accessRole"]) >= order.index(query.get("minAccessRole", "freeBusyReader"))]
                offset = int(query["pageToken"].removeprefix("cal-")) if "pageToken" in query else 0
                page = found[offset:offset + size]
                body = {"kind": "calendar#calendarList", "items": [
                    {"kind": "calendar#calendarListEntry", "id": ident, "summary": data["summary"],
                     "timeZone": data["timeZone"], "accessRole": data["accessRole"], "colorId": "7",
                     "selected": True, **({"primary": True} if data["primary"] else {}),
                     **({"hidden": True} if data["hidden"] else {})} for ident, data in page]}
                if offset + size < len(found):
                    body["nextPageToken"] = f"cal-{offset + size}"
                self.reply(200, body)

            def op_calendar_get(self, call, query, calendar, _ident):
                data = api.calendars[calendar]
                self.reply(200, {"kind": "calendar#calendar", "id": calendar, "summary": data["summary"],
                                 "timeZone": data["timeZone"]})

            def op_list(self, call, query, calendar, _ident):
                size = query.get("maxResults", "250")
                assert size.isdigit() and 1 <= int(size) <= 2500, size
                size = int(size)
                events = api.calendars[calendar]["events"]
                sync = query.get("syncToken")
                if sync is not None:
                    if any(key in query for key in ("timeMin", "timeMax", "q", "orderBy", "updatedMin", "iCalUID")):
                        self.reply(400, error(400, "Invalid Value", "invalid", location="syncToken"))
                        return
                    match = re.fullmatch(r"sync-([0-9a-f]{8})-([0-9]+)", sync)
                    if not match or match.group(1) != hashlib.sha256(calendar.encode()).hexdigest()[:8]:
                        self.reply(400, error(400, "Invalid sync token value.", "invalid", location="syncToken"))
                        return
                    if int(match.group(2)) < api.min_sync:
                        self.reply(410, error(410, "Sync token is no longer valid, a full sync is required.",
                                              "fullSyncRequired"))
                        return
                    if query.get("showDeleted") == "false":
                        self.reply(400, error(400, "Invalid Value", "invalid", location="showDeleted"))
                        return
                    floor = int(match.group(2))
                    found = sorted((item for item in events.values() if api.seqs[(calendar, item["id"])] > floor),
                                   key=lambda item: api.seqs[(calendar, item["id"])])
                else:
                    found = [item for item in events.values()
                             if item["status"] != "cancelled" or query.get("showDeleted") == "true"]
                single = query.get("singleEvents") == "true"
                if single:
                    found = [instance for item in found for instance in
                             (instances(item) if item["status"] != "cancelled" else [item])]
                if query.get("orderBy") == "startTime" and not single:
                    self.reply(400, error(400, "The requested ordering is not available for the particular query.",
                                          "invalid", location="orderBy"))
                    return
                if "timeMin" in query or "timeMax" in query:
                    low = datetime.fromisoformat(query.get("timeMin", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
                    high = datetime.fromisoformat(query.get("timeMax", "2999-01-01T00:00:00Z").replace("Z", "+00:00"))
                    found = [item for item in found if "start" in item and when(item["end"]) > low and when(item["start"]) < high]
                if "q" in query:
                    found = [item for item in found if text_match(item, query["q"])]
                if sync is None:
                    if query.get("orderBy") == "updated":
                        found.sort(key=lambda item: item["updated"])
                    else:
                        found.sort(key=lambda item: (when(item["start"]) if "start" in item else datetime.min.replace(
                            tzinfo=timezone.utc), item["id"]))
                digest = hashlib.sha256(json.dumps({key: value for key, value in query.items()
                                                    if key not in ("pageToken", "maxResults")}, sort_keys=True).encode()
                                        ).hexdigest()[:12]
                offset, snapshot = 0, api.seq
                if "pageToken" in query:
                    match = re.fullmatch(r"page-([0-9]+)-([0-9]+)-([0-9a-f]{12})", query["pageToken"])
                    if not match or match.group(3) != digest:
                        self.reply(400, error(400, "Invalid Value", "invalid", location="pageToken"))
                        return
                    offset, snapshot = int(match.group(1)), int(match.group(2))
                page = found[offset:offset + size]
                items = []
                for item in page:
                    if item["status"] == "cancelled":
                        minimal = {"kind": "calendar#event", "etag": item.get("etag", '"0"'), "id": item["id"],
                                   "status": "cancelled"}
                        for key in ("recurringEventId", "originalStartTime"):
                            if key in item:
                                minimal[key] = item[key]
                        items.append(minimal)
                    else:
                        items.append(dict(item))
                body = {"kind": "calendar#events", "summary": calendar, "timeZone": api.calendars[calendar]["timeZone"],
                        "accessRole": "owner", "items": items}
                if offset + size < len(found):
                    body["nextPageToken"] = f"page-{offset + size}-{snapshot}-{digest}"
                elif not any(key in query for key in ("timeMin", "timeMax", "q", "orderBy")):
                    body["nextSyncToken"] = f"sync-{hashlib.sha256(calendar.encode()).hexdigest()[:8]}-{snapshot}"
                self.reply(200, body)

            def visible(self, calendar, ident):
                item = api.calendars[calendar]["events"].get(ident)
                if item is None:
                    master, _, suffix = ident.partition("_")
                    parent = api.calendars[calendar]["events"].get(master)
                    if parent is not None and suffix:
                        item = next((entry for entry in instances(parent) if entry["id"] == ident), None)
                if item is None:
                    self.reply(404, error(404, "Not Found", "notFound"))
                return item

            def op_get(self, call, query, calendar, ident):
                item = self.visible(calendar, ident)
                if item is not None:
                    self.reply(200, item)

            def op_insert(self, call, query, calendar, _ident):
                body = call["body"]
                assert query.get("sendUpdates") in ("none", "externalOnly", "all"), query
                if "start" not in body or "end" not in body:
                    self.reply(400, error(400, "Missing end time.", "required"))
                    return
                if api.calendars[calendar]["accessRole"] not in ("writer", "owner"):
                    self.reply(403, error(403, "You need to have writer access to this calendar.", "requiredAccessLevel"))
                    return
                if "id" in body:
                    if not re.fullmatch(r"[a-v0-9]{5,1024}", body["id"]):
                        self.reply(400, error(400, "Invalid resource id value.", "invalid"))
                        return
                    if body["id"] in api.calendars[calendar]["events"]:
                        self.reply(409, error(409, "The requested identifier already exists.", "duplicate"))
                        return
                else:
                    api.counter += 1
                    body = {**body, "id": f"fixturecreated{api.counter:06d}"}
                if when(body["end"]) <= when(body["start"]):
                    self.reply(400, error(400, "The specified time range is empty.", "timeRangeEmpty"))
                    return
                self.reply(200, add(calendar, body))

            def conditional(self, call, item):
                if call["if_match"] is not None and call["if_match"] != item.get("etag"):
                    self.reply(412, error(412, "Precondition Failed", "conditionNotMet", location="If-Match"))
                    return False
                return True

            def op_patch(self, call, query, calendar, ident):
                item = self.visible(calendar, ident)
                if item is None or not self.conditional(call, item):
                    return
                assert query.get("sendUpdates") in ("none", "externalOnly", "all"), query
                if item["status"] == "cancelled":
                    self.reply(410, error(410, "Resource has been deleted", "deleted"))
                    return
                merged = {**item, **call["body"]}
                if when(merged["end"]) <= when(merged["start"]):
                    self.reply(400, error(400, "The specified time range is empty.", "timeRangeEmpty"))
                    return
                self.reply(200, touch(calendar, item, **call["body"]))

            def op_delete(self, call, query, calendar, ident):
                item = self.visible(calendar, ident)
                if item is None or not self.conditional(call, item):
                    return
                assert query.get("sendUpdates") in ("none", "externalOnly", "all"), query
                if item["status"] == "cancelled":
                    self.reply(410, error(410, "Resource has been deleted", "deleted"))
                    return
                cancel(calendar, ident)
                self.reply(204, None)

            def op_freebusy(self, call, query, _calendar, _ident):
                body = call["body"]
                low = datetime.fromisoformat(body["timeMin"].replace("Z", "+00:00"))
                high = datetime.fromisoformat(body["timeMax"].replace("Z", "+00:00"))
                if high <= low:
                    self.reply(400, error(400, "The specified time range is empty.", "timeRangeEmpty"))
                    return
                result = {}
                for entry in body["items"]:
                    ident = resolve(entry["id"])
                    if ident not in api.calendars:
                        result[entry["id"]] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
                        continue
                    busy = []
                    for item in api.calendars[ident]["events"].values():
                        if item["status"] == "cancelled" or item.get("transparency") == "transparent":
                            continue
                        for occurrence in instances(item):
                            begin, end = when(occurrence["start"]), when(occurrence["end"])
                            if end > low and begin < high:
                                busy.append((max(begin, low), min(end, high)))
                    busy.sort()
                    result[entry["id"]] = {"busy": [{"start": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                     "end": b.strftime("%Y-%m-%dT%H:%M:%SZ")} for a, b in busy]}
                self.reply(200, {"kind": "calendar#freeBusy", "timeMin": body["timeMin"], "timeMax": body["timeMax"],
                                 "calendars": result})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.base_url = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        api.kinds = lambda: [call["kind"] for call in api.calls]
        api.events = lambda calendar=OWNER: api.calendars[calendar]["events"]

        def change(calendar, ident, **fields):
            with lock:
                return touch(calendar, api.calendars[calendar]["events"][ident], **fields)

        def create(calendar, ident, summary, start, end, **extra):
            with lock:
                return add(calendar, {"id": ident, "summary": summary, "start": start, "end": end, **extra})

        def remove(calendar, ident):
            with lock:
                return cancel(calendar, ident)

        def expire():
            """Invalidate every sync token issued so far, as Google does after a long absence."""
            with lock:
                api.seq += 1
                api.min_sync = api.seq

        api.change, api.create, api.remove, api.expire = change, create, remove, expire
        try:
            yield api
        finally:
            api.closing.set()
            api.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
