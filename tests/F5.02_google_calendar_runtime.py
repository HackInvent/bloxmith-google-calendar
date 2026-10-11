"""FB2-FB7: loopback Calendar API protocol, changes, errors, the watch poller and listener, and both runtimes.

Every provider exchange targets the loopback double in calendar_fixture.py. The
wallet and its fixture credentials exist only in disposable test storage.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.google_calendar import logic, operations, storage, transport, watcher
from blocs.google_calendar.block import GoogleCalendarBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from calendar_fixture import (ACCESS, GRANT, HIDDEN, HOLIDAYS, MISSING, OFFSITE, OWNER, PAST, PRIVATE, REF, REFRESHED,
                              REVIEW, STANDUP, TEAM, at, calendar_api, error, on, settings)
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, graph_storage_dir,
                             http_json, isolated_server, stop_run_api, text_node, wait_for_run_predicate,
                             wait_for_run_terminal)

TOKEN = {"access_token": ACCESS}
QUOTA = {"status": 429, "body": error(429, "Rate Limit Exceeded", "rateLimitExceeded", "RESOURCE_EXHAUSTED")}
READS = ["list_calendars", "list_events", "get_event", "free_busy"]


def run(config, value, secret=TOKEN, cancel=lambda: False):
    """Execute one request through the real local checks, session and operations."""
    request = logic.parse_request(value, config, datetime.now(timezone.utc))
    return operations.run(transport.Session(config, secret, cancel), request, config)


def failure(call, code):
    try:
        call()
    except logic.CalendarError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        text = json.dumps(exc.envelope())
        assert ACCESS not in text and REFRESHED not in text and GRANT["client_secret"] not in text
        return exc
    raise AssertionError("Expected " + code)


def until(predicate, timeout=10.0, label="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(label)


def summaries(values):
    return [item["summary"] for item in values["events"]]


def read_protocol():
    """# - FB2 - # - FB6 - calendars, events with windows, text and expansion, exact page tokens, one event, free/busy."""
    with calendar_api() as api:
        config = logic.configuration(settings(api, allowed_operations=READS))
        calendars = run(config, {"operation": "list_calendars"})["result"]
        assert [item["id"] for item in calendars["calendars"]] == [OWNER, TEAM, HOLIDAYS] and calendars["count"] == 3
        assert calendars["calendars"][0]["primary"] is True and calendars["calendars"][1]["access_role"] == "writer"
        call = api.calls[-1]
        assert call["query"] == {"maxResults": "250", "showHidden": "false"} and call["auth"] == "Bearer " + ACCESS
        assert call["accept_encoding"] == "identity"
        writable = run(config, {"operation": "list_calendars", "min_access_role": "writer", "show_hidden": True})["result"]
        assert [item["id"] for item in writable["calendars"]] == [OWNER, TEAM]
        assert HIDDEN in [item["id"] for item in run(config, {"operation": "list_calendars", "show_hidden": True})[
            "result"]["calendars"]]
        small = logic.configuration(settings(api, allowed_operations=READS, page_size=2, max_pages=1))
        first = run(small, {"operation": "list_calendars"})["result"]
        rest = run(small, {"operation": "list_calendars", "page_token": first["next_page_token"]})["result"]
        assert first["stop_reason"] == "max_pages" and [item["id"] for item in rest["calendars"]] == [HOLIDAYS]
        # Events: every occurrence by default, sorted by start, nothing outside the calendar.
        listed = run(config, "go")
        assert listed["result"]["count"] == 32 and listed["result"]["calendar_id"] == "primary"
        assert summaries(listed)[:6] == ["Old retro", "Daily stand-up", "Daily stand-up", "Quarterly review", "Dentist",
                                         "Daily stand-up"], summaries(listed)[:6]
        assert listed["events"][1]["recurring_event_id"] == STANDUP and listed["result"]["time_zone"] == "Europe/Paris"
        assert api.calls[-1]["query"] == {"maxResults": "250", "singleEvents": "true", "showDeleted": "false"}
        review = next(item for item in listed["events"] if item["id"] == REVIEW)
        assert review["attendees"][1] == {"email": "guest.two@example.test", "display_name": None, "self": False,
                                          "response_status": "needsAction", "optional": True, "organizer": False,
                                          "resource": False}
        assert review["description"] == "Budget and roadmap" and review["etag"]
        window = run(config, {"time_min": on(0), "time_max": on(2), "order_by": "startTime", "request_id": "w-1",
                              "time_zone": "Europe/Paris"})
        assert summaries(window) == ["Daily stand-up", "Daily stand-up", "Quarterly review"]
        query = api.calls[-1]["query"]
        assert query["timeMin"] == on(-1) + "T23:00:00Z" or query["timeMin"] == on(-1) + "T22:00:00Z", query
        assert query["orderBy"] == "startTime" and query["timeZone"] == "Europe/Paris"
        assert window["result"]["request_id"] == "w-1" and window["result"]["time_min"] == query["timeMin"]
        utc_window = run(config, {"time_min": on(0) + "T00:00:00Z", "time_max": on(2) + "T00:00:00Z"})
        assert summaries(utc_window) == ["Daily stand-up", "Daily stand-up", "Quarterly review"]
        assert summaries(run(config, {"query": "budget"})) == ["Quarterly review"] and api.calls[-1]["query"]["q"] == "budget"
        series = run(config, {"single_events": False})
        assert series["result"]["count"] == 30 and series["events"][1]["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=3"]
        # Page tokens: max_pages stops with the exact next token, which resumes without gap or duplicate.
        paged = logic.configuration(settings(api, allowed_operations=READS, page_size=10, max_pages=2))
        first = run(paged, {"request_id": "p-1"})
        assert first["result"]["count"] == 20 and first["result"]["stop_reason"] == "max_pages" and first["result"]["truncated"]
        rest = run(paged, {"page_token": first["result"]["next_page_token"]})
        assert [item["id"] for item in first["events"] + rest["events"]] == [item["id"] for item in listed["events"]]
        assert rest["result"]["stop_reason"] == "end" and rest["result"]["next_page_token"] is None
        limited = run(paged, {"limit": 12})
        assert [call["query"]["maxResults"] for call in api.calls[-2:]] == ["10", "2"]
        assert limited["result"]["stop_reason"] == "limit" and limited["result"]["count"] == 12
        follow = run(paged, {"page_token": limited["result"]["next_page_token"]})
        assert [item["id"] for item in limited["events"] + follow["events"]] == [item["id"] for item in listed["events"]]
        # One event, including an occurrence of a recurring series.
        got = run(config, {"operation": "get_event", "event_id": REVIEW})
        assert got["events"][0]["summary"] == "Quarterly review" and got["result"]["event_id"] == REVIEW
        occurrence = STANDUP + "_" + at(1, "08:30:00").replace("-", "").replace(":", "")
        instance = run(config, {"operation": "get_event", "event_id": occurrence})["events"][0]
        assert instance["recurring_event_id"] == STANDUP and instance["original_start"]["date_time"] == at(1, "08:30:00")
        missing = failure(lambda: run(config, {"operation": "get_event", "event_id": "fixturemissing01"}), "not_found")
        assert missing.provider_reason == "notFound" and missing.http_status == 404
        failure(lambda: run(config, {"calendar_id": MISSING}), "not_found")
        # Free/busy: opaque events only, one entry per requested calendar, in request order.
        busy = run(config, {"operation": "free_busy", "calendars": ["primary", TEAM, MISSING], "time_min": at(1, "00:00:00"),
                            "time_max": at(2, "00:00:00")})["result"]
        assert busy["calendars"] == [
            {"id": "primary", "busy": [{"start": at(1, "08:30:00"), "end": at(1, "08:45:00")},
                                       {"start": at(1, "14:00:00"), "end": at(1, "15:30:00")}], "errors": []},
            {"id": TEAM, "busy": [{"start": at(1, "15:00:00"), "end": at(1, "16:00:00")}], "errors": []},
            {"id": MISSING, "busy": [], "errors": ["notFound"]}]
        assert busy["busy_count"] == 3 and api.calls[-1]["body"]["items"] == [{"id": "primary"}, {"id": TEAM}, {"id": MISSING}]
        assert "timeZone" not in api.calls[-1]["body"]
        # The refresh-token grant gets one access token per activation; a refused grant reads nothing.
        before = len(api.calls)
        assert run(config, "go", GRANT)["result"]["requests"] == 2
        assert api.kinds()[before:] == ["token", "list"] and api.calls[before + 1]["auth"] == "Bearer " + REFRESHED
        assert api.calls[before]["body"]["grant_type"] == "refresh_token"
        refused = failure(lambda: run(config, "go", {**GRANT, "refresh_token": "revoked-fixture"}), "auth_failed")
        assert refused.provider_status == "invalid_grant" and refused.http_status == 400
        # A page that does not fit the output limit is dropped whole and resumed from its own token.
        bulk = {"time_min": at(31, "00:00:00")}
        sample = run(config, {**bulk, "limit": 1})["events"][0]
        weight = len(json.dumps(sample, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        saved, logic.OUTPUT_LIMIT = logic.OUTPUT_LIMIT, 2 + weight * 5
        try:
            four = logic.configuration(settings(api, allowed_operations=READS, page_size=4))
            capped = run(four, bulk)
            assert capped["result"]["stop_reason"] == "output_limit" and capped["result"]["count"] == 4
            again = run(four, {**bulk, "page_token": capped["result"]["next_page_token"]})
            assert summaries(again)[0] == "Bulk slot 05"
            failure(lambda: run(logic.configuration(settings(api, allowed_operations=READS, page_size=25)), bulk), "protocol")
        finally:
            logic.OUTPUT_LIMIT = saved
    print("[ok] Google Calendar calendars, events, page tokens, occurrences, free/busy and refresh grant", flush=True)


def change_protocol():
    """# - FB3 - # - FB4 - creation, client IDs, conditional patches, deletion, notification policy and calendar gates."""
    with calendar_api() as api:
        config = logic.configuration(settings(api, allow_writes=True, allow_delete=True, send_updates="all",
                                              default_time_zone="Europe/Paris", allowed_operations=list(logic.OPERATIONS)))
        made = run(config, {"operation": "create_event", "summary": "Planning", "start": on(4) + "T10:00:00",
                            "duration_minutes": 45, "attendees": ["guest.three@example.test"], "send_updates": "none",
                            "reminders": {"overrides": [{"method": "popup", "minutes": 15}]}, "request_id": "c-1"})["result"]
        call = api.calls[-1]
        assert call["kind"] == "insert" and call["query"] == {"sendUpdates": "none"} and call["calendar"] == OWNER
        assert call["body"]["start"] == {"dateTime": on(4) + "T10:00:00", "timeZone": "Europe/Paris"}
        assert call["body"]["end"] == {"dateTime": on(4) + "T10:45:00", "timeZone": "Europe/Paris"}
        assert made["created"] is True and made["request_id"] == "c-1" and made["send_updates"] == "none"
        assert made["event"]["summary"] == "Planning" and made["event_id"] in api.events()
        assert made["event"]["attendees"][0]["email"] == "guest.three@example.test"
        allday = run(config, {"operation": "create_event", "summary": "Holiday", "start": on(6)})["result"]
        assert allday["event"]["all_day"] and api.calls[-1]["body"]["end"] == {"date": on(7)}
        assert api.calls[-1]["query"] == {"sendUpdates": "all"}, "The configured notification policy is the default"
        # A client event_id makes a re-sent creation fail as duplicate instead of creating a second event.
        once = {"operation": "create_event", "summary": "Once", "start": at(8, "09:00:00"), "end": at(8, "10:00:00"),
                "event_id": "bloksmith0001"}
        assert run(config, once)["result"]["event_id"] == "bloksmith0001"
        duplicate = failure(lambda: run(config, once), "duplicate")
        assert not duplicate.uncertain and duplicate.http_status == 409
        assert [ident for ident in api.events() if ident == "bloksmith0001"] == ["bloksmith0001"]
        # Patches send only the given fields; an etag makes them conditional.
        current = run(config, {"operation": "get_event", "event_id": REVIEW})["events"][0]
        patched = run(config, {"operation": "update_event", "event_id": REVIEW, "location": "Room 5",
                               "etag": current["etag"]})["result"]
        assert api.calls[-1]["body"] == {"location": "Room 5"} and api.calls[-1]["if_match"] == current["etag"]
        assert api.calls[-1]["kind"] == "patch" and api.calls[-1]["query"] == {"sendUpdates": "all"}
        assert patched["fields"] == ["location"] and patched["event"]["location"] == "Room 5"
        assert patched["event"]["description"] == "Budget and roadmap", "Fields not sent are kept"
        stale = failure(lambda: run(config, {"operation": "update_event", "event_id": REVIEW, "summary": "Late",
                                              "etag": current["etag"]}), "conflict")
        assert stale.http_status == 412 and api.events()[REVIEW]["summary"] == "Quarterly review"
        failure(lambda: run(config, {"operation": "update_event", "event_id": REVIEW, "start": at(1, "16:00:00")}),
                "bad_request")
        moved = run(config, {"operation": "update_event", "event_id": REVIEW, "start": at(1, "15:00:00"),
                             "end": at(1, "16:00:00"), "attendees": []})["result"]
        assert moved["event"]["start"]["date_time"] == at(1, "15:00:00") and moved["event"]["attendees"] == []
        # Deletion: 204, then an already deleted event counts as deleted; wrong etags and unknown events are refused.
        gone = run(config, {"operation": "delete_event", "event_id": PRIVATE, "send_updates": "none"})["result"]
        assert gone == {"request_id": None, "operation": "delete_event", "calendar_id": "primary", "event_id": PRIVATE,
                        "deleted": True, "already_deleted": False, "send_updates": "none", "requests": 1}
        assert api.events()[PRIVATE]["status"] == "cancelled" and api.calls[-1]["kind"] == "delete"
        assert run(config, {"operation": "delete_event", "event_id": PRIVATE})["result"]["already_deleted"] is True
        failure(lambda: run(config, {"operation": "delete_event", "event_id": "fixturemissing01"}), "not_found")
        failure(lambda: run(config, {"operation": "delete_event", "event_id": OFFSITE, "etag": '"1"'}), "conflict")
        assert api.events()[OFFSITE]["status"] == "confirmed"
        # Calendar and notification gates are enforced before anything is sent.
        before = len(api.calls)
        failure(lambda: run(config, {"operation": "create_event", "summary": "x", "start": on(9), "calendar_id": TEAM}),
                "calendar_not_allowed")
        failure(lambda: run(logic.configuration({**config, "send_updates": "none"}),
                            {"operation": "delete_event", "event_id": OFFSITE, "send_updates": "all"}), "invalid_input")
        assert len(api.calls) == before, "Refused changes send nothing"
        chooser = logic.configuration({**config, "allow_request_calendar": True})
        team = run(chooser, {"operation": "create_event", "summary": "Team lunch", "start": on(9), "calendar_id": TEAM})
        assert team["result"]["event_id"] in api.events(TEAM) and api.calls[-1]["calendar"] == TEAM
        refused = failure(lambda: run(chooser, {"operation": "create_event", "summary": "x", "start": on(9),
                                                "calendar_id": HOLIDAYS}), "forbidden")
        assert refused.provider_reason == "requiredAccessLevel" and not refused.uncertain
    print("[ok] Google Calendar creation, duplicates, conditional patches, deletion and gates", flush=True)


def error_protocol():
    """# - FB6 - 401/403/404/429/5xx, rate-limit 403, lost connections, uncertain changes, cancellation, deadline, TLS."""
    with calendar_api() as api:
        config = logic.configuration(settings(api, allow_writes=True, allow_delete=True, max_retry_wait_sec=2,
                                              allowed_operations=list(logic.OPERATIONS)))
        create = {"operation": "create_event", "summary": "Retry", "start": at(10, "09:00:00"), "end": at(10, "09:30:00")}
        expired = failure(lambda: run(config, "go", {"access_token": "expired-fixture-token"}), "unauthorized")
        assert expired.provider_status == "UNAUTHENTICATED" and expired.provider_reason == "authError" and not expired.uncertain
        api.script["list"] = [{"status": 403, "body": error(403, "Insufficient Permission", "insufficientPermissions")}]
        assert failure(lambda: run(config, "go"), "forbidden").provider_reason == "insufficientPermissions"
        api.script["list"] = [{"status": 403, "body": error(403, "Calendar usage limits exceeded.", "quotaExceeded")}]
        before = len(api.calls)
        assert failure(lambda: run(config, "go"), "quota_exceeded").attempts == 1 and len(api.calls) == before + 1
        # Rate limits (429 or a 403 rate-limit reason) honour Retry-After within bounds, for reads and changes.
        api.script["list"] = [{"status": 403, "body": error(403, "User Rate Limit Exceeded", "userRateLimitExceeded"),
                               "headers": {"Retry-After": "1"}}]
        started = time.monotonic()
        assert run(config, "go")["result"]["requests"] == 2 and time.monotonic() - started >= 1
        api.script["insert"] = [{**QUOTA, "headers": {"Retry-After": "1"}}]
        before = len(api.events())
        assert run(config, create)["result"]["created"] and api.kinds()[-2:] == ["insert", "insert"]
        assert len(api.events()) == before + 1, "A rate-limited creation was not processed, so it is sent once more"
        api.script["list"] = [{**QUOTA, "headers": {"Retry-After": "30"}}]
        limited = failure(lambda: run(config, "go"), "rate_limited")
        assert limited.retry_after == 30 and limited.attempts == 1
        api.script["list"] = [QUOTA] * 3
        assert failure(lambda: run(logic.configuration({**config, "max_retries": 0}), "go"), "rate_limited").attempts == 1
        api.script["list"] = ["html502", "lost"]
        before = len(api.calls)
        assert run(config, "go")["result"]["count"] >= 32 and api.kinds()[before:] == ["list"] * 3
        for scripted, code in (({"status": 503, "body": error(503, "Backend Error", "backendError")}, "provider_unavailable"),
                               ("html502", "provider_unavailable"), ("lost", "network"), ("oversized", "protocol"),
                               ("gzip", "protocol")):
            api.script["insert"] = [scripted]
            before = len(api.calls)
            outcome = failure(lambda: run(logic.configuration({**config, "max_response_bytes": 65536}), create), code)
            assert outcome.uncertain and api.kinds()[before:].count("insert") == 1, scripted
        api.script["delete"] = ["lost"]
        assert failure(lambda: run(config, {"operation": "delete_event", "event_id": OFFSITE}), "network").uncertain
        api.script["list"] = ["oversized"]
        assert not failure(lambda: run(logic.configuration({**config, "max_response_bytes": 65536, "max_retries": 0}), "go"),
                           "protocol").uncertain
        api.script["list"] = [{"status": 400, "body": error(400, "Invalid Value", "invalid", location="timeMin")}]
        assert failure(lambda: run(config, "go"), "bad_request").provider_reason == "invalid"
        # Cancellation and the deadline shut the socket down while Google holds the request.
        api.script["insert"] = ["hold"]
        started = time.monotonic()
        cancelled = failure(lambda: run(config, create, cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
        assert cancelled.uncertain and time.monotonic() - started < 2.5
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["list"] = ["hold"]
        started = time.monotonic()
        quiet = failure(lambda: run(config, "go", cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
        assert not quiet.uncertain and time.monotonic() - started < 2.5
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["patch"] = ["hold"]
        started = time.monotonic()
        timed = failure(lambda: run(logic.configuration({**config, "timeout_sec": 5}),
                                    {"operation": "update_event", "event_id": REVIEW, "summary": "Slow"}), "timeout")
        assert timed.uncertain and 4 <= time.monotonic() - started < 8
        api.release.set()
        time.sleep(0.2)
        assert not [thread for thread in threading.enumerate() if thread.name == transport.THREAD_NAME]
        assert not failure(lambda: run(logic.configuration({**config, "api_base_url": "http://127.0.0.1:9",
                                                            "max_retries": 0}), "go"), "network").uncertain
        failure(lambda: run(config, "go", cancel=lambda: True), "cancelled")
    with calendar_api(tls=True) as api:
        config = logic.configuration(settings(api))
        assert run(config, "go")["result"]["count"] == 32
        before = len(api.calls)
        untrusted = failure(lambda: run(logic.configuration({**settings(api), "ca_file": ""}), "go"), "tls")
        assert not untrusted.uncertain and len(api.calls) == before
    print("[ok] Google Calendar errors, retries, uncertainty, cancellation, deadline and TLS", flush=True)


class Clock:
    """Wall clock for poll intervals: tests move it forward instead of sleeping 60 seconds."""

    def __init__(self):
        self.value = int(time.time())

    def __call__(self):
        return self.value

    def advance(self, seconds=61):
        self.value += seconds


def poll(config, folder, clock, secret=TOKEN, advance=61, cancel=lambda: False):
    clock.advance(advance)
    return watcher.poll_once(config, {"request_id": "w", "operation": "watch"}, storage_dir=lambda: folder,
                             resolve=lambda: secret, cancel=cancel, wall=clock)


def kinds(batch):
    return [(item["change"], item["event"]["id"]) for item in batch]


def watch_protocol():
    """# - FB5 - baseline without flood, created/updated/cancelled once, queue bounds, 410 resync, emit_existing, 60 s."""
    with calendar_api() as api, tempfile.TemporaryDirectory() as folder:
        folder, clock = Path(folder), Clock()
        config = logic.configuration(settings(api, operation="watch", allowed_operations=["watch"]))
        batch, receipt = poll(config, folder, clock)
        assert batch == [] and receipt["baseline"] is True and receipt["baseline_complete"] is True
        assert receipt["phase"] == "incremental" and receipt["pages"] == 1 and receipt["count"] == 0
        assert api.kinds() == ["calendar_get", "list"]
        assert api.calls[-1]["query"] == {"maxResults": "250", "showDeleted": "true", "singleEvents": "false"}
        state = (folder / storage.STATE_NAME).read_text()
        assert "syncToken" not in state and OWNER not in state and "Quarterly" not in state and ACCESS not in state
        too_soon = failure(lambda: poll(config, folder, clock, advance=10), "too_soon")
        assert too_soon.retry_after == 50 and len(api.calls) == 2, "A poll within 60 s sends nothing"
        # Each change is reported once, in Google's order.
        api.create(OWNER, "bloknew00001", "Kickoff", {"dateTime": at(12, "09:00:00")}, {"dateTime": at(12, "10:00:00")})
        api.change(OWNER, REVIEW, location="Room 9")
        api.remove(OWNER, PRIVATE)
        before = len(api.calls)
        batch, receipt = poll(config, folder, clock)
        assert kinds(batch) == [("created", "bloknew00001"), ("updated", REVIEW), ("cancelled", PRIVATE)]
        assert batch[0]["event"]["summary"] == "Kickoff" and batch[1]["event"]["location"] == "Room 9"
        assert batch[2]["event"]["status"] == "cancelled" and batch[2]["event"]["summary"] is None
        assert all(item["calendar_id"] == "primary" for item in batch) and receipt["pending"] == 0
        assert api.kinds()[before:] == ["calendar_get", "list", "get", "get"]
        assert "syncToken" in api.calls[before + 1]["query"] and "timeMin" not in api.calls[before + 1]["query"]
        assert poll(config, folder, clock)[0] == [], "Nothing is replayed"
        api.change(OWNER, "bloknew00001", summary="Kickoff v2")
        assert kinds(poll(config, folder, clock)[0]) == [("updated", "bloknew00001")]
        # watch_max_changes bounds a poll; the rest waits in order. A merged later state wins.
        two = logic.configuration({**config, "watch_max_changes": 2})
        for index in range(3):
            api.create(OWNER, f"blokqueue000{index}", f"Queued {index}", {"date": on(20 + index)}, {"date": on(21 + index)})
        batch, receipt = poll(two, folder, clock)
        assert kinds(batch) == [("created", "blokqueue0000"), ("created", "blokqueue0001")] and receipt["pending"] == 1
        api.remove(OWNER, "blokqueue0002")
        assert kinds(poll(two, folder, clock)[0]) == [("cancelled", "blokqueue0002")]
        # An event that cannot be read any more is skipped; a transient failure keeps it queued.
        api.create(OWNER, "blokskip0001", "Skipped", {"date": on(25)}, {"date": on(26)})
        api.script["get"] = [{"status": 404, "body": error(404, "Not Found", "notFound")}]
        batch, receipt = poll(config, folder, clock)
        assert batch == [] and receipt["skipped"] == 1 and receipt["pending"] == 0
        api.create(OWNER, "bloklate0001", "Late", {"date": on(26)}, {"date": on(27)})
        api.script["get"] = [{"status": 503, "body": error(503, "Backend Error", "backendError")}] * 3
        batch, receipt = poll(logic.configuration({**config, "max_retries": 0}), folder, clock)
        assert batch == [] and receipt["incomplete"] and receipt["deferred_error"] == "provider_unavailable"
        assert receipt["pending"] == 1
        api.script["get"] = []
        assert kinds(poll(config, folder, clock)[0]) == [("created", "bloklate0001")]
        # A failed poll changes nothing but still counts for the 60-second minimum.
        api.create(OWNER, "blokafter001", "After failure", {"date": on(27)}, {"date": on(28)})
        api.script["calendar_get"] = [{"status": 401, "body": error(401, "Invalid Credentials", "authError")}]
        failure(lambda: poll(config, folder, clock), "unauthorized")
        failure(lambda: poll(config, folder, clock, advance=5), "too_soon")
        assert kinds(poll(config, folder, clock)[0]) == [("created", "blokafter001")]
        # The watch never uses a wallet credential it does not need, and storage holds no event content.
        state = json.loads((folder / storage.STATE_NAME).read_text())
        assert len(state["watches"]) == 1 and "Kickoff" not in json.dumps(state) and ACCESS not in json.dumps(state)
    with calendar_api() as api, tempfile.TemporaryDirectory() as folder:
        # Google invalidates the sync token (HTTP 410): one bounded full resynchronization, then incremental again.
        folder, clock = Path(folder), Clock()
        config = logic.configuration(settings(api, operation="watch", page_size=10, max_pages=1))
        receipts = []
        while not receipts or not receipts[-1]["baseline_complete"]:
            batch, receipt = poll(config, folder, clock)
            assert batch == [], "A multi-page baseline reports nothing"
            receipts.append(receipt)
        assert len(receipts) == 3 and receipts[0]["phase"] == "baseline" and receipts[-1]["phase"] == "incremental"
        api.expire()
        api.change(OWNER, REVIEW, summary="Review moved")
        api.remove(OWNER, PRIVATE)
        found = []
        for _ in range(3):
            batch, receipt = poll(config, folder, clock)
            found.extend(batch)
            if receipt["phase"] == "incremental":
                break
        # Google keeps only the ID of a deleted event, so a resync cannot date that deletion and does not report it.
        assert kinds(found) == [("updated", REVIEW)], kinds(found)
        assert all(not call["query"].get("syncToken") for call in api.calls[-3:] if call["kind"] == "list")
        assert any(call["kind"] == "list" and "syncToken" in call["query"] for call in api.calls)
        assert poll(config, folder, clock)[0] == [] and receipt["phase"] == "incremental"
    with calendar_api() as api, tempfile.TemporaryDirectory() as folder:
        # emit_existing: upcoming events once, at the first poll only; past events never.
        folder, clock = Path(folder), Clock()
        config = logic.configuration(settings(api, operation="watch", emit_existing=True))
        batch, receipt = poll(config, folder, clock)
        reported = [item["event"]["id"] for item in batch]
        assert {item["change"] for item in batch} == {"existing"} and len(batch) == 29 and PAST not in reported
        assert reported[:2] == [STANDUP, REVIEW] and receipt["baseline"] is True
        assert poll(config, folder, clock)[0] == []
    print("[ok] Google Calendar watch baseline, changes, queue, skips, 410 resync, emit_existing and interval", flush=True)


def listener(config, folder, emitted, stop, clock, secret=TOKEN, emit=None):
    def collect(kind, value, stats):
        if stop.is_set():
            raise RuntimeError("stopped")
        emitted.append((kind, value, stats))
    clock.advance(1000)
    instance = watcher.Watcher({**config, "watch_interval_sec": 0.3}, resolve_secret=lambda: secret,
                               storage_dir=lambda: folder, emit=emit or collect, stop=stop.is_set, wall=clock)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    return thread


def finish(stop, thread):
    started = time.monotonic()
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 1.5, "Listener did not stop promptly"


def listener_protocol():
    """# - FB5 - # - FB7 - the Run listener: periodic polls, no replay after restart, refused hand-off, failures, Stop."""
    with calendar_api() as api, tempfile.TemporaryDirectory() as folder:
        folder, clock = Path(folder), Clock()
        config = logic.configuration(settings(api, watch_enabled=True))
        changes = lambda: [item for kind, value, _ in emitted if kind == "changed" for item in value]
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: any(kind == "status" and stats["state"] == "listening" for kind, _, stats in emitted), 10)
        api.create(OWNER, "bloklisten01", "Heard", {"date": on(30)}, {"date": on(31)})
        until(lambda: changes(), 10, "the listener did not report the new event")
        assert kinds(changes()) == [("created", "bloklisten01")]
        stats = emitted[-1][2]
        assert stats["phase"] == "incremental" and stats["emitted"] >= 0 and stats["errors"] == 0
        time.sleep(0.8)
        assert len(changes()) == 1, "Each change is reported once"
        finish(stop, thread)
        # A new Run resumes from the stored position: nothing already handed off is replayed.
        api.change(OWNER, OFFSITE, summary="Offsite moved")
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: changes(), 10, "restart did not report the change")
        time.sleep(0.5)
        assert kinds(changes()) == [("updated", OFFSITE)]
        finish(stop, thread)
        # A refused hand-off restores the stored position, so the next Run reports the change again.
        api.create(OWNER, "blokrefused1", "Refused", {"date": on(32)}, {"date": on(33)})
        refused, stop = [], threading.Event()

        def flaky(kind, value, stats):
            if kind == "changed":
                refused.append(value)
                raise RuntimeError("runtime_listeners.queue_full")
        thread = listener(config, folder, [], stop, clock, emit=flaky)
        until(lambda: refused, 10, "refused hand-off not attempted")
        time.sleep(0.3)
        finish(stop, thread)
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: changes(), 10, "the refused change was not reported again")
        assert kinds(changes()) == [("created", "blokrefused1")]
        finish(stop, thread)
        # Failures are reported once per distinct code; malformed state, a held lock or no storage stop polling.
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock, secret={"access_token": "wrong-fixture-token"})
        until(lambda: [value for kind, value, _ in emitted if kind == "error"], 10)
        time.sleep(1)
        errors = [value for kind, value, _ in emitted if kind == "error"]
        assert [item["code"] for item in errors] == ["unauthorized"] and errors[0]["source"] == "watch"
        assert errors[0]["operation"] == "watch" and ACCESS not in json.dumps(errors)
        finish(stop, thread)
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock, secret="not a token {")
        until(lambda: any(kind == "error" and value["code"] == "secret" for kind, value, _ in emitted), 10)
        finish(stop, thread)
        holder = storage.WatchStore(folder)
        assert holder.acquire()
        before = len(api.calls)
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: any(kind == "error" and value["code"] == "busy" for kind, value, _ in emitted), 10)
        assert len(api.calls) == before
        holder.close()
        until(lambda: any(kind == "status" and stats["state"] == "listening" for kind, _, stats in emitted), 10)
        finish(stop, thread)
        (folder / storage.STATE_NAME).write_text("{broken")
        before = len(api.calls)
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: any(kind == "error" and value["code"] == "state_invalid" for kind, value, _ in emitted), 10)
        time.sleep(0.3)
        assert len(api.calls) == before, "A malformed state must stop polling before any request"
        finish(stop, thread)
        emitted, stop = [], threading.Event()
        missing = watcher.Watcher(config, resolve_secret=lambda: TOKEN, emit=lambda kind, value, stats: emitted.append(
            (kind, value, stats)), storage_dir=lambda: (_ for _ in ()).throw(RuntimeError("block_storage.missing_scope")),
            stop=stop.is_set)
        thread = threading.Thread(target=missing.run, daemon=True)
        thread.start()
        until(lambda: any(kind == "error" and value["code"] == "storage" for kind, value, _ in emitted), 10)
        finish(stop, thread)
        # Stop while Google holds a listing ends the listener promptly.
        (folder / storage.STATE_NAME).unlink()
        api.script["list"] = ["hold"]
        emitted, stop = [], threading.Event()
        thread = listener(config, folder, emitted, stop, clock)
        until(lambda: api.holding >= 1, 10, "held listing not reached")
        finish(stop, thread)
        api.release.set()
    print("[ok] Google Calendar listener polls, restart, refused hand-off, failures and Stop", flush=True)


def value(data, port):
    raw = data.get("output_values", {}).get("calendar:" + str(port), {}).get("value")
    return json.loads(raw) if raw else None


def wallet(server, secret):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-calendar-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST",
              payload={"name": "google_calendar_fixture", "value": json.dumps(secret)})


def clean(data):
    text = json.dumps(data)
    assert ACCESS not in text and REFRESHED not in text and GRANT["client_secret"] not in text


def rewind(path):
    """Simulate the 60 s minimum having elapsed: move the stored last poll back in the node's watch state."""
    document = json.loads(path.read_text())
    document["last_poll"] -= 120
    path.write_text(json.dumps(document))


def graph_runs():
    """# - FB7 - real graphs in One Shot and Active Runtime for bundled, managed and linked packages, with prompt Stop."""
    block = GoogleCalendarBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            with calendar_api() as api, isolated_server() as server:
                node = block.build_node_payload(node_id="calendar", position={"x": 380, "y": 160}, config_overrides=settings(
                    api, allow_writes=True, timeout_sec=20, watch_enabled=True, emit_existing=True,
                    allowed_operations=["list_events", "get_event", "create_event", "free_busy", "watch"]))
                if origin:
                    model = install_test_package(server, "google_calendar", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and ACCESS not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_operations"' in html
                wallet(server, GRANT if full else {"access_token": ACCESS})
                sinks = [display_node("listed", "Events", 760, 20), display_node("changes", "Changes", 760, 160),
                         display_node("receipt", "Result", 760, 300), display_node("problem", "Problem", 760, 440)]
                edges = [data_edge("o1", "calendar", 1, "listed", 1), data_edge("o2", "calendar", 2, "changes", 1),
                         data_edge("o3", "calendar", 3, "receipt", 1), data_edge("o4", "calendar", 4, "problem", 1)]

                def graph(request=""):
                    return graph_payload("Calendar", [text_node("req", "Request", request, 60, 160), node, *sinks],
                                         [data_edge("in", "req", 1, "calendar", 1), *edges])

                if mode == "centralized":
                    def once(document, project=None, expect="success"):
                        project = project or create_project_api(server, document=document)["project"]
                        created = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == expect, (origin, data.get("status"), data.get("logs"))
                        clean(data)
                        return data, project

                    data, _ = once(graph(json.dumps({"request_id": "q-1", "time_min": on(0), "time_max": on(2)})))
                    assert [item["summary"] for item in value(data, 1)] == ["Daily stand-up", "Daily stand-up",
                                                                             "Quarterly review"]
                    assert value(data, 3)["request_id"] == "q-1" and value(data, 3)["count"] == 3
                    data, _ = once(graph(json.dumps({"operation": "create_event", "summary": "Graph " + str(origin),
                                                     "start": on(14)})))
                    assert value(data, 3)["event"]["summary"] == "Graph " + str(origin) and value(data, 4) is None
                    assert api.kinds().count("insert") == 1
                    # One poll per watch request: the first records the position (and reports upcoming events once).
                    watch = graph(json.dumps({"operation": "watch", "request_id": "w-1"}))
                    data, project = once(watch)
                    assert value(data, 3)["baseline"] is True and value(data, 3)["request_id"] == "w-1"
                    existing = value(data, 2)
                    assert existing and {item["change"] for item in existing} == {"existing"}
                    state = (graph_storage_dir(server, project) / "instances" / "1" / "blocs" / "calendar"
                             / storage.STATE_NAME)
                    assert state.is_file() and OWNER not in state.read_text()
                    data, _ = once(watch, project)
                    assert value(data, 4)["code"] == "too_soon" and value(data, 2) is None
                    rewind(state)
                    api.create(OWNER, "blokgraph0001", "Graph change", {"date": on(15)}, {"date": on(16)})
                    data, _ = once(watch, project)
                    assert [(item["change"], item["event"]["id"]) for item in value(data, 2)] == [
                        ("created", "blokgraph0001")]
                    assert data["results"]["calendar"]["google_calendar"]["error_codes"] == []
                    if full:
                        before = len(api.calls)
                        data, _ = once(graph(json.dumps({"operation": "delete_event", "event_id": REVIEW})))
                        assert value(data, 4)["code"] == "operation_not_allowed" and len(api.calls) == before
                        assert data["results"]["calendar"]["google_calendar"]["error_codes"] == ["operation_not_allowed"]
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        data, _ = once(graph("go"))
                        assert value(data, 4)["code"] == "secret" and len(api.calls) == before
                        http_json(server.base_url, "/api/application/secrets/unlock", method="POST",
                                  payload={"password": "disposable-calendar-fixture"})
                        # Stop while Google holds a creation: the run ends promptly and nothing is retried.
                        api.script["insert"] = ["hold"]
                        document = graph(json.dumps({"operation": "create_event", "summary": "Held", "start": on(16)}))
                        project = create_project_api(server, document=document)["project"]
                        rid = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)["run_id"]
                        until(lambda: api.holding >= 1, 20, "held creation not reached")
                        held, started = len(api.calls), time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert stopped["status"] == "cancelled", (stopped["status"], value(stopped, 4), stopped.get("logs"))
                        assert value(stopped, 3) is None and len(api.calls) == held
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        api.release.set()
                else:
                    document = graph()
                    project = create_project_api(server, document=document)["project"]
                    state = graph_storage_dir(server, project) / "instances" / "1" / "blocs" / "calendar" / storage.STATE_NAME

                    def publish(rid, text):
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "req", "port_id": 1, "value": text,
                            "content_type": "text/plain"})

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        clean(data)
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    def start():
                        rid = prepare_release_run(server, project["graph_id"], document)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        return rid

                    rid = start()
                    try:
                        # The listener's first poll records the position and reports upcoming events once.
                        data = wait(rid, lambda item: value(item, 2), "existing events missing")
                        assert {item["change"] for item in value(data, 2)} == {"existing"}
                        publish(rid, json.dumps({"request_id": "live-1", "time_min": on(0), "time_max": on(2)}))
                        data = wait(rid, lambda item: (value(item, 3) or {}).get("request_id") == "live-1", "listing missing")
                        assert [item["summary"] for item in value(data, 1)][-1] == "Quarterly review"
                        # A requested poll cannot run beside the Run listener of the same node.
                        publish(rid, json.dumps({"request_id": "live-2", "operation": "watch"}))
                        data = wait(rid, lambda item: (value(item, 4) or {}).get("request_id") == "live-2", "busy missing")
                        assert value(data, 4)["code"] in ("busy", "too_soon")
                        if full:
                            publish(rid, json.dumps({"request_id": "live-3", "operation": "create_event",
                                                     "summary": "Live", "start": on(17)}))
                            data = wait(rid, lambda item: (value(item, 3) or {}).get("request_id") == "live-3", "creation missing")
                            live_id = value(data, 3)["event_id"]
                    finally:
                        stop(rid)
                    assert state.is_file()
                    if full:
                        # A new Run resumes from the stored position: only what changed since is reported.
                        rewind(state)
                        api.create(OWNER, "blokactive001", "Between runs", {"date": on(18)}, {"date": on(19)})
                        rid = start()
                        try:
                            data = wait(rid, lambda item: any(change["event"]["id"] == "blokactive001"
                                                              for change in value(item, 2) or []), "restart change missing")
                            reported = [(item["change"], item["event"]["id"]) for item in value(data, 2)]
                            assert reported == [("created", live_id), ("created", "blokactive001")], reported
                        finally:
                            stop(rid)
                logs = "\n".join(server.logs)
                assert ACCESS not in logs and REFRESHED not in logs and GRANT["client_secret"] not in logs
            print("[ok] Google Calendar graph " + mode + " " + str(origin), flush=True)


def main():
    read_protocol()
    change_protocol()
    error_protocol()
    watch_protocol()
    listener_protocol()
    graph_runs()


if __name__ == "__main__":
    main()
