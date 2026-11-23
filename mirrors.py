"""Keep mirror health in memory without altering the configured priorities."""

import logging
from math import ceil
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from .http_client import ArchiveError

logger = logging.getLogger(__name__)
COOLDOWN_SECONDS = 120


class MirrorPool:
    def __init__(self, *, clock=monotonic):
        self.clock = clock
        self.lock = Lock()
        self.last_success = None
        self.cooldowns = {}

    def preferred(self, mirrors):
        with self.lock:
            if (self.last_success in mirrors and
                    self.cooldowns.get(self.last_success, (0, ''))[0] <= self.clock()):
                return self.last_success
        return mirrors[0] if mirrors else None

    def reset(self):
        with self.lock:
            self.last_success = None
            self.cooldowns.clear()

    def fetch(self, mirrors, operation, load, deadline, *, preferred=None, track_health=True):
        if not mirrors:
            raise ArchiveError('configuration', 'No Anna’s Archive mirrors are configured.')
        ordered = tuple(dict.fromkeys((preferred, *mirrors))) if preferred in mirrors else mirrors
        failures = []
        for mirror in ordered:
            host = urlsplit(mirror).netloc
            with self.lock:
                until, reason = self.cooldowns.get(mirror, (0, ''))
                remaining = until - self.clock()
            if remaining > 0:
                failures.append(f'{host} ({reason}; retry in {ceil(remaining)}s)')
                continue
            if not deadline.remaining():
                failures.append('time budget exhausted')
                break
            try:
                value = load(mirror)
            except ArchiveError as error:
                failures.append(f'{host} ({error})')
                logger.warning('Anna’s Archive %s [%s] %s: %s', operation, error.category, host, error)
                if track_health:
                    with self.lock:
                        self.cooldowns[mirror] = (
                            self.clock() + max(COOLDOWN_SECONDS, error.retry_after), str(error)
                        )
                continue
            if track_health:
                with self.lock:
                    self.cooldowns.pop(mirror, None)
                    self.last_success = mirror
            return mirror, value
        raise ArchiveError('mirrors', 'No usable Anna’s Archive mirrors. ' + '; '.join(failures))
