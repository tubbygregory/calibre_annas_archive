"""Exercise the installed plugin in Calibre with offline HTTP fixtures."""

import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from calibre.constants import get_version
from calibre.customize.ui import available_store_plugins
from calibre.gui2.ebook_download import download_file
from calibre.gui2.store.search_result import SearchResult
from calibre.utils.config import JSONConfig

try:
    from qt.core import QApplication
except ImportError:
    from PyQt5.QtWidgets import QApplication


def main():
    check = unittest.TestCase()
    app = QApplication.instance() or QApplication([])
    launch = Mock()
    gui = SimpleNamespace(job_manager=SimpleNamespace(launch_gui_app=launch))
    base = next(plugin for plugin in available_store_plugins() if plugin.name == "Anna's Archive")
    store = base.load_actual_plugin(gui)

    from calibre_plugins.store_annas_archive import annas_archive
    from calibre_plugins.store_annas_archive.constants import FORMATS
    from calibre_plugins.store_annas_archive.http_client import HttpClient

    check.assertEqual(tuple(base.formats), FORMATS)
    support = runpy.run_path(str(Path(__file__).with_name('support.py')))
    mirror, backup = support['MIRROR'], support['BACKUP']
    fake_response, fixture = support['FakeResponse'], support['fixture']
    pdf = support['PDF']

    with TemporaryDirectory() as folder:
        store.config = JSONConfig('store', base_path=folder)
        store.config['mirrors'] = [mirror, backup]
        store.config['search'] = {'language': ['en']}
        widget = store.config_widget()
        check.assertEqual(widget.mirrors.topLevelItem(0).text(0), mirror)
        widget._move_mirror(-1)
        widget.prefer_epub.setChecked(True)
        widget.tags.setText('smoke test')
        store.save_settings(widget)

        store.config = JSONConfig('store', base_path=folder)
        restored = store.config_widget()
        check.assertEqual(restored.mirrors.topLevelItem(0).text(0), backup)
        check.assertTrue(restored.prefer_epub.isChecked())
        check.assertEqual(restored.tags.text(), 'smoke test')

        main_thread = get_ident()
        resume = Event()

        def probe(address):
            check.assertNotEqual(get_ident(), main_thread)
            check.assertEqual(address, mirror)
            check.assertTrue(resume.wait(2))

        with patch.object(store.backend, 'test_mirror', side_effect=probe):
            restored._test_mirror()
            check.assertEqual(restored.mirrors.currentItem().text(1), 'Checking…')
            resume.set()
            until = monotonic() + 5
            while restored.testing and monotonic() < until:
                app.processEvents()
                sleep(0.01)
            check.assertEqual(restored.mirrors.currentItem().text(1), 'Search page verified')

        def respond(request, timeout):
            url = request.full_url
            parts = urlsplit(url)
            path = parts.path
            if path == '/search':
                query = parse_qs(parts.query)
                check.assertIn(query['q'][0], ('library', '9780141439518'))
                check.assertEqual(query['lang'], ['en'])
                return fake_response(url, fixture('search-page-1.html'))
            if path.startswith('/md5/'):
                return fake_response(url, b'<div id="md5-panel-downloads"><a download href="/get?key=8">File</a></div>')
            if path == '/get':
                return fake_response(url, pdf, content_type='application/pdf')
            raise AssertionError(f'Unexpected HTTP request for {path}')

        store.backend.client = HttpClient(Mock(**{'open.side_effect': respond}))
        isbn_book, = list(store.search('978-0-14-143951-8', max_results=1))
        check.assertEqual(isbn_book.formats, 'EPUB')
        first, book = list(store.search('library', max_results=2))
        check.assertIsInstance(book, SearchResult)
        check.assertEqual((first.formats, book.formats), ('EPUB', 'PDF'))
        check.assertTrue(store.get_details(book))
        check.assertEqual(book.downloads, {'PDF': backup + '/get?key=8'})
        downloaded = Path(download_file(book.downloads['PDF'], filename='sample.pdf', create_browser=book.create_browser))
        try:
            check.assertEqual(downloaded.read_bytes(), pdf)
        finally:
            downloaded.unlink()

        with patch.object(annas_archive, 'open_url') as browser:
            store.open(gui=gui, parent=restored, detail_item=book.detail_item, external=True)
            check.assertEqual(browser.call_args[0][0].toString(), book.detail_item)
            store.open(restored, book.detail_item, True)
            check.assertEqual(browser.call_count, 2)

            check.assertTrue(store.get_details(first))
            check.assertFalse(first.downloads)
            store.open(gui=gui, parent=restored, detail_item=first.detail_item)
            check.assertTrue(browser.call_args[0][0].toString().endswith('#md5-panel-downloads'))

        store.open(gui)
        launch.assert_called_once()
        check.assertEqual(launch.call_args[0][0], 'store-dialog')

    print(f'Calibre {get_version()} plugin, Qt preferences, browser fallback and native download smoke passed')


if __name__ == '__main__':
    main()
