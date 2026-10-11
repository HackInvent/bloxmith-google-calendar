"""Watch positions in the node-owned directory, written atomically.

The document holds no token, calendar address or event content: a digest per
watched calendar, Google sync and page tokens, event IDs, change kinds and
timestamps. A held advisory lock keeps two pollers of the same node (the Run
listener and a requested poll, or two Runs) from polling concurrently.
"""

import fcntl
import json
import os
from pathlib import Path
import stat

from . import logic

STATE_NAME = "google-calendar-watch.json"
LOCK_NAME = "google-calendar-watch.lock"
STATE_LIMIT = 4194304


def directory(provider):
    """Resolve the node storage directory from the framework service; refuse when it is unavailable."""
    if not callable(provider):
        logic.fail("storage")
    try:
        path = Path(provider())
    except Exception:
        logic.fail("storage")
    if path.is_symlink() or not path.is_dir():
        logic.fail("storage")
    return path


class WatchStore:
    def __init__(self, folder):
        self.directory = Path(folder)
        self.lock = None

    def acquire(self):
        """Take the exclusive poller lock; False when another poller holds it."""
        if self.directory.is_symlink() or not self.directory.is_dir():
            logic.fail("storage")
        try:
            descriptor = os.open(self.directory / LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            logic.fail("storage")
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            logic.fail("storage")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        self.lock = descriptor
        return True

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def load(self):
        """Return the validated document; a missing file is an empty state, never a guess."""
        path = self.directory / STATE_NAME
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return logic.empty_document()
        except OSError:
            logic.fail("state_invalid")
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > STATE_LIMIT:
                logic.fail("state_invalid")
            raw = stream.read(STATE_LIMIT + 1)
        try:
            value = logic.strict_json(raw, STATE_LIMIT, "state_invalid")
        except logic.CalendarError:
            logic.fail("state_invalid")
        return logic.watch_document(value)

    def save(self, document):
        """Write a complete validated document with fsync and an atomic rename."""
        logic.watch_document(document)
        data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(data) > STATE_LIMIT:
            logic.fail("storage")
        temporary = self.directory / (STATE_NAME + ".tmp")
        try:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / STATE_NAME)
            folder = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except OSError:
            logic.fail("storage")
