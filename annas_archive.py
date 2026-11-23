"""Calibre store adapter for Anna's Archive."""

import logging
import re
from functools import partial
from html import escape
from urllib.parse import urljoin

from calibre.ebooks.metadata import check_isbn
from calibre.gui2 import error_dialog, open_url
from calibre.gui2.store import StorePlugin
from calibre.gui2.store.search_result import SearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog

from .downloads import DownloadBrowser, resolve_downloads
from .feedback import SearchFeedback
from .http_client import ArchiveError, Deadline, InFlightLookups, origin, web_url
from .parsing import download_panel, fetch_cover, normalise_formats, read_html, record_path
from .search import Search, search_url
from .settings import configured_mirrors, link_options, search_options

try:
    from qt.core import QUrl
except ImportError:
    from PyQt5.QtCore import QUrl

logger = logging.getLogger(__name__)
DOWNLOAD_PANEL = '#md5-panel-downloads'


class AnnasArchiveStore(StorePlugin):
    def __init__(self, gui, name, config=None, base_plugin=None):
        super().__init__(gui, name, config, base_plugin)
        self.backend = Search()
        self.feedback = SearchFeedback()
        self.lookups = InFlightLookups()

    def search(self, query, max_results=10, timeout=60):
        # The website redirects hyphenated ISBNs to an unfiltered search.
        if '-' in query and re.fullmatch(r'[0-9-]+[0-9Xx]', query.strip()):
            query = check_isbn(query) or query
        config = dict(self.config)
        search_number = self.feedback.start()
        try:
            for book in self.backend.books(config, query, max_results, timeout):
                result = SearchResult()
                result.detail_item = urljoin(book.mirror, book.path)
                # Pass cover bytes via get_details so Calibre does not open an unchecked URL.
                result._archive_cover_url = book.cover
                # Calibre inserts these strings into rich-text cells.
                result.title = escape(book.title, quote=False)
                result.author = escape(book.author, quote=False)
                result.formats = ', '.join(book.formats)
                result.price = '$0.00'
                result.drm = SearchResult.DRM_UNKNOWN
                yield result
        except ArchiveError as error:
            logger.warning('Anna’s Archive search [%s]: %s', error.category, error)
            mirror = self.backend.mirrors.preferred(configured_mirrors(config))
            browser_url = search_url(mirror, query.strip(), 1, search_options(config)) if mirror else ''
            self.feedback.changed.emit(search_number, str(error), browser_url)
            raise
        except Exception as error:
            logger.error('Anna’s Archive search [application]: %s', type(error).__name__)
            raise

    def _record(self, detail_item, mirrors):
        path = record_path(detail_item)
        preferred = self.backend.mirrors.preferred(mirrors)
        supplied = web_url(detail_item)
        if supplied and origin(supplied) in mirrors:
            preferred = origin(supplied)
        return preferred, path

    def open(self, parent=None, detail_item=None, external=False, *, gui=None):
        mirrors = configured_mirrors(self.config)
        mirror, path = self._record(detail_item, mirrors)
        if not mirror:
            error_dialog(parent, _('Anna’s Archive'), _('Add a mirror in the plugin preferences.'), show=True)
            return
        url = urljoin(mirror, path) if path else mirror
        browser_fallback = isinstance(detail_item, str) and detail_item.endswith(DOWNLOAD_PANEL)
        if external or self.config.get('open_external', False) or browser_fallback:
            open_url(QUrl(url + DOWNLOAD_PANEL if browser_fallback and path else url))
            return
        dialog = WebStoreDialog(gui or self.gui, mirror, parent, url if path else None)
        dialog.setWindowTitle(self.name)
        dialog.set_tags(self.config.get('tags', ''))
        dialog.exec_()

    def _load_record(self, mirrors, preferred, path, formats, options, deadline):
        client = self.backend.client

        def load(candidate):
            url = urljoin(candidate, path)
            with client.open(url, deadline, archive_mirrors=mirrors) as response:
                destination = response.geturl()
                if record_path(destination) != path:
                    raise ArchiveError('parse', 'redirect did not return the requested record')
                document = read_html(response)
            download_panel(document)
            return document, destination

        _, (document, destination) = self.backend.mirrors.fetch(
            mirrors, 'record', load, deadline, preferred=preferred, track_health=False,
        )
        return destination, resolve_downloads(client, document, destination, formats, options, deadline)

    def _record_downloads(self, mirrors, mirror, path, formats, options, deadline):
        key = (mirrors, mirror, path, formats, tuple(options.items()))
        return self.lookups.run(
            key, deadline, partial(self._load_record, mirrors, mirror, path, formats, options, deadline),
        )

    def _update_cover(self, result, deadline):
        url = getattr(result, '_archive_cover_url', '')
        if not url or result.cover_data or not deadline.remaining():
            return False
        cover_deadline = Deadline(min(3, deadline.remaining()))
        try:
            result.cover_data = self.lookups.run(
                ('cover', url), cover_deadline, partial(fetch_cover, self.backend.client, url, cover_deadline),
            )
        except ArchiveError as error:
            logger.debug('Anna’s Archive cover [%s]: %s', error.category, error)
        return bool(result.cover_data)

    def get_details(self, search_result, timeout=60):
        deadline = Deadline(timeout)
        cover_changed = self._update_cover(search_result, deadline)
        previous_item = search_result.detail_item
        had_downloads = bool(search_result.downloads)
        config = dict(self.config)
        mirrors = configured_mirrors(config)
        mirror, path = self._record(search_result.detail_item, mirrors)
        formats = normalise_formats(search_result.formats)
        search_result.downloads.clear()
        search_result.create_browser = self.create_browser
        if mirror and path:
            search_result.detail_item = urljoin(mirror, path) + DOWNLOAD_PANEL
        changed = cover_changed or had_downloads or search_result.detail_item != previous_item
        if not mirror or not path or not formats:
            return changed

        options = link_options(config)
        try:
            destination, downloads = self._record_downloads(mirrors, mirror, path, formats, options, deadline)
            search_result.detail_item = destination + DOWNLOAD_PANEL
        except ArchiveError as error:
            logger.debug('Anna’s Archive record [%s]: %s', error.category, error)
            return changed
        except Exception as error:
            logger.error('Anna’s Archive record [application]: %s', type(error).__name__)
            raise
        if downloads:
            search_result.downloads.update(downloads)
            search_result.detail_item = destination

            def refresh(fmt, budget):
                _, updated = self._record_downloads(mirrors, mirror, path, (fmt,), options, budget)
                return updated.get(fmt)

            expected_md5 = path.rsplit('/', 1)[1] if path.startswith('/md5/') else None
            search_result.create_browser = partial(
                DownloadBrowser, self.backend.client, downloads, options, refresh=refresh, expected_md5=expected_md5,
            )
        return bool(downloads) or changed or search_result.detail_item != previous_item

    def customization_help(self, gui=False):
        return _('Set search filters, mirror priority, and download preferences for Anna’s Archive.')

    def config_widget(self):
        from .config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.save_settings()
        self.backend.reset()
