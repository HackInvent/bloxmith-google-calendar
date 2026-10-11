# Google Calendar

List, create, update and watch Google Calendar events. The block lists the
calendars of an account and the events of a calendar (by time window, text and
with recurring events expanded), reads one event, queries free/busy time,
creates, patches and deletes events, and watches a calendar for created,
updated and cancelled events. The OAuth grant never leaves the BloxSmith wallet,
remote access stays off until you enable it, and nothing is created, changed or
deleted until you also enable changes or deletion.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Google Calendar — list, create, update and watch calendar events](media/thumbnail.webp)](media/cover.png)

## When to use it

- Feed today's or this week's meetings to a summary, a report or a notification.
- Create an event from a form, a ticket or an approval, with guests and reminders.
- Move, rename or cancel an event that a workflow created earlier.
- Check when people or rooms are busy before proposing a slot.
- Start a workflow when an event is created, changed or cancelled in a calendar.

The Calendar Booking block covers shared availability and
approval-gated booking of meetings; this block is general event management.

Each node has one **calendar** (`calendar_id`, default `primary`) for changes and
for watching, set in its properties. Reading (listings, one event, free/busy) can
address any calendar the Google account can see.

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `request` | A JSON object with the operation parameters, or any other text as a plain trigger |
| Output | `events` | `list_events` and `get_event`: a JSON array of event objects |
| Output | `changed` | `watch`: a JSON array of change objects (created, updated, cancelled or existing events) |
| Output | `result` | Operation receipt: counts, continuation token, calendars, free/busy, the created or updated event |
| Output | `error` | Machine `code`, English `message`, `uncertain` flag and provider details |

Each fresh `request` value runs exactly one operation. In One Shot, a value that
was already handled is not sent again when the node is activated again. Business
failures are published on `error` and the run continues; they never stop an
Active Runtime worker. `changed` is published only when a poll found changes.

### `request` input

| Field | Operations | Meaning |
| --- | --- | --- |
| `operation` | all | One of the operations below; defaults to the `operation` setting and must be allowed |
| `request_id` | all | Optional correlation ID (1–128 of `A-Za-z0-9._:-`), echoed in `result` and `error` |
| `calendar_id` | all but `list_calendars`, `free_busy`, `watch` | Calendar to use (default: the `calendar_id` setting). For a change, only with `allow_request_calendar` |
| `time_min`, `time_max` | `list_events`, `free_busy` | Window bounds: `now`, a date (`2026-11-02`, midnight in the time zone) or an RFC 3339 date-time. Required for `free_busy` |
| `query` | `list_events` | Free text Google matches in titles, descriptions, locations and guests |
| `single_events` | `list_events` | `true` returns each occurrence of a recurring event; `false` one item per series (default: setting) |
| `order_by` | `list_events` | `startTime` (needs `single_events`) or `updated`; Google's order otherwise |
| `show_deleted` | `list_events` | Include cancelled events |
| `limit`, `page_token` | `list_events`, `list_calendars` | Items to return (1 to `list_limit`) and the `next_page_token` of a previous result, with the same query |
| `min_access_role`, `show_hidden` | `list_calendars` | `freeBusyReader`, `reader`, `writer` or `owner`; include hidden calendars |
| `calendars` | `free_busy` | Up to 50 calendar IDs (default: the `calendar_id` setting) |
| `time_zone` | all but `list_calendars`, `watch` | IANA time zone (default: `default_time_zone`) for times without an offset, date-only bounds and response times |
| `event_id` | `get_event`, `update_event`, `delete_event` | Event ID from a listing (an occurrence ID such as `abc_20261102T090000Z` works too) |
| `event_id` | `create_event` | Optional new ID: 5–1024 characters among `a`–`v` and `0`–`9`. Sending the same creation twice then fails as `duplicate` instead of creating two events |
| `summary` | `create_event` (required), `update_event` | Title, 1–1024 characters |
| `description`, `location` | `create_event`, `update_event` | Text (up to 8192 and 1024 characters); an empty text clears the field in an update |
| `start`, `end` | `create_event`, `update_event` | A date (`2026-11-05`) for an all-day event — `end` is exclusive and defaults to the next day — or a date-time with an offset (`2026-11-02T10:00:00+01:00`) or without one plus `time_zone` |
| `duration_minutes` | `create_event`, `update_event` | Instead of `end` for a timed event (elapsed minutes, so daylight-saving changes are respected); needs `start` |
| `attendees` | `create_event`, `update_event` | Up to 100 guests: an email, or `{"email", "display_name", "optional"}`. An update replaces the whole list |
| `reminders` | `create_event`, `update_event` | `{"use_default": true}` or `{"overrides": [{"method": "popup" or "email", "minutes": 0–40320}]}` (up to 5) |
| `recurrence` | `create_event`, `update_event` | Up to 10 RFC 5545 lines such as `RRULE:FREQ=WEEKLY;COUNT=10`; a recurring timed event needs `time_zone` |
| `transparency`, `visibility` | `create_event`, `update_event` | `opaque`/`transparent` (busy or free) and `default`/`public`/`private`/`confidential` |
| `send_updates` | `create_event`, `update_event`, `delete_event` | `none`, `externalOnly` or `all`; a request can only lower the `send_updates` setting |
| `etag` | `update_event`, `delete_event` | `etag` of a previous event result: the change applies only if the event did not change since (else `conflict`) |

The operations are `list_calendars`, `list_events`, `get_event`, `free_busy`,
`watch`, `create_event`, `update_event` (a patch: only the given fields change)
and `delete_event`. Unknown fields are refused. Text that does not start with `{`
(for example `go` from a trigger) runs the default operation without parameters.

### Event objects (`events` output and `event` fields)

```json
{"id": "abc123", "status": "confirmed", "summary": "Quarterly review", "description": "Budget",
 "location": "Room 4", "start": {"date": null, "date_time": "2026-11-03T14:00:00Z", "time_zone": null},
 "end": {"date": null, "date_time": "2026-11-03T15:30:00Z", "time_zone": null}, "all_day": false,
 "created": "2026-10-01T08:00:00.000Z", "updated": "2026-10-02T08:00:00.000Z",
 "html_link": "https://www.google.com/calendar/event?eid=...", "hangout_link": null,
 "recurrence": null, "recurring_event_id": null, "original_start": null,
 "organizer": {"email": "me@example.com", "display_name": null, "self": true}, "creator": {...},
 "attendees": [{"email": "guest@example.com", "display_name": null, "self": false,
                "response_status": "accepted", "optional": false, "organizer": false, "resource": false}],
 "reminders": {"use_default": true, "overrides": []}, "transparency": null, "visibility": null,
 "event_type": "default", "ical_uid": "abc123@google.com", "etag": "\"3181161784712000\""}
```

All-day events have `start.date` and `end.date` (end exclusive) and `all_day: true`.
An occurrence of a recurring event has `recurring_event_id` and `original_start`.

### `changed` output

```json
[{"change": "created", "calendar_id": "primary", "event": {"id": "abc123", "summary": "Kickoff", ...}},
 {"change": "cancelled", "calendar_id": "primary", "event": {"id": "def456", "status": "cancelled", "summary": null, ...}}]
```

`change` is `created`, `updated`, `cancelled` or `existing` (`emit_existing`, first
poll only). Created and updated events carry their current content, read when the
change is published. A cancelled event carries only its ID, `updated`, and for a
cancelled occurrence `recurring_event_id` and `original_start`: Google keeps
nothing else of a deleted event. An event too large for one Run message is
published with its identifiers only and `"oversized": true`.

### `result` output

- `list_calendars`: `count`, `truncated`, `stop_reason`, `next_page_token`, `pages`
  and `calendars` (objects with `id`, `summary`, `summary_override`, `description`,
  `time_zone`, `access_role`, `primary`, `selected`, `hidden`, `color_id`).
- `list_events`: `calendar_id`, `count`, `truncated`, `stop_reason` (`end`, `limit`,
  `max_pages` or `output_limit`), `next_page_token`, `pages`, `time_min`, `time_max`,
  `query`, `single_events` and the calendar's `time_zone`. Pass `next_page_token` as
  `page_token` with the same query to continue: pages are never cut.
- `get_event`: `calendar_id`, `event_id`, `status`.
- `free_busy`: `time_min`, `time_max`, `time_zone`, `busy_count` and `calendars`:
  one `{"id", "busy": [{"start", "end"}], "errors"}` per requested calendar, in
  request order (`errors` holds Google's reasons, such as `notFound`).
- `create_event`, `update_event`: `calendar_id`, `event_id`, `created` or `updated`
  (with the patched `fields`), `send_updates` and the resulting `event`.
- `delete_event`: `calendar_id`, `event_id`, `deleted`, `already_deleted` (true when
  Google answered that the event was already deleted) and `send_updates`.
- `watch`: `calendar_id`, `phase` (`baseline`, `incremental` or `resync`),
  `baseline`, `baseline_complete`, `pages`, `count`, `pending`, `dropped`,
  `skipped`, `resyncs`, `incomplete` and `deferred_error`.

All receipts carry `request_id`, `operation` and `requests` (HTTP requests used,
token exchange included). They never contain the token.

### `error` output

`{"code", "message", "uncertain", "operation", "request_id", "calendar_id",
"event_id", "input"}` plus, when Google provides them, `http_status`, `retry_after`,
`provider_status`, `provider_reason` (for example `requiredAccessLevel`),
`provider_message` (bounded, credentials removed) and `attempts`. `uncertain: true`
means a change may have been applied: check the calendar before sending it again,
or give creations an `event_id`.

| Code | Meaning |
| --- | --- |
| `config`, `disabled`, `secret`, `invalid_input`, `request_too_large` | Refused locally; nothing sent |
| `operation_not_allowed`, `writes_disabled`, `delete_disabled`, `calendar_not_allowed` | The operation or calendar is not enabled in the properties |
| `storage`, `state_invalid`, `busy`, `too_soon` | `watch` only: no node storage, unreadable stored position, another poll running, or less than 60 s since the previous poll (`retry_after` gives the wait) |
| `auth_failed` | The refresh-token grant was refused by Google's token endpoint |
| `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `gone` (410), `duplicate` (409 for an existing `event_id`), `conflict` (409/412, for example a stale `etag`), `rate_limited` (429 or 403 rate limit), `quota_exceeded` (403 usage limit), `provider_error` (other 4xx) | Google refused the request |
| `provider_unavailable` (5xx), `network`, `timeout`, `cancelled`, `protocol` | Transport outcome; `uncertain` tells whether a change may have been applied |
| `tls` | Certificate verification failed; nothing sent |

The Run listener also publishes `overflow` on `error` when more than 1000
changes were waiting and the oldest extra ones were dropped.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, nothing reaches Google and the wallet is not read |
| `secret_ref` | empty | Wallet reference such as `secret://workspace/google_calendar` (see Authentication) |
| `calendar_id` | `primary` | Calendar read by default, changed and watched. `primary` is the account's main calendar |
| `operation` | `list_events` | Operation used when a request names none; always allowed |
| `allowed_operations` | `["list_calendars", "list_events", "get_event", "free_busy"]` | Other operations a request may select |
| `default_time_zone` | empty | IANA time zone for times without an offset and date-only bounds |
| `single_events` | `true` | `list_events` expands recurring events into occurrences |
| `list_limit` | 250 | Maximum items returned by one listing, 1–10000 |
| `allow_writes` | `false` | Required when `create_event` or `update_event` is selectable |
| `allow_delete` | `false` | Required when `delete_event` is selectable |
| `send_updates` | `none` | Guest notification emails for changes: `none`, `externalOnly` or `all` |
| `allow_request_calendar` | `false` | Lets a change request choose another `calendar_id` |
| `watch_enabled` | `false` | Runs the watch listener in Active Runtime |
| `watch_interval_sec` | 300 | Time between two polls, 60–86400 seconds |
| `watch_max_changes` | 50 | Changes published by one poll, 1–100 |
| `emit_existing` | `false` | The first poll also reports up to 100 upcoming events, as `existing` |
| `page_size` | 250 | Items per Google page, 1–2500 (calendar lists: at most 250) |
| `max_pages` | 10 | Google pages per listing or per poll, 1–100 |
| `timeout_sec` | 60 | Deadline for one request or one poll, token exchange and retries included, 5–900 |
| `max_retries` | 2 | Retries, 0–5 (see Limits) |
| `max_retry_wait_sec` | 20 | Longest wait before a retry, 1–120 |
| `max_request_bytes` | 65536 | Bound for the incoming request and each JSON body sent, 1 KiB–1 MiB |
| `max_response_bytes` | 4194304 | Bound for each JSON response from Google, 64 KiB–32 MiB |
| `api_base_url` | `https://www.googleapis.com` | Calendar API origin; plain HTTP only for a loopback test host |
| `oauth_base_url` | `https://oauth2.googleapis.com` | Token origin for refresh-token grants; plain HTTP only for a loopback test host |
| `ca_file` | empty | Optional absolute PEM file for a privately signed HTTPS origin |

The settings are validated on Apply and again before a Run starts. `create_event`
or `update_event` in `operation` or `allowed_operations` is refused unless
`allow_writes` is on, and `delete_event` unless `allow_delete` is on. Restart a
loaded Run to use changed settings.

## Watching a calendar

Watching uses Google's incremental synchronization: no webhook and no listening
port. Each poll first reads the calendar resource, then lists the calendar with
`showDeleted=true` (recurring series not expanded) from the stored `syncToken`.

- **Active Runtime**: with `watch_enabled`, a listener polls `calendar_id` when the
  Run starts and then every `watch_interval_sec` seconds until Stop, and publishes
  the changes on `changed`.
- **One Shot** (or Active Runtime without the listener): a `watch` request performs
  one poll and returns. A `watch` request is refused with `busy` while the Run
  listener of the same node is polling.
- **First poll (baseline)**: records the current position only, so existing events
  are never replayed. With `emit_existing`, it also reports the 100 soonest
  upcoming events once, as `existing`.
- **Later polls**: report each created, updated or cancelled event once. `created`
  means the event was created after the previous poll; an event created and edited
  between two polls is reported once, as created.
- **Positions survive restarts**: the sync token and the queue of waiting changes are
  stored in the node's storage (`.../instances/<instance>/blocs/<node>/google-calendar-watch.json`),
  written atomically before a batch is published. A new Run resumes from there and
  nothing already published is replayed; a batch the Run refuses is kept for the
  next Run. The file holds tokens, event IDs, change kinds and timestamps, never
  titles, guests, descriptions or the calendar address.
- **Bounds**: a poll reads at most `max_pages` pages (a large first listing continues
  at the next polls) and publishes at most `watch_max_changes` changes, oldest
  first; the others wait (at most 1000). Two polls of the same node are always at
  least 60 seconds apart, across Runs and requests (`too_soon` otherwise).
- **Expired token (HTTP 410)**: Google sometimes invalidates sync tokens. The block
  then performs one full resynchronization (bounded by `max_pages` per poll) and
  reports the events updated since the last completed poll. Deletions during that
  gap are not reported: Google keeps only the ID of a deleted event, so its time
  cannot be checked. Events changed in the two minutes before the expiry may be
  reported a second time as `updated`.
- A change whose event can no longer be read (deleted since) is skipped; its
  cancellation arrives with the next poll. A transient failure while reading an
  event keeps the remaining changes queued (`incomplete` in the receipt).

## Authentication

The block uses the same wallet credential as Google Sheets, Google Drive and
Calendar Booking, so one grant can serve several Google blocks. Store one of these
values in an unlocked wallet secret, then configure only its reference, for example
`secret://workspace/google_calendar`:

```json
{"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_CLIENT_SECRET", "refresh_token": "YOUR_REFRESH_TOKEN"}
```

or `{"access_token": "YOUR_ACCESS_TOKEN"}`, or the bare access token text. These
are placeholders, not credentials. With the refresh-token form, the block
exchanges the refresh token at `https://oauth2.googleapis.com/token` once per
request (once per poll for the listener) and keeps the short-lived access token
only in memory; nothing is cached or persisted. Service-account keys, API keys and
environment-variable credentials are not supported.

Minimal scopes:

- reading, free/busy and watching only:
  `https://www.googleapis.com/auth/calendar.readonly`;
- creating, updating and deleting events:
  `https://www.googleapis.com/auth/calendar.events`, plus
  `https://www.googleapis.com/auth/calendar.readonly` when the same grant also
  lists calendars, queries free/busy or watches (a poll reads the calendar resource).

`https://www.googleapis.com/auth/calendar` covers everything. Google also offers
narrower per-feature scopes; check its scope reference for each method used (see
Google Calendar API usage) and choose the narrowest set that fits the workflow. The
account also needs permission on each calendar.

## Example

1. Create an OAuth client and a refresh token with a Calendar scope, and store the
   JSON grant in the wallet as `google_calendar`.
2. Set `enabled = true`, `secret_ref = secret://workspace/google_calendar` and
   `default_time_zone = Europe/Paris`.
3. Wire a Text block containing
   `{"time_min": "now", "time_max": "2026-11-09", "order_by": "startTime"}` to
   `request` and a Display to `events`: One Shot returns the coming events.
4. To create an event, enable `allow_writes`, add `create_event` to
   `allowed_operations` and send
   `{"operation": "create_event", "summary": "Planning", "start": "2026-11-04T10:00", "duration_minutes": 45, "attendees": ["guest@example.com"]}`.
   Guests are not emailed unless you raise `send_updates`.
5. To react to new meetings, enable `watch_enabled`, run in Active Runtime and wire
   `changed` to the next block. The first poll records the position; later polls
   publish what changed.

## Limits and warnings

- Reads (listings, events, free/busy, watch polls) are retried after HTTP 429, a 403
  rate-limit reason (`userRateLimitExceeded`, `rateLimitExceeded`), 5xx or a lost
  connection. Creations, patches and deletions are retried only after a rate-limit
  answer (Google did not process them); a change that fails after it was sent is
  reported with `uncertain: true` and never replayed. `Retry-After` is honoured up
  to `max_retry_wait_sec`, within the operation deadline. Usage limits
  (`quotaExceeded`, `dailyLimitExceeded`) are not retried.
- Deleting an event cancels it for every guest; with `send_updates = none` they are
  not told by email. Deletion has its own switch, `allow_delete`.
- An update replaces list fields (`attendees`, `reminders`, `recurrence`) as a whole.
- Conference links (Meet), attachments, working locations, colors and event
  moves between calendars are not supported in 0.1.0.
- `query` uses Google's own text matching. Free/busy is limited by Google to its
  maximum window and to 50 calendars per request.
- No HTTP proxy support; the host needs direct outbound HTTPS to the Google origins.
- Event titles, descriptions and guest data are untrusted data. Do not feed them to
  tools that execute commands.

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`, `zoneinfo`); no
`requirements.txt`. Time zone names are checked with the system time zone database
when it is available; without one, Google validates them.

## Security

- The wallet value is read at run time, only after settings and the request are
  validated (and, for `watch`, after the storage, lock and interval checks). Tokens
  never appear in settings, outputs, logs, run metadata, the watch state or error
  messages; provider messages are bounded and credentials removed.
- Guest notifications are off by default and a request can only lower them.
- TLS certificates and host names are always verified; there is no insecure mode.
- Redirects, cookies, proxies and compressed responses are not accepted.
- `api_base_url` and `oauth_base_url` other than the official origins receive the
  credentials: change them only for a trusted gateway or a local test double.
- The block opens no listening port; watching is outbound polling only.

## Google Calendar API usage

Calendar API **v3** on `https://www.googleapis.com/calendar/v3`:

- `GET /users/me/calendarList` (`maxResults`, `pageToken`, `minAccessRole`, `showHidden`)
- `GET /calendars/{calendarId}` (watch: resolves `primary` and checks access)
- `GET /calendars/{calendarId}/events` (`timeMin`, `timeMax`, `q`, `singleEvents`,
  `orderBy`, `showDeleted`, `timeZone`, `maxResults`, `pageToken`, `syncToken`)
- `GET /calendars/{calendarId}/events/{eventId}` (`timeZone`)
- `POST /calendars/{calendarId}/events` (`sendUpdates`)
- `PATCH /calendars/{calendarId}/events/{eventId}` (`sendUpdates`, `If-Match`)
- `DELETE /calendars/{calendarId}/events/{eventId}` (`sendUpdates`, `If-Match`)
- `POST /freeBusy` (`timeMin`, `timeMax`, `timeZone`, `items`)

OAuth 2.0 refresh-token grant: `POST https://oauth2.googleapis.com/token`
(`grant_type=refresh_token`). Errors follow the documented
`{"error": {"code", "message", "errors": [{"reason"}], "status"}}` envelope.

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes; no Google account,
token or calendar was contacted. In particular, the exact content Google returns
for deleted events in a full listing, the expiry behaviour of real sync tokens and
Google's handling of naive date-times with `timeZone` are reproduced from the
documentation, not observed.

## Framing decisions

- One calendar per node for changes and watching, fixed in the properties: a graph
  cannot change another calendar unless `allow_request_calendar` is enabled. Reading
  is not restricted beyond the OAuth scope.
- Separate gates: `enabled`, `allow_writes` (create and patch), `allow_delete` and
  the operation allowlist. `update_event` is a patch, never a full replacement.
- Guest notifications default to `none`; a request may lower but never raise the
  `send_updates` setting, so a graph cannot email guests the operator did not allow.
- Creation duplicates are prevented with an optional client `event_id` (Google
  answers 409), instead of replaying an uncertain creation.
- Times are explicit: a date makes an all-day event, a date-time needs an offset or
  a time zone; there is no guess from the host's clock zone.
- Watch output is one JSON array per poll (`changed`), so both runtimes publish the
  same shape and downstream blocks can split it.
- Watch reports series, not expanded occurrences (`singleEvents=false`), so an
  endless recurring event never floods a poll; modified or cancelled occurrences
  appear as their own items with `recurring_event_id`.
- Created versus updated is decided from Google's `created` time compared with the
  previous poll (with a two-minute margin and a memory of recently reported IDs).
- Queued changes store event IDs only; the content is read when a change is
  published, so the stored state holds no personal data and stays small.
- The watch interval has a 60-second floor that also applies to requested polls and
  across Runs, to protect the account's quota.
- The API and token origins are fixed but overridable for tests; plain HTTP is
  accepted only for loopback hosts.

## Tests

`tests/F5.01_google_calendar.py` checks settings, credentials, every request
contract, time handling (offsets, time zones, all-day events, daylight-saving
durations), event bodies, resource normalization, provider error mapping, watch
classification, queues, persisted state and the node lock, and the block hooks
without a socket. `tests/F5.02_google_calendar_runtime.py` runs the real client
against the loopback double (`tests/calendar_fixture.py`, HTTP and HTTPS):
calendar and event listings with page tokens, windows, text and expansion, one
event and occurrences, free/busy, the refresh grant, creation with client IDs and
duplicates, conditional patches, deletion, 400/401/403/404/409/410/412/429/5xx,
rate-limit 403, lost connections, oversized and compressed bodies, cancellation and
the deadline; then the watch poller (baseline, changes once, queue bounds, skips,
transient failures, multi-page baseline, 410 resynchronization, `emit_existing`,
the 60-second floor) and the Run listener (restart without replay, refused
hand-off, failures, lock, Stop during a pending request); and real graphs in One
Shot and Active Runtime for bundled, managed and linked packages, including Stop
while a creation is pending. `tests/F8.01_google_calendar_ui.py` covers the English
and French forms at desktop and mobile widths. They run in the private BloxSmith
workspace against a pinned framework; the initial development version does not
imply a release.

## License

Block code: Apache-2.0. Google Calendar is an external service governed by its own
terms. No Google SDK, logo, account data or credential is bundled.
