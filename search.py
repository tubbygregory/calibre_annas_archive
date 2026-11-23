"""Search Anna's Archive with bounded pagination, failover, and metadata caching."""

import logging
from collections import OrderedDict
from threading import Lock
from time import monotonic
from urllib.parse import urlencode, urljoin

from .constants import FILTERS
from .http_client import ArchiveError, Deadline, HttpClient
from .mirrors import MirrorPool
from .parsing import parse_search, read_html
from .settings import cache_seconds, configured_mirrors, search_options

logger = logging.getLogger(__name__)
MAX_CACHE_ENTRIES = 32
MAX_PAGES = 20


def search_url(mirror, query, page, options):
    params = [('q', query), ('display', 'table'), ('page', page)]
    if options['order']:
        params.append(('sort', options['order']))
    params.extend((field.parameter, value) for field in FILTERS for value in options[field.key])
    return urljoin(mirror, '/search?' + urlencode(params))


class Search:
    def __init__(self, client=None, *, clock=monotonic):
        self.client = client if client is not None else HttpClient()
        self.mirrors = MirrorPool(clock=clock)
        self.clock = clock
        self.cache = OrderedDict()
        self.cache_lock = Lock()

    def reset(self):
        self.mirrors.reset()
        with self.cache_lock:
            self.cache.clear()

    def _load(self, mirror, mirrors, query, page, options, deadline):
        url = search_url(mirror, query, page, options)
        with self.client.open(url, deadline, archive_mirrors=mirrors) as response:
            return parse_search(read_html(response), response.geturl(), page)

    def test_mirror(self, mirror):
        # Probe a normal search and check its shape, including a legitimate empty result.
        self._load(mirror, (mirror,), 'Pride and Prejudice', 1, search_options({}), Deadline(8, clock=self.clock))

    def books(self, config, query, limit, timeout):
        query = query.strip()
        if not query or limit <= 0:
            return
        mirrors = configured_mirrors(config)
        options = search_options(config)
        epub_first = config.get('prefer_epub', False) is True
        ttl = cache_seconds(config)
        key = (mirrors, tuple(options.items()), epub_first, query, limit)
        if ttl:
            with self.cache_lock:
                cached = self.cache.pop(key, None)
                if cached and self.clock() - cached[0] < ttl:
                    self.cache[key] = cached
                else:
                    cached = None
            if cached:
                yield from cached[1]
                return

        deadline = Deadline(timeout, clock=self.clock)
        books = []
        seen = set()
        mirror = None
        page = 1
        for _ in range(MAX_PAGES):
            try:
                mirror, result = self.mirrors.fetch(
                    mirrors, 'search',
                    lambda candidate, page=page: self._load(candidate, mirrors, query, page, options, deadline),
                    deadline, preferred=mirror,
                )
            except ArchiveError as error:
                if not books:
                    raise
                logger.warning('Anna’s Archive pagination [%s] after %d results: %s', error.category, len(books), error)
                return

            batch = sorted(result.books, key=lambda book: 'EPUB' not in book.formats) if epub_first else result.books
            for book in batch:
                if book.path in seen:
                    continue
                seen.add(book.path)
                books.append(book)
                yield book
                if len(books) >= limit:
                    break
            if len(books) >= limit or result.next_page is None:
                break
            page = result.next_page
        else:
            logger.warning('Anna’s Archive reached the search page limit.')
            return

        if ttl:
            with self.cache_lock:
                self.cache[key] = (self.clock(), tuple(books))
                self.cache.move_to_end(key)
                if len(self.cache) > MAX_CACHE_ENTRIES:
                    self.cache.popitem(last=False)
