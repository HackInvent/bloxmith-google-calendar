"""Incremental watch of one calendar: Google sync tokens, persisted positions and change delivery.

A pass lists the calendar with events.list (showDeleted, recurring series not
expanded). The first pass (baseline) only records the position: existing events
are never replayed unless emit_existing is set, and then at most 100 upcoming
events are reported. Later passes use the stored syncToken and report each
created, updated or cancelled event. When Google invalidates the token (HTTP
410), a full resynchronization reports only events updated since the last
completed pass. Every pass reads at most max_pages pages per poll and resumes
from its page token on the next poll.

Changes wait in a bounded queue of event IDs. A poll fetches up to
watch_max_changes of them (the current event content, never stored), saves the
shortened queue, then hands the batch over: delivery is at-most-once at the
Stop boundary. If the Run refuses the batch, the previous state is restored and
the changes stay queued for the next Run. All listener state is local to the
Watcher object of one listen_runtime call.
"""

from datetime import datetime, timezone
import time

from . import logic, operations, storage, transport

TRANSIENT = frozenset(("rate_limited", "provider_unavailable", "network", "timeout", "cancelled", "quota_exceeded",
                       "unauthorized", "auth_failed", "tls"))


class Halt(Exception):
    """Stop processing after a refused hand-off or a storage failure."""


def utc(wall):
    return datetime.fromtimestamp(wall, tz=timezone.utc)


def advance(session, entry, config, wall, stats):
    """Read up to max_pages pages of the current pass; return the new entry, the changes found and pass facts."""
    path = operations.calendar_path(config["calendar_id"]) + "/events"
    additions, pages, restarted = [], 0, False
    while pages < config["max_pages"]:
        params = {"maxResults": config["page_size"], "showDeleted": "true", "singleEvents": "false"}
        if entry["phase"] == "incremental":
            params["syncToken"] = entry["sync_token"]
        if entry["page_token"]:
            params["pageToken"] = entry["page_token"]
        try:
            body = session.call("GET", path, params, mutation=False)
        except logic.CalendarError as exc:
            if exc.code not in ("sync_expired", "gone") or restarted:
                raise
            # Google no longer accepts this sync or page token: restart the pass, once per poll.
            restarted = True
            if entry["phase"] == "incremental":
                entry = {**entry, "phase": "resync", "sync_token": None}
                stats["resyncs"] += 1
            entry = {**entry, "page_token": None, "pass_started": None}
            continue
        pages += 1
        if entry["page_token"] is None:
            entry = {**entry, "pass_started": logic.server_time(session.last_date, utc(wall))}
        raw, following, sync, _zone = logic.event_page(body, config["page_size"])
        for item in raw:
            kind = logic.classify(item, entry, emit_existing=config["emit_existing"])
            if kind:
                additions.append(logic.change(item, kind))
        entry = logic.remember(entry, raw)
        if following:
            entry = {**entry, "page_token": following}
            continue
        if not sync:
            logic.fail("protocol", "Google ended a calendar listing without a sync token.")
        entry = {**entry, "phase": "incremental", "sync_token": sync, "page_token": None, "since": entry["pass_started"]}
        break
    return entry, additions, pages


def deliver(session, entry, config, stop, stats):
    """Fetch the oldest queued changes; return (batch, consumed count, deferred transient error or None)."""
    batch, consumed, size, deferred = [], 0, 2, None
    for item in entry["pending"][:config["watch_max_changes"]]:
        if stop():
            break
        if item["change"] == "cancelled":
            found = {"change": "cancelled", "calendar_id": config["calendar_id"], "event": logic.cancelled_event(item)}
        else:
            try:
                event = logic.normalize_event(session.call(
                    "GET", operations.event_path(config["calendar_id"], item["id"]), None, mutation=False))
            except logic.CalendarError as exc:
                if exc.code in TRANSIENT:
                    deferred = exc
                    break
                # Deleted since it was listed (its cancellation arrives with the next pass) or no longer readable.
                stats["skipped"] += 1
                consumed += 1
                continue
            if event["status"] == "cancelled":
                stats["skipped"] += 1
                consumed += 1
                continue
            found = {"change": item["change"], "calendar_id": config["calendar_id"], "event": event}
        weight = logic.weight(found)
        if weight > logic.BATCH_LIMIT - 2:
            found = logic.reduced(found)
            weight = logic.weight(found)
        if batch and size + weight + 1 > logic.BATCH_LIMIT:
            break
        batch.append(found)
        size += weight + 1
        consumed += 1
    return batch, consumed, deferred


def poll(session, document, config, wall, stop, stats):
    """One poll: resolve the calendar, advance its pass, queue changes and fetch a batch. Nothing is saved here."""
    calendar = logic.calendar_resource(session.call("GET", operations.calendar_path(config["calendar_id"]), None,
                                                    mutation=False))
    digest = logic.watch_digest(calendar["id"])
    entry = document["watches"].get(digest) or logic.new_entry()
    baseline = entry["phase"] == "baseline"
    entry, additions, pages = advance(session, entry, config, wall, stats)
    pending = logic.enqueue(entry["pending"], additions)
    if baseline:
        pending = logic.trim_existing(pending)
    dropped = max(0, len(pending) - logic.PENDING_LIMIT)
    if dropped:
        pending = pending[dropped:]
        stats["dropped"] += dropped
    entry = {**entry, "pending": pending}
    batch, consumed, deferred = deliver(session, entry, config, stop, stats)
    entry = {**entry, "pending": entry["pending"][consumed:]}
    following = logic.store_entry({**document, "last_poll": int(wall)}, digest, entry, wall)
    stats["pending"] = len(entry["pending"])
    stats["phase"] = entry["phase"]
    receipt = {"calendar_id": config["calendar_id"], "phase": entry["phase"], "baseline": baseline,
               "baseline_complete": entry["phase"] != "baseline", "pages": pages, "count": len(batch),
               "pending": len(entry["pending"]), "dropped": dropped, "requests": session.requests}
    return following, batch, receipt, deferred


def poll_once(config, request, *, storage_dir, resolve, cancel, wall=time.time):
    """The watch operation: one bounded poll under the node lock, at least 60 s after the previous one."""
    store = storage.WatchStore(storage.directory(storage_dir))
    if not store.acquire():
        logic.fail("busy")
    try:
        document = store.load()
        now = int(wall())
        wait = logic.poll_wait(document, now)
        if wait:
            logic.fail("too_soon", retry_after=wait)
        secret = resolve()
        session = transport.Session(config, secret, cancel)
        stats = {"resyncs": 0, "skipped": 0, "dropped": 0, "pending": 0, "phase": None}
        try:
            following, batch, receipt, deferred = poll(session, document, config, now, cancel, stats)
        except logic.CalendarError:
            try:
                store.save({**document, "last_poll": now})
            except logic.CalendarError:
                pass
            raise
        store.save(following)
    finally:
        store.close()
    receipt = {"request_id": request["request_id"], "operation": "watch", **receipt, "resyncs": stats["resyncs"],
               "skipped": stats["skipped"], "incomplete": deferred is not None,
               "deferred_error": deferred.code if deferred is not None else None}
    return batch, receipt


class Watcher:
    def __init__(self, config, *, resolve_secret, storage_dir, emit, stop, session=transport.Session,
                 monotonic=time.monotonic, wall=time.time):
        self.config, self.resolve_secret, self.storage_dir = config, resolve_secret, storage_dir
        self.emit_callback, self.stop, self.session = emit, stop, session
        self.monotonic, self.wall = monotonic, wall
        self.stats = {"state": "starting", "phase": None, "cycles": 0, "emitted": 0, "skipped": 0, "dropped": 0,
                      "resyncs": 0, "errors": 0, "pending": 0, "last_error": None}
        self.last_error = None
        self.store, self.document = None, None

    def emit(self, kind, value=None):
        self.emit_callback(kind, value, dict(self.stats))

    def wait(self, seconds):
        end = self.monotonic() + max(0.0, seconds)
        while not self.stop() and self.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - self.monotonic())))

    def report(self, error):
        """Emit each distinct failure once; recovery re-arms the report."""
        self.stats.update(state="backoff", last_error=error.code, errors=self.stats["errors"] + 1)
        if error.code != self.last_error and not self.stop():
            self.last_error = error.code
            try:
                self.emit("error", error.envelope(operation="watch", source="watch",
                                                  calendar_id=self.config["calendar_id"]))
            except Exception:
                raise Halt() from None

    def idle(self, error):
        """Fail closed: report once, then poll nothing until framework Stop."""
        try:
            self.report(error)
        except Halt:
            pass
        self.stats["state"] = "blocked"
        while not self.stop():
            time.sleep(0.1)

    def run(self):
        try:
            self.store = storage.WatchStore(storage.directory(self.storage_dir))
        except Exception:
            self.idle(logic.CalendarError("storage"))
            return
        try:
            while True:
                try:
                    acquired = self.store.acquire()
                except logic.CalendarError as exc:
                    self.idle(exc)
                    return
                if acquired:
                    break
                try:
                    self.report(logic.CalendarError("busy"))
                except Halt:
                    pass
                self.wait(2)
                if self.stop():
                    return
            try:
                self.document = self.store.load()
            except logic.CalendarError as exc:
                self.idle(exc)
                return
            self.loop()
        except Halt:
            while not self.stop():
                time.sleep(0.1)
        finally:
            self.store.close()

    def loop(self):
        # The minimum interval also holds across Runs and requested polls of this node.
        due = self.monotonic() + logic.poll_wait(self.document, int(self.wall()))
        while not self.stop():
            now = self.monotonic()
            if now < due:
                self.wait(min(0.1, due - now))
                continue
            delay = self.cycle()
            due = self.monotonic() + max(self.config["watch_interval_sec"], delay or 0)

    def recovered(self):
        self.last_error = None
        self.stats.update(state="listening", last_error=None)

    def status(self):
        if self.stop():
            return
        try:
            self.emit("status")
        except Exception:
            raise Halt() from None

    def save(self, document):
        try:
            self.store.save(document)
        except logic.CalendarError as exc:
            self.idle(exc)
            raise Halt() from None
        self.document = document

    def cycle(self):
        """One poll: store the new position first, then hand the batch over; a refused hand-off restores it."""
        wall = int(self.wall())
        try:
            secret = logic.credentials(self.resolve_secret())
        except logic.CalendarError as exc:
            self.report(exc)
            return None
        except Exception:
            self.report(logic.CalendarError("secret"))
            return None
        session = self.session(self.config, secret, self.stop)
        try:
            following, batch, receipt, deferred = poll(session, self.document, self.config, wall, self.stop,
                                                       self.stats)
        except logic.CalendarError as exc:
            if self.stop():
                return None
            self.save({**self.document, "last_poll": wall})
            self.report(exc)
            return exc.retry_after
        previous = self.document
        self.save(following)
        if batch:
            try:
                self.emit("changed", batch)
            except Exception:
                # Not handed off: restore the stored position so the next Run delivers these changes again.
                try:
                    self.store.save(previous)
                    self.document = previous
                except logic.CalendarError:
                    pass
                raise Halt() from None
            self.stats["emitted"] += len(batch)
        if receipt["dropped"] and not self.stop():
            try:
                self.emit("error", logic.CalendarError("overflow").envelope(
                    operation="watch", source="watch", calendar_id=self.config["calendar_id"], dropped=receipt["dropped"]))
            except Exception:
                raise Halt() from None
        if deferred is not None:
            if self.stop():
                return None
            self.report(deferred)
            return deferred.retry_after
        self.recovered()
        self.stats["cycles"] += 1
        self.status()
        return None
