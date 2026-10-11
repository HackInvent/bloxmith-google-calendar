"""FB1-FB8: pure settings, requests, times, event bodies, resources, errors, watch state and block hooks.

No socket is opened and no provider is contacted; the wallet resolver is a local double.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.google_calendar import logic, storage
from blocs.google_calendar.block import GoogleCalendarBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from calendar_fixture import ACCESS, GRANT, REF, TEAM

NOW = datetime(2026, 10, 11, 8, 30, 15, tzinfo=timezone.utc)
BASE = {"enabled": True, "secret_ref": REF}
WRITER = {**BASE, "allow_writes": True, "allow_delete": True, "send_updates": "all",
          "allowed_operations": list(logic.OPERATIONS)}
EVENT = "abcdefghij0123456789"


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.CalendarError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert ACCESS not in str(exc) and GRANT["client_secret"] not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def parse(value, config):
    return logic.parse_request(value, config, NOW)


def settings_contract():
    """# - FB1 - remote access, changes, deletion and watching stay off by default; every setting is bounded."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["allow_writes"] is False and defaults["allow_delete"] is False
    assert defaults["watch_enabled"] is False and defaults["emit_existing"] is False and defaults["send_updates"] == "none"
    assert defaults["api_base_url"] == logic.OFFICIAL_API == "https://www.googleapis.com"
    assert defaults["oauth_base_url"] == "https://oauth2.googleapis.com" and defaults["calendar_id"] == "primary"
    assert logic.selectable_operations(defaults) == ["list_calendars", "list_events", "get_event", "free_busy"]
    assert defaults["watch_interval_sec"] == 300 and logic.BOUNDS["watch_interval_sec"][0] == 60
    assert set(GoogleCalendarBlock().default_config()) == set(logic.DEFAULTS)
    config = logic.configuration({**WRITER, "execution": {"inhibited": False}, "calendar_id": " " + TEAM + " ",
                                  "default_time_zone": " Europe/Paris "})
    assert config["calendar_id"] == TEAM and config["default_time_zone"] == "Europe/Paris"
    assert logic.selectable_operations(config) == list(logic.OPERATIONS)
    frozen = logic.configuration(MappingProxyType({**WRITER, "allowed_operations": ("watch",)}))
    assert frozen["allowed_operations"] == ["watch"] and logic.selectable_operations(frozen) == ["list_events", "watch"]
    for good in ("en.usa#holiday@group.v.calendar.google.com", "c_0123abcd@group.calendar.google.com",
                 "someone@example.test"):
        assert logic.configuration({**BASE, "calendar_id": good})["calendar_id"] == good
    for raw in ({"enabled": True}, {**BASE, "secret_ref": ""}, {**BASE, "secret_ref": ACCESS},
                {**BASE, "secret_ref": "env://GOOGLE"}, {**BASE, "token": ACCESS}, {**BASE, "access_token": ACCESS},
                {**BASE, "calendar_id": ""}, {**BASE, "calendar_id": "https://calendar.google.com/calendar/u/0/r"},
                {**BASE, "calendar_id": "no-at-sign"}, {**BASE, "calendar_id": 7}, {**BASE, "operation": "purge"},
                {**BASE, "operation": "create_event"}, {**BASE, "allowed_operations": ["update_event"]},
                {**BASE, "allow_writes": True, "allowed_operations": ["delete_event"]},
                {**BASE, "allowed_operations": []}, {**BASE, "allowed_operations": ["watch", "watch"]},
                {**BASE, "allowed_operations": "watch"}, {**BASE, "default_time_zone": "Mars/Olympus"},
                {**BASE, "default_time_zone": "+01:00"}, {**BASE, "default_time_zone": 1},
                {**BASE, "enabled": "yes"}, {**BASE, "allow_writes": 1}, {**BASE, "watch_enabled": None},
                {**BASE, "send_updates": "everyone"}, {**BASE, "ca_file": "ca.pem"},
                {**BASE, "api_base_url": "http://www.googleapis.com"}, {**BASE, "api_base_url": "https://www.googleapis.com/calendar/v3"},
                {**BASE, "api_base_url": "https://user:pw@www.googleapis.com"}, {**BASE, "api_base_url": "https://x.test/?a=1"},
                {**BASE, "api_base_url": "ftp://www.googleapis.com"}, {**BASE, "api_base_url": "https://bad host"},
                {**BASE, "oauth_base_url": "http://example.test:8080"}):
        rejected(lambda raw=raw: logic.configuration(raw), "config")
    for key, (low, high) in logic.BOUNDS.items():
        for bad in (low - 1, high + 1, True, float(low), str(low)):
            rejected(lambda key=key, bad=bad: logic.configuration({**BASE, key: bad}), "config")
        assert logic.configuration({**BASE, key: low})[key] == low and logic.configuration({**BASE, key: high})[key] == high
    origins = {"https://www.googleapis.com/": "https://www.googleapis.com", "http://127.0.0.1:8081": "http://127.0.0.1:8081",
               "http://localhost:9": "http://localhost:9", "http://[::1]:7": "http://[::1]:7",
               "https://Calendar.Example.test:8443": "https://calendar.example.test:8443"}
    for raw, expected in origins.items():
        assert logic.configuration({**BASE, "api_base_url": raw, "oauth_base_url": raw})["api_base_url"] == expected


def credential_contract():
    """# - FB1 - # - FB6 - wallet values: bare token, access_token JSON or refresh grant; never echoed."""
    assert logic.credentials(" " + ACCESS + "\n") == {"access_token": ACCESS}
    assert logic.credentials({"access_token": ACCESS}) == {"access_token": ACCESS}
    assert logic.credentials(json.dumps(GRANT)) == GRANT
    for bad in ("", None, 12, {"access_token": ""}, {"access_token": "a b"}, {**GRANT, "extra": "x"},
                {"client_id": "a", "refresh_token": "b"}, '{"access_token": ', json.dumps({"access_token": 5}),
                {"type": "service_account", "private_key": "-----BEGIN " + "PRIVATE KEY-----"}):
        error = rejected(lambda bad=bad: logic.credentials(bad), "secret")
        assert "BEGIN" not in json.dumps(error.envelope())
    text = logic.redact("Bearer " + ACCESS + " secret=" + GRANT["client_secret"], [ACCESS, GRANT["client_secret"], ""])
    assert ACCESS not in text and GRANT["client_secret"] not in text and text.count("***") == 2


def read_requests():
    """# - FB2 - listings, one event and free/busy: explicit, bounded and validated before the wallet."""
    reader = logic.configuration({**BASE, "default_time_zone": "Europe/Paris", "allowed_operations": list(logic.OPERATIONS[:5])})
    trigger = parse("go", reader)
    assert trigger == {"operation": "list_events", "request_id": None, "calendar_id": "primary", "limit": 250,
                       "page_token": None, "single_events": True, "show_deleted": False, "order_by": None, "query": None,
                       "time_zone": "Europe/Paris"}
    listed = parse({"calendar_id": TEAM, "time_min": "now", "time_max": "2026-10-12", "query": " review ",
                    "order_by": "startTime", "limit": 5, "page_token": "tok-1", "request_id": "r-1"}, reader)
    assert listed["time_min"] == "2026-10-11T08:30:15Z" and listed["time_max"] == "2026-10-11T22:00:00Z"
    assert (listed["calendar_id"], listed["query"], listed["limit"], listed["page_token"]) == (TEAM, "review", 5, "tok-1")
    utc = logic.configuration(BASE)
    window = parse({"time_min": "2026-11-02", "time_max": "2026-11-03T10:00:00+02:00"}, utc)
    assert window["time_min"] == "2026-11-02T00:00:00Z" and window["time_max"] == "2026-11-03T08:00:00Z"
    assert window["time_zone"] is None and window["single_events"] is True
    series = parse({"single_events": False, "show_deleted": True, "order_by": "updated", "time_zone": "Asia/Tokyo"}, utc)
    assert not series["single_events"] and series["show_deleted"] and series["time_zone"] == "Asia/Tokyo"
    assert parse({"operation": "get_event", "event_id": " " + EVENT + "_20261102T083000Z "}, utc)["event_id"] == \
        EVENT + "_20261102T083000Z"
    calendars = parse({"operation": "list_calendars", "min_access_role": "writer", "show_hidden": True, "limit": 3}, utc)
    assert calendars == {"operation": "list_calendars", "request_id": None, "limit": 3, "page_token": None,
                         "min_access_role": "writer", "show_hidden": True}
    busy = parse({"operation": "free_busy", "calendars": ["primary", TEAM], "time_min": "2026-11-03",
                  "time_max": "2026-11-04", "time_zone": "Europe/Paris"}, utc)
    assert busy["calendars"] == ["primary", TEAM] and busy["time_min"] == "2026-11-02T23:00:00Z"
    assert busy["time_max"] == "2026-11-03T23:00:00Z" and busy["time_zone"] == "Europe/Paris"
    assert parse({"operation": "free_busy", "time_min": "now", "time_max": "2026-10-12T00:00:00Z"}, utc)["calendars"] == ["primary"]
    assert parse({"operation": "watch", "request_id": "w-1"}, reader) == {"operation": "watch", "request_id": "w-1"}
    for value, code in (({"operation": "create_event"}, "operation_not_allowed"), ({"operation": "purge"}, "invalid_input"),
                        ([{"calendar_id": TEAM}], "invalid_input"), ("[1, 2]", "invalid_input"),
                        ({"limit": 251}, "invalid_input"), ({"limit": 0}, "invalid_input"),
                        ({"page_token": "has space"}, "invalid_input"), ({"order_by": "start"}, "invalid_input"),
                        ({"order_by": "startTime", "single_events": False}, "invalid_input"),
                        ({"single_events": "yes"}, "invalid_input"), ({"calendar_id": "../x"}, "invalid_input"),
                        ({"time_min": "yesterday"}, "invalid_input"), ({"time_min": "2026-02-30"}, "invalid_input"),
                        ({"time_min": "2026-11-02T10:00:00"}, "invalid_input"),
                        ({"time_min": "2026-11-02", "time_max": "2026-11-01"}, "invalid_input"),
                        ({"query": ""}, "invalid_input"), ({"query": "a\nb"}, "invalid_input"),
                        ({"time_zone": "Mars/Olympus"}, "invalid_input"), ({"event_id": EVENT}, "invalid_input"),
                        ({"operation": "get_event"}, "invalid_input"),
                        ({"operation": "get_event", "event_id": "https://calendar.google.com/event?eid=x"}, "invalid_input"),
                        ({"operation": "free_busy", "time_min": "now"}, "invalid_input"),
                        ({"operation": "free_busy", "calendars": [], "time_min": "now", "time_max": "2027-01-01"}, "invalid_input"),
                        ({"operation": "free_busy", "calendars": [TEAM, TEAM], "time_min": "now", "time_max": "2027-01-01"},
                         "invalid_input"),
                        ({"operation": "free_busy", "calendars": ["x@y.test"] * 51, "time_min": "now",
                          "time_max": "2027-01-01"}, "invalid_input"),
                        ({"operation": "list_calendars", "min_access_role": "admin"}, "invalid_input"),
                        ('{"limit": 1, "limit": 2}', "invalid_input"), ("{broken", "invalid_input"),
                        ({"request_id": "bad id"}, "invalid_input")):
        rejected(lambda value=value: parse(value, utc), code)
    rejected(lambda: parse({"time_min": "2026-11-02T10:00:00"}, utc), "invalid_input")
    rejected(lambda: parse({"operation": "watch", "calendar_id": TEAM}, reader), "invalid_input")
    assert parse({"time_min": "2026-11-02T10:00"}, reader)["time_min"] == "2026-11-02T09:00:00Z"
    small = logic.configuration({**BASE, "max_request_bytes": 1024})
    rejected(lambda: parse({"query": "x", "pad": "y" * 2000}, small), "request_too_large")
    rejected(lambda: parse("y" * 2000, small), "request_too_large")


def change_requests():
    """# - FB3 - # - FB4 - event bodies: timed, all-day, guests, reminders, recurrence; gates apply before the wallet."""
    writer = logic.configuration({**WRITER, "default_time_zone": "Europe/Paris"})
    created = parse({"operation": "create_event", "summary": " Review ", "start": "2026-11-02T10:00", "duration_minutes": 90,
                     "description": "Line 1\nLine 2", "location": "Room 4",
                     "attendees": ["a@example.test", {"email": "b@example.test", "display_name": "Bea", "optional": True}],
                     "reminders": {"overrides": [{"method": "popup", "minutes": 10}]},
                     "transparency": "transparent", "visibility": "private", "send_updates": "externalOnly"}, writer)
    assert created["calendar_id"] == "primary" and created["send_updates"] == "externalOnly"
    assert created["body"] == {"summary": "Review", "description": "Line 1\nLine 2", "location": "Room 4",
                               "start": {"dateTime": "2026-11-02T10:00:00", "timeZone": "Europe/Paris"},
                               "end": {"dateTime": "2026-11-02T11:30:00", "timeZone": "Europe/Paris"},
                               "attendees": [{"email": "a@example.test"},
                                             {"email": "b@example.test", "displayName": "Bea", "optional": True}],
                               "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
                               "transparency": "transparent", "visibility": "private"}
    # Elapsed time across the end of daylight saving time: 01:30 + 120 minutes is 02:30 local.
    dst = parse({"operation": "create_event", "summary": "Night", "start": "2026-10-25T01:30:00", "duration_minutes": 120},
                writer)
    assert dst["body"]["end"] == {"dateTime": "2026-10-25T02:30:00", "timeZone": "Europe/Paris"}
    offset = parse({"operation": "create_event", "summary": "Call", "start": "2026-11-02T10:00:00+01:00",
                    "end": "2026-11-02T09:30:00Z", "event_id": "bloksmith0001", "send_updates": "none"},
                   logic.configuration({**WRITER}))
    assert offset["body"] == {"summary": "Call", "start": {"dateTime": "2026-11-02T10:00:00+01:00"},
                              "end": {"dateTime": "2026-11-02T09:30:00Z"}, "id": "bloksmith0001"}
    allday = parse({"operation": "create_event", "summary": "Offsite", "start": "2026-11-05",
                    "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"], "reminders": {"use_default": True}}, writer)
    assert allday["body"]["start"] == {"date": "2026-11-05"} and allday["body"]["end"] == {"date": "2026-11-06"}
    assert allday["body"]["reminders"] == {"useDefault": True} and allday["body"]["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"]
    assert parse({"operation": "create_event", "summary": "x", "start": "2026-11-05", "end": "2026-11-08"},
                 writer)["body"]["end"] == {"date": "2026-11-08"}
    patch = parse({"operation": "update_event", "event_id": EVENT, "location": "", "etag": '"3181161784712000"'}, writer)
    assert patch["body"] == {"location": ""} and patch["etag"] == '"3181161784712000"' and patch["send_updates"] == "all"
    moved = parse({"operation": "update_event", "event_id": EVENT, "start": "2026-11-02T10:00:00Z"}, writer)
    assert moved["body"] == {"start": {"dateTime": "2026-11-02T10:00:00Z", "timeZone": "Europe/Paris"}}
    deleted = parse({"operation": "delete_event", "event_id": EVENT, "send_updates": "none"}, writer)
    assert deleted == {"operation": "delete_event", "request_id": None, "calendar_id": "primary", "event_id": EVENT,
                       "etag": None, "send_updates": "none"}
    chooser = logic.configuration({**WRITER, "allow_request_calendar": True})
    assert parse({"operation": "delete_event", "event_id": EVENT, "calendar_id": TEAM}, chooser)["calendar_id"] == TEAM
    assert parse({"operation": "get_event", "event_id": EVENT, "calendar_id": TEAM}, writer)["calendar_id"] == TEAM
    quiet = logic.configuration({**WRITER, "send_updates": "none"})
    for config, value, code in (
            (quiet, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "send_updates": "all"},
             "invalid_input"),
            (writer, {"operation": "create_event", "start": "2026-11-05"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-02T10:00:00"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-02T10:00:00",
                      "end": "2026-11-02T09:00:00"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-02T10:00:00Z",
                      "end": "2026-11-02T10:00:00Z"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "end": "2026-11-05"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "end": "2026-11-06T10:00:00Z"},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "duration_minutes": 60}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05T10:00:00Z", "end": "2026-11-05T11:00:00Z",
                      "duration_minutes": 60}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05T10:00:00Z", "duration_minutes": 0},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "a\x00b", "start": "2026-11-05"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x" * 1025, "start": "2026-11-05"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "attendees": "a@example.test"},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "attendees": ["not an address"]},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "attendees": ["a@example.test", "A@example.test"]}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "attendees": ["a@example.test\r\nBcc: c@example.test"]}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "attendees": [f"g{index}@example.test" for index in range(101)]}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "reminders": {"use_default": True, "overrides": [{"method": "popup", "minutes": 5}]}}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "reminders": {"overrides": [{"method": "sms", "minutes": 5}]}}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "reminders": {"overrides": [{"method": "popup", "minutes": 40321}]}}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "recurrence": ["FREQ=DAILY"]}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05",
                      "recurrence": ["RRULE:FREQ=DAILY\nATTENDEE:x"]}, "invalid_input"),
            (logic.configuration(WRITER), {"operation": "create_event", "summary": "x", "start": "2026-11-05T10:00:00Z",
                                           "duration_minutes": 30, "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "visibility": "secret"},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "event_id": "UPPERCASE1"},
             "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "event_id": "wxyz0"}, "invalid_input"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "calendar_id": TEAM},
             "calendar_not_allowed"),
            (writer, {"operation": "create_event", "summary": "x", "start": "2026-11-05", "colorId": "3"}, "invalid_input"),
            (writer, {"operation": "update_event", "event_id": EVENT}, "invalid_input"),
            (writer, {"operation": "update_event", "summary": "x"}, "invalid_input"),
            (writer, {"operation": "update_event", "event_id": EVENT, "duration_minutes": 30}, "invalid_input"),
            (writer, {"operation": "update_event", "event_id": EVENT, "summary": "x", "etag": "a b"}, "invalid_input"),
            (writer, {"operation": "update_event", "event_id": EVENT, "summary": ""}, "invalid_input"),
            (writer, {"operation": "update_event", "event_id": EVENT, "start": "2026-11-05", "end": "2026-11-04"},
             "invalid_input"),
            (writer, {"operation": "delete_event", "event_id": EVENT, "calendar_id": TEAM}, "calendar_not_allowed"),
            (writer, {"operation": "delete_event", "event_id": EVENT, "summary": "x"}, "invalid_input")):
        rejected(lambda config=config, value=value: parse(value, config), code)
    # A tampered configuration (validation bypassed) still cannot change or delete anything.
    rejected(lambda: parse({"operation": "create_event", "summary": "x", "start": "2026-11-05"},
                           {**writer, "allow_writes": False}), "writes_disabled")
    rejected(lambda: parse({"operation": "delete_event", "event_id": EVENT}, {**writer, "allow_delete": False}),
             "delete_disabled")


def resource_contract():
    """# - FB2 - Google resources are validated and normalized into stable public shapes."""
    raw = {"kind": "calendar#event", "id": EVENT, "status": "confirmed", "summary": "Review", "htmlLink": "https://x.test/e",
           "created": "2026-10-01T08:00:00.000Z", "updated": "2026-10-02T08:00:00.000Z", "etag": '"31"',
           "start": {"dateTime": "2026-11-02T10:00:00+01:00", "timeZone": "Europe/Paris"},
           "end": {"dateTime": "2026-11-02T11:00:00+01:00", "timeZone": "Europe/Paris"},
           "organizer": {"email": "o@example.test", "self": True},
           "attendees": [{"email": "g@example.test", "responseStatus": "accepted", "optional": True}],
           "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 30}]},
           "recurrence": ["RRULE:FREQ=WEEKLY"], "iCalUID": EVENT + "@google.com", "eventType": "default"}
    event = logic.normalize_event(raw)
    assert event["start"] == {"date": None, "date_time": "2026-11-02T10:00:00+01:00", "time_zone": "Europe/Paris"}
    assert event["all_day"] is False and event["etag"] == '"31"' and event["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert event["organizer"] == {"email": "o@example.test", "display_name": None, "self": True} and event["creator"] is None
    assert event["attendees"] == [{"email": "g@example.test", "display_name": None, "self": False,
                                   "response_status": "accepted", "optional": True, "organizer": False, "resource": False}]
    assert event["reminders"] == {"use_default": False, "overrides": [{"method": "email", "minutes": 30}]}
    assert set(event) == set(logic.cancelled_event({"id": EVENT, "change": "cancelled", "start": None,
                                                    "recurring_event_id": None, "original_start": None, "updated": None}))
    allday = logic.normalize_event({"id": EVENT, "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}})
    assert allday["all_day"] is True and allday["status"] is None
    minimal = logic.normalize_event({"id": EVENT, "status": "cancelled"})
    assert minimal["start"] is None and minimal["attendees"] == []
    for bad in (None, [], {**raw, "id": "has space"}, {**raw, "summary": 5}, {**raw, "attendees": "x"},
                {**raw, "start": "2026"}, {**raw, "reminders": {"overrides": [{"minutes": "5"}]}},
                {**raw, "organizer": "o"}, {**raw, "recurrence": [1]}, {**raw, "attendees": [{"self": "yes"}]}):
        rejected(lambda bad=bad: logic.normalize_event(bad), "protocol")
    assert logic.event_page({"items": [raw], "nextPageToken": "n-1", "timeZone": "UTC"}, 5) == ([raw], "n-1", None, "UTC")
    assert logic.event_page({"nextSyncToken": "s-1"}, 5) == ([], None, "s-1", None)
    for bad in ({"items": {}}, {"items": [raw] * 3}, {"items": [], "nextPageToken": "a b"}, {"nextSyncToken": 5}, []):
        rejected(lambda bad=bad: logic.event_page(bad, 2), "protocol")
    calendar = logic.normalize_calendar({"id": TEAM, "summary": "Team", "timeZone": "UTC", "accessRole": "writer"})
    assert calendar == {"id": TEAM, "summary": "Team", "summary_override": None, "description": None, "time_zone": "UTC",
                        "access_role": "writer", "primary": False, "selected": False, "hidden": False, "color_id": None}
    rejected(lambda: logic.normalize_calendar({"id": TEAM, "primary": "yes"}), "protocol")
    assert logic.calendar_page({"items": [], "nextPageToken": "c-1"}, 3) == ([], "c-1")
    busy = logic.free_busy({"calendars": {"primary": {"busy": [{"start": "2026-11-03T14:00:00Z", "end": "2026-11-03T15:00:00Z"}]},
                                          TEAM: {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}},
                           ["primary", TEAM, "x@example.test"])
    assert busy == [{"id": "primary", "busy": [{"start": "2026-11-03T14:00:00Z", "end": "2026-11-03T15:00:00Z"}], "errors": []},
                    {"id": TEAM, "busy": [], "errors": ["notFound"]}, {"id": "x@example.test", "busy": [], "errors": ["notFound"]}]
    rejected(lambda: logic.free_busy({"calendars": {"primary": {"busy": [{"start": 1}]}}}, ["primary"]), "protocol")
    assert logic.calendar_resource({"id": "o@example.test", "timeZone": "UTC"}) == {"id": "o@example.test", "time_zone": "UTC"}


def provider_contract():
    """# - FB6 - Calendar error envelopes map to stable codes; rate limits retry; changes after 5xx are uncertain."""
    secrets = [ACCESS]
    for status, reason, code in ((400, "invalid", "bad_request"), (400, "timeRangeEmpty", "bad_request"),
                                 (401, "authError", "unauthorized"), (403, "forbiddenForNonOrganizer", "forbidden"),
                                 (403, "requiredAccessLevel", "forbidden"), (403, "userRateLimitExceeded", "rate_limited"),
                                 (403, "rateLimitExceeded", "rate_limited"), (403, "quotaExceeded", "quota_exceeded"),
                                 (403, "dailyLimitExceeded", "quota_exceeded"), (404, "notFound", "not_found"),
                                 (409, "duplicate", "duplicate"), (409, "conflict", "conflict"),
                                 (410, "fullSyncRequired", "sync_expired"), (410, "deleted", "gone"),
                                 (412, "conditionNotMet", "conflict"), (418, "teapot", "provider_error"),
                                 (429, "rateLimitExceeded", "rate_limited"), (500, "backendError", "provider_unavailable"),
                                 (503, "backendError", "provider_unavailable")):
        body = {"error": {"code": status, "message": "Detail with " + ACCESS + "\n" + "x" * 400, "status": "SOME_STATUS",
                          "errors": [{"domain": "global", "reason": reason, "message": "m"}]}}
        for mutation in (False, True):
            error = rejected(lambda status=status, body=body, mutation=mutation: logic.provider_failure(
                status, body, secrets, mutation=mutation, retry_after=7), code)
            envelope = error.envelope(operation="create_event", request_id=None)
            assert envelope["http_status"] == status and envelope["provider_status"] == "SOME_STATUS"
            assert envelope["provider_reason"] == reason
            assert len(envelope["provider_message"]) <= 200 and "\n" not in envelope["provider_message"]
            assert ACCESS not in json.dumps(envelope) and "request_id" not in envelope
            assert error.uncertain is (mutation and status >= 500)
            assert envelope.get("retry_after") == (7 if code == "rate_limited" else None)
    rejected(lambda: logic.provider_failure(502, None, secrets, mutation=False), "provider_unavailable")
    assert rejected(lambda: logic.provider_failure(404, {"error": "x"}, secrets, mutation=False), "not_found").provider_reason is None


def item(ident, created, updated=None, **extra):
    return {"id": ident, "status": "confirmed", "created": created, "updated": updated or created,
            "start": {"dateTime": "2026-11-02T10:00:00Z"}, "end": {"dateTime": "2026-11-02T11:00:00Z"}, **extra}


def watch_contract():
    """# - FB5 - pass classification, bounded queues, strict persisted state, the 60 s minimum and the node lock."""
    entry = {**logic.new_entry(), "pass_started": "2026-10-11T08:00:00Z"}
    old, fresh = item("oldevent001", "2026-01-01T00:00:00.000Z"), item("newevent001", "2026-10-11T07:59:00.000Z")
    ended = item("pastevent01", "2026-01-01T00:00:00.000Z", end={"dateTime": "2026-10-01T11:00:00Z"})
    assert logic.classify(old, entry, emit_existing=False) is None, "The first pass reports nothing"
    assert logic.classify(old, entry, emit_existing=True) == "existing"
    assert logic.classify(ended, entry, emit_existing=True) is None, "Only upcoming events are existing"
    assert logic.classify({**old, "status": "cancelled"}, entry, emit_existing=True) is None
    remembered = logic.remember(entry, [old, fresh])
    assert remembered["recent"] == ["newevent001"], "Events created around a pass are remembered"
    passed = {**remembered, "phase": "incremental", "sync_token": "s-1", "since": "2026-10-11T08:00:00Z",
              "pass_started": "2026-10-11T08:05:00Z"}
    later = item("later000001", "2026-10-11T08:03:00.000Z")
    assert logic.classify(later, passed, emit_existing=False) == "created"
    assert logic.classify(fresh, passed, emit_existing=False) == "updated", "Already seen when the previous pass ran"
    assert logic.classify({**old, "updated": "2026-10-11T08:04:00.000Z"}, passed, emit_existing=False) == "updated"
    assert logic.classify({"id": "gone0000001", "status": "cancelled"}, passed, emit_existing=False) == "cancelled"
    resync = {**passed, "phase": "resync"}
    assert logic.classify(old, resync, emit_existing=False) is None, "A resync ignores events unchanged since the last pass"
    assert logic.classify({**old, "updated": "2026-10-11T08:01:00.000Z"}, resync, emit_existing=False) == "updated"
    assert logic.classify({**old, "status": "cancelled", "updated": "2026-10-11T08:01:00.000Z"}, resync,
                          emit_existing=False) == "cancelled"
    rejected(lambda: logic.classify({"id": "a b"}, passed, emit_existing=False), "protocol")
    instance = {"id": "series01_20261102T100000Z", "status": "cancelled", "recurringEventId": "series01",
                "originalStartTime": {"dateTime": "2026-11-02T10:00:00Z"}, "updated": "2026-10-11T08:04:00.000Z"}
    pending = logic.change(instance, "cancelled")
    assert pending == {"id": "series01_20261102T100000Z", "change": "cancelled", "start": None,
                       "recurring_event_id": "series01", "original_start": "2026-11-02T10:00:00Z",
                       "updated": "2026-10-11T08:04:00.000Z"}
    cancelled = logic.cancelled_event(pending)
    assert cancelled["status"] == "cancelled" and cancelled["original_start"]["date_time"] == "2026-11-02T10:00:00Z"
    created = logic.change(later, "created")
    queue = logic.enqueue([created], [logic.change(later, "updated"), logic.change(old, "updated")])
    assert [(entry["id"], entry["change"]) for entry in queue] == [("later000001", "created"), ("oldevent001", "updated")]
    queue = logic.enqueue(queue, [logic.change({**later, "status": "cancelled"}, "cancelled")])
    assert queue[0]["change"] == "cancelled" and len(queue) == 2
    many = [logic.change(item(f"existing{index:04d}", "2026-01-01T00:00:00Z",
                              start={"dateTime": f"2026-11-{index % 28 + 1:02d}T{index % 24:02d}:00:00Z"}), "existing")
            for index in range(150)]
    kept = logic.trim_existing(many + [created])
    assert len(kept) == logic.EXISTING_LIMIT + 1 and kept[-1]["change"] == "created"
    assert max(entry["start"] for entry in kept if entry["change"] == "existing") <= \
        min(entry["start"] for entry in many if entry not in kept)
    document = logic.store_entry(logic.empty_document(), logic.watch_digest("o@example.test"),
                                 {**passed, "pending": queue}, 1000)
    assert logic.watch_document(json.loads(json.dumps(document))) == document
    assert "o@example.test" not in json.dumps(document) and "Review" not in json.dumps(document)
    for index in range(10):
        document = logic.store_entry(document, logic.watch_digest(f"c{index}@example.test"), logic.new_entry(), 2000 + index)
    assert len(document["watches"]) == logic.WATCH_LIMIT
    digest = logic.watch_digest("o@example.test")
    assert digest not in document["watches"], "The least recently polled calendars are forgotten first"
    for bad in ({}, {**document, "schema_version": 2}, {**document, "last_poll": "1"}, {**document, "extra": 1},
                {**document, "watches": {"x": logic.new_entry()}},
                {**document, "watches": {digest: {**logic.new_entry(), "phase": "fast"}}},
                {**document, "watches": {digest: {**logic.new_entry(), "phase": "incremental"}}},
                {**document, "watches": {digest: {**logic.new_entry(), "since": "yesterday"}}},
                {**document, "watches": {digest: {**logic.new_entry(), "recent": ["a b"]}}},
                {**document, "watches": {digest: {**logic.new_entry(), "pending": [{"id": "x"}]}}},
                {**document, "watches": {digest: {**logic.new_entry(), "pending": [{**created, "change": "moved"}]}}},
                {**document, "watches": {digest: {**logic.new_entry(), "token": "x"}}}):
        rejected(lambda bad=bad: logic.watch_document(bad), "state_invalid")
    assert logic.poll_wait({"last_poll": 1000}, 1030) == 30 and logic.poll_wait({"last_poll": 1000}, 1060) == 0
    assert logic.poll_wait({"last_poll": 5000}, 1000) == 0, "A clock moved backwards never blocks polling for long"
    assert logic.server_time("Sun, 11 Oct 2026 08:30:15 GMT", NOW.replace(year=2000)) == "2026-10-11T08:30:15Z"
    assert logic.server_time("garbage", NOW) == logic.server_time(None, NOW) == "2026-10-11T08:30:15Z"
    big = {"change": "updated", "calendar_id": "primary",
           "event": {**logic.normalize_event(item("big0000001", "2026-10-01T00:00:00Z")), "description": "é" * 200000}}
    small = logic.reduced(big)
    assert logic.weight(big) > logic.BATCH_LIMIT and logic.weight(small) < 2000 and small["oversized"] is True
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        store = storage.WatchStore(base)
        assert store.load() == logic.empty_document()
        assert store.acquire()
        second = storage.WatchStore(base)
        assert second.acquire() is False, "A second poller of the same node is refused"
        store.save(document)
        assert storage.WatchStore(base).load() == document and not list(base.glob("*.tmp"))
        assert oct(os.stat(base / storage.STATE_NAME).st_mode & 0o777) == "0o600"
        store.close()
        assert second.acquire()
        second.close()
        (base / storage.STATE_NAME).write_text("{broken")
        rejected(lambda: storage.WatchStore(base).load(), "state_invalid")
        (base / storage.STATE_NAME).unlink()
        (base / "elsewhere.json").write_text("{}")
        (base / storage.STATE_NAME).symlink_to(base / "elsewhere.json")
        rejected(lambda: storage.WatchStore(base).load(), "state_invalid")
        rejected(lambda: storage.directory(None), "storage")
        rejected(lambda: storage.directory(lambda: (_ for _ in ()).throw(RuntimeError("block_storage.missing_scope"))),
                 "storage")
        rejected(lambda: storage.directory(lambda: str(base / "absent")), "storage")
        assert storage.directory(lambda: str(base)) == base


def block_contract():
    """# - FB1 - # - FB6 - # - FB7 - # - FB8 - hooks fail before side effects; events run once; surfaces are translated."""
    block = GoogleCalendarBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    assert [port.name for port in ports_in] == ["request"]
    assert [port.name for port in ports_out] == ["events", "changed", "result", "error"]
    assert all(port["execution_requirement"] == "not_required_for_execution" for port in block.model["ports"]["inputs"])
    assert block.model["runtime"]["centralized_dependency_mode"] == "any_event"
    assert block.model["version"] == "0.1.0" and block.model["description"]
    calls = []

    def resolver(ref):
        calls.append(ref)
        return "not json {" + ACCESS

    def run(config, inputs=None, events=(), services=None, mode="zeromq_active"):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs or {}, input_ports=ports_in,
                                      output_ports=ports_out, input_events=tuple(events), runtime_mode=mode,
                                      services={"resolve_secret": resolver, **(services or {})})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, [(output.port_name, json.loads(output.value)) for output in result.outputs]

    assert run(BASE)[0].status == "skipped"
    watcher_config = {**BASE, "allowed_operations": ["list_events", "watch"]}
    with tempfile.TemporaryDirectory() as folder:
        node_storage = Path(folder)
        recent = {"get_block_storage_dir": lambda: str(node_storage)}
        storage.WatchStore(node_storage).save({**logic.empty_document(), "last_poll": int(datetime.now().timestamp())})
        for config, inputs, services, code in (
                ({**BASE, "enabled": False}, {"request": "go"}, None, "disabled"),
                (BASE, {"request": {"operation": "create_event", "summary": "x", "start": "2026-11-05"}}, None,
                 "operation_not_allowed"),
                (BASE, {"request": '{"limit":'}, None, "invalid_input"),
                ({**BASE, "allow_writes": True, "allowed_operations": ["create_event"]},
                 {"request": {"operation": "create_event", "summary": "x", "start": "2026-11-02T10:00:00"}}, None,
                 "invalid_input"),
                (watcher_config, {"request": {"operation": "watch"}}, None, "storage"),
                (watcher_config, {"request": {"operation": "watch"}}, recent, "too_soon"),
                ({**BASE, "secret_ref": ACCESS}, {"request": "go"}, None, "config")):
            result, outputs = run(config, inputs, services=services)
            assert [name for name, _ in outputs] == ["error"] and outputs[0][1]["code"] == code, outputs
            assert result.metadata["google_calendar"] == {"status": "error", "operations": 1, "error_codes": [code]}
            assert ACCESS not in json.dumps(outputs) and ACCESS not in " ".join(result.logs)
        assert 50 <= run(watcher_config, {"request": {"operation": "watch"}}, services=recent)[1][0][1]["retry_after"] <= 60
        holder = storage.WatchStore(node_storage)
        assert holder.acquire()
        assert run(watcher_config, {"request": {"operation": "watch"}}, services=recent)[1][0][1]["code"] == "busy"
        holder.close()
    assert not calls, "The wallet must not be read before settings, request, storage and interval validation"
    result, outputs = run(BASE, {"request": {"request_id": "req-1", "limit": 3, "calendar_id": TEAM}})
    error = outputs[0][1]
    assert error["code"] == "secret" and error["request_id"] == "req-1" and error["operation"] == "list_events"
    assert error["calendar_id"] == TEAM and error["input"] == "request" and calls == [REF]
    assert ACCESS not in json.dumps(outputs) and result.logs == ["[google-calendar-error] list_events: secret"]
    # One Shot hands every connected value to each any_event activation: each event runs once per run.
    memory = {}
    services = {"get_node_runtime_value": lambda key, default=None: memory.get(key, default),
                "set_node_runtime_value": lambda key, value: memory.__setitem__(key, value)}
    event = SimpleNamespace(edge_id="e1", input_port_id=1, input_port_name="request", source_node_id="src",
                            source_port_id=1, value='{"request_id": "once"}', content_type="application/json", sequence=0)
    calls.clear()
    first = run(BASE, {"request": event.value}, [event], services, "centralized")[1]
    again = run(BASE, {"request": event.value}, [event], services, "centralized")
    assert first[0][1]["request_id"] == "once" and again[0].status == "skipped" and calls == [REF]
    flood = [SimpleNamespace(**{**vars(event), "edge_id": f"x{index}"}) for index in range(9)]
    assert run(BASE, {}, flood)[1][0][1]["code"] == "invalid_input"

    def prepared(config, mode="zeromq_active"):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context)

    assert prepared(WRITER).listen_on_run is False
    assert prepared({**BASE, "watch_enabled": True}).listen_on_run is True
    assert prepared({**BASE, "watch_enabled": True}, "centralized").listen_on_run is False, "One Shot never listens"
    assert prepared({**BASE, "enabled": False, "watch_enabled": True}).listen_on_run is False
    rejected(lambda: prepared({**BASE, "secret_ref": ACCESS}), "config")
    node = block.build_node_payload(node_id="calendar", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings", values={"node_patch": {"config": WRITER}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True
    assert operation["config"]["allowed_operations"] == WRITER["allowed_operations"]
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BASE, "operation": "delete_event"}}})
    assert "allow_delete" in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    assert 'data-block-config-field="secret_ref"' in html and 'data-google-calendar-show="write" hidden' in html
    assert 'data-google-calendar-show="watch" hidden' in html and 'data-block-value-type="json"' in html and "{{" not in html
    assert 'data-i18n="block.google_calendar.label_watch_interval_sec"' in html
    watching = {**node, "config": {**node["config"], **BASE, "watch_enabled": True}}
    panel = block.render_inspector_panel(node=watching)["html"]
    assert 'data-google-calendar-show="watch" hidden' not in panel and 'data-google-calendar-show="write" hidden' in panel
    deleting = {**node, "config": {**node["config"], **BASE, "allow_delete": True}}
    assert 'data-google-calendar-show="write" hidden' not in block.render_modal(node=deleting)["html"]
    card = block.render_node_card(node=node)["html"]
    assert card.count("Remote access off") == 1 and 'data-i18n="block.google_calendar.card_disabled" data-i18n-params=""' in card
    assert "Read only · list_events" in block.render_node_card(node={**node, "config": {**node["config"], **BASE}})["html"]
    assert "Watching · list_events" in block.render_node_card(node=watching)["html"]
    card = block.render_node_card(node=deleting)["html"]
    assert ("Read and write · list_events" in card and 'data-i18n="block.google_calendar.card_write" '
            'data-i18n-params="{&quot;operation&quot;: &quot;list_events&quot;}"' in card)
    assert REF not in card and "primary" not in card.replace("list_events", "")
    assert block.translate("block.google_calendar.card_watch", {"operation": "watch"}, lang="fr") == "Surveillance · watch"
    english = json.loads((block.directory / "locales" / "en.json").read_text(encoding="utf-8"))
    french = json.loads((block.directory / "locales" / "fr.json").read_text(encoding="utf-8"))
    assert set(english) == set(french) and all(key.startswith("block.google_calendar.") for key in english)
    fields = json.loads((block.directory / "fields.json").read_text(encoding="utf-8"))
    assert {spec["key"] for spec in fields} == set(logic.DEFAULTS)
    assert all(english["block.google_calendar.label_" + spec["key"]] == spec["label"] for spec in fields)
    assert english["block.google_calendar.model.description"] == block.model["description"]


def main():
    settings_contract()
    credential_contract()
    read_requests()
    change_requests()
    resource_contract()
    provider_contract()
    watch_contract()
    block_contract()
    print("[ok] Google Calendar settings, credentials, requests, times, resources, errors, watch state and hooks")


if __name__ == "__main__":
    main()
