"""The Google Calendar request operations, built from Session calls and pure logic.

Every operation validates what it can before its first request and sends at
most one change. Listings follow page tokens bounded by page_size, max_pages,
list_limit and the output size limit; a page is never cut, so next_page_token
always resumes exactly where the result stopped. The watch poll lives in
watcher.py because it owns persisted state.
"""

import json
from urllib.parse import quote

from . import logic


def identity(request):
    return {"request_id": request["request_id"], "operation": request["operation"]}


def calendar_path(calendar):
    return "/calendars/" + quote(calendar, safe="")


def event_path(calendar, event):
    return calendar_path(calendar) + "/events/" + quote(event, safe="")


def paged(session, request, config, fetch, page_size_cap):
    """Collect normalized items page by page; return (items, stop reason, next token, pages, last body)."""
    limit, token = request["limit"], request["page_token"]
    found, pages, total = [], 0, 2
    stop, next_token, last = "end", None, {}
    while True:
        if pages >= config["max_pages"]:
            stop, next_token = "max_pages", token
            break
        size = min(config["page_size"], page_size_cap, limit - len(found))
        page, following, last = fetch(size, token)
        pages += 1
        weight = sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1 for item in page)
        if total + weight > logic.OUTPUT_LIMIT:
            if not found:
                logic.fail("protocol", "One Calendar page exceeds the 16 MiB output limit; lower page_size.")
            # The page that does not fit is dropped whole; its own token resumes it.
            stop, next_token = "output_limit", token
            break
        found.extend(page)
        total += weight
        token = following
        if not token:
            stop, next_token = "end", None
            break
        if len(found) >= limit:
            stop, next_token = "limit", token
            break
    return found, stop, next_token, pages, last


def list_calendars(session, request, config):
    def fetch(size, token):
        params = {"maxResults": size, "showHidden": "true" if request["show_hidden"] else "false"}
        if request["min_access_role"]:
            params["minAccessRole"] = request["min_access_role"]
        if token:
            params["pageToken"] = token
        raw, following = logic.calendar_page(session.call("GET", "/users/me/calendarList", params, mutation=False), size)
        return [logic.normalize_calendar(item) for item in raw], following, {}

    found, stop, token, pages, _last = paged(session, request, config, fetch, logic.CALENDAR_LIST_PAGE)
    result = {**identity(request), "count": len(found), "truncated": token is not None, "stop_reason": stop,
              "next_page_token": token, "pages": pages, "calendars": found, "requests": session.requests}
    return {"result": result}


def list_events(session, request, config):
    calendar = request["calendar_id"]

    def fetch(size, token):
        params = {"maxResults": size, "singleEvents": "true" if request["single_events"] else "false",
                  "showDeleted": "true" if request["show_deleted"] else "false"}
        for key, name in (("time_min", "timeMin"), ("time_max", "timeMax"), ("query", "q"), ("order_by", "orderBy"),
                          ("time_zone", "timeZone")):
            if request.get(key):
                params[name] = request[key]
        if token:
            params["pageToken"] = token
        body = session.call("GET", calendar_path(calendar) + "/events", params, mutation=False)
        raw, following, _sync, zone = logic.event_page(body, size)
        return [logic.normalize_event(item) for item in raw], following, {"time_zone": zone}

    found, stop, token, pages, last = paged(session, request, config, fetch, 2500)
    result = {**identity(request), "calendar_id": calendar, "count": len(found), "truncated": token is not None,
              "stop_reason": stop, "next_page_token": token, "pages": pages, "time_min": request.get("time_min"),
              "time_max": request.get("time_max"), "query": request["query"], "single_events": request["single_events"],
              "time_zone": last.get("time_zone"), "requests": session.requests}
    return {"events": found, "result": result}


def get_event(session, request, config):
    params = {"timeZone": request["time_zone"]} if request["time_zone"] else None
    event = logic.normalize_event(session.call("GET", event_path(request["calendar_id"], request["event_id"]), params,
                                               mutation=False))
    result = {**identity(request), "calendar_id": request["calendar_id"], "event_id": event["id"],
              "status": event["status"], "requests": session.requests}
    return {"events": [event], "result": result}


def free_busy(session, request, config):
    payload = {"timeMin": request["time_min"], "timeMax": request["time_max"],
               "items": [{"id": ident} for ident in request["calendars"]]}
    if request["time_zone"]:
        payload["timeZone"] = request["time_zone"]
    calendars = logic.free_busy(session.call("POST", "/freeBusy", None, payload, mutation=False), request["calendars"])
    result = {**identity(request), "time_min": request["time_min"], "time_max": request["time_max"],
              "time_zone": request["time_zone"], "calendars": calendars,
              "busy_count": sum(len(item["busy"]) for item in calendars), "requests": session.requests}
    return {"result": result}


def create_event(session, request, config):
    """One events.insert; a client event_id makes a re-sent request fail as duplicate instead of creating twice."""
    body = session.call("POST", calendar_path(request["calendar_id"]) + "/events",
                        {"sendUpdates": request["send_updates"]}, request["body"], mutation=True)
    try:
        event = logic.normalize_event(body)
    except logic.CalendarError:
        logic.fail("protocol", "Google answered the creation with an invalid event; check the calendar before retrying.",
                   uncertain=True)
    result = {**identity(request), "calendar_id": request["calendar_id"], "event_id": event["id"], "created": True,
              "send_updates": request["send_updates"], "event": event, "requests": session.requests}
    return {"result": result}


def update_event(session, request, config):
    """One events.patch with only the given fields; an etag makes it conditional (If-Match)."""
    body = session.call("PATCH", event_path(request["calendar_id"], request["event_id"]),
                        {"sendUpdates": request["send_updates"]}, request["body"], mutation=True, if_match=request["etag"])
    try:
        event = logic.normalize_event(body)
    except logic.CalendarError:
        logic.fail("protocol", "Google answered the update with an invalid event; check the event before retrying.",
                   uncertain=True)
    result = {**identity(request), "calendar_id": request["calendar_id"], "event_id": event["id"], "updated": True,
              "fields": sorted(request["body"]), "send_updates": request["send_updates"], "event": event,
              "requests": session.requests}
    return {"result": result}


def delete_event(session, request, config):
    """One events.delete; an event that is already deleted (HTTP 410) counts as deleted."""
    already = False
    try:
        session.call("DELETE", event_path(request["calendar_id"], request["event_id"]),
                     {"sendUpdates": request["send_updates"]}, mutation=True, if_match=request["etag"], empty_ok=True)
    except logic.CalendarError as exc:
        if exc.code != "gone":
            raise
        already = True
    result = {**identity(request), "calendar_id": request["calendar_id"], "event_id": request["event_id"], "deleted": True,
              "already_deleted": already, "send_updates": request["send_updates"], "requests": session.requests}
    return {"result": result}


RUNNERS = {"list_calendars": list_calendars, "list_events": list_events, "get_event": get_event, "free_busy": free_busy,
           "create_event": create_event, "update_event": update_event, "delete_event": delete_event}


def run(session, request, config):
    return RUNNERS[request["operation"]](session, request, config)
