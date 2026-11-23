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
from .http_client import ArchiveError, Deadline, origin, web_url
from .parsing import download_panel, normalise_formats, read_html, record_path
from .search import Search
from .settings import configured_mirrors, link_options

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

    def search(self, query, max_results=10, timeout=60):
        # The website redirects hyphenated ISBNs to an unfiltered search.
        if '-' in query and re.fullmatch(r'[0-9-]+[0-9Xx]', query.strip()):
            query = check_isbn(query) or query
        try:
            for book in self.backend.books(dict(self.config), query, max_results, timeout):
                result = SearchResult()
                result.detail_item = urljoin(book.mirror, book.path)
                result.cover_url = book.cover
                # Calibre inserts these strings into rich-text cells.
                result.title = escape(book.title, quote=False)
                result.author = escape(book.author, quote=False)
                result.formats = ', '.join(book.formats)
                result.price = '$0.00'
                result.drm = SearchResult.DRM_UNKNOWN
                yield result
        except ArchiveError as error:
            logger.warning('Anna’s Archive search [%s]: %s', error.category, error)
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

    def get_details(self, search_result, timeout=60):
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
        if not mirror or not path or not formats:
            return had_downloads or search_result.detail_item != previous_item

        deadline = Deadline(timeout)
        client = self.backend.client
        options = link_options(config)

        def load(candidate):
            url = urljoin(candidate, path)
            with client.open(url, deadline, archive_mirrors=mirrors) as response:
                destination = response.geturl()
                if record_path(destination) != path:
                    raise ArchiveError('parse', 'redirect did not return the requested record')
                document = read_html(response)
            download_panel(document)
            return document, destination

        try:
            _, (document, destination) = self.backend.mirrors.fetch(
                mirrors, 'record', load, deadline, preferred=mirror, track_health=False,
            )
            search_result.detail_item = destination + DOWNLOAD_PANEL
            downloads = resolve_downloads(client, document, destination, formats, options, deadline)
        except ArchiveError as error:
            logger.debug('Anna’s Archive record [%s]: %s', error.category, error)
            return had_downloads or search_result.detail_item != previous_item
        except Exception as error:
            logger.error('Anna’s Archive record [application]: %s', type(error).__name__)
            raise
        if downloads:
            search_result.downloads.update(downloads)
            search_result.detail_item = destination
            search_result.create_browser = partial(DownloadBrowser, client, downloads, options)
        return bool(downloads) or had_downloads or search_result.detail_item != previous_item

    def customization_help(self, gui=False):
        return _('Set search filters, mirror priority, and download preferences for Anna’s Archive.')

    def config_widget(self):
        from .config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.save_settings()
        self.backend.reset()
