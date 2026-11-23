"""Exercise the installed plugin in Calibre with offline HTTP fixtures."""

import runpy
import unittest
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from hashlib import md5
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from calibre.constants import get_version
from calibre.customize.ui import available_store_plugins
from calibre.db.legacy import LibraryDatabase
from calibre.ebooks.metadata.meta import get_metadata
from calibre.gui2.ebook_download import download_file
from calibre.gui2.store.search_result import SearchResult
from calibre.utils.config import JSONConfig

try:
    from qt.core import QApplication, QDialog, QVBoxLayout
except ImportError:
    from PyQt5.QtWidgets import QApplication, QDialog, QVBoxLayout


def sample_epub(cover):
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '''
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="package.opf" media-type="application/oebps-package+xml"/></rootfiles>
            </container>
        ''', ZIP_DEFLATED)
        archive.writestr('package.opf', '''
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book">
              <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                <dc:identifier id="book">annas-archive-smoke</dc:identifier>
                <dc:title>Archive Smoke Test</dc:title><dc:creator>Example Author</dc:creator>
                <dc:language>en</dc:language><meta property="dcterms:modified">2026-11-23T00:00:00Z</meta>
                <meta name="cover" content="cover"/>
              </metadata>
              <manifest>
                <item id="text" href="book.xhtml" media-type="application/xhtml+xml" properties="nav"/>
                <item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>
              </manifest>
              <spine><itemref idref="text"/></spine>
            </package>
        ''', ZIP_DEFLATED)
        archive.writestr('book.xhtml', '''
            <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
              <head><title>Archive Smoke Test</title></head>
              <body><nav epub:type="toc"><ol><li><a href="#entry">Entry</a></li></ol></nav>
                <p id="entry">A sample ebook for the offline store smoke test.</p>
              </body>
            </html>
        ''', ZIP_DEFLATED)
        archive.writestr('cover.png', cover, ZIP_DEFLATED)
    return stream.getvalue()


def check_search_warning(check, app, store, support):
    from calibre_plugins.store_annas_archive import feedback
    from calibre_plugins.store_annas_archive.http_client import ArchiveError, HttpClient

    fake_response, fixture = support['FakeResponse'], support['fixture']
    backup = support['BACKUP']
    dialog = QDialog()
    QVBoxLayout(dialog)
    dialog.show()

    def unavailable(request, timeout):
        url = request.full_url
        if url.startswith(backup):
            return fake_response(url, code=429, headers={'Retry-After': '90'})
        return fake_response(url, headers={'CF-Mitigated': 'challenge'})

    with patch.object(store.feedback, '_active_dialog', return_value=dialog), ThreadPoolExecutor(max_workers=1) as worker:
        store.backend.reset()
        store.backend.client = HttpClient(Mock(**{'open.side_effect': unavailable}))
        with check.assertRaises(ArchiveError):
            worker.submit(lambda: list(store.search('private query term 1234'))).result(10)
        app.processEvents()
        banner = dialog.findChild(feedback.SearchWarning)
        check.assertIsNotNone(banner)
        check.assertIn('rate limited', banner.message.text())
        check.assertIn('browser verification required', banner.message.text())
        check.assertNotIn('private query term 1234', banner.message.text())
        check.assertFalse(banner.isHidden())
        with patch.object(feedback, 'open_url') as browser:
            banner.browser.click()
            url = urlsplit(browser.call_args.args[0].toString())
            check.assertEqual(url.netloc, urlsplit(backup).netloc)
            check.assertEqual(parse_qs(url.query)['q'], ['private query term 1234'])

        store.backend.reset()
        store.backend.client = HttpClient(Mock(**{
            'open.side_effect': lambda request, timeout: fake_response(request.full_url, fixture('search-empty.html')),
        }))
        check.assertEqual(worker.submit(lambda: list(store.search('empty search'))).result(10), [])
        app.processEvents()
        check.assertTrue(banner.isHidden())
    dialog.close()


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
    cover = b64decode('iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR4nGNQSZmNFTEMLQkAT6xIwQxquW8AAAAASUVORK5CYII=')
    epub = sample_epub(cover)
    pdf_id, epub_id = md5(pdf).hexdigest(), md5(epub).hexdigest()

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

        epub_record_reads = 0

        def respond(request, timeout):
            nonlocal epub_record_reads
            url = request.full_url
            parts = urlsplit(url)
            path = parts.path
            if path == '/search':
                query = parse_qs(parts.query)
                check.assertIn(query['q'][0], ('library', '9780141439518'))
                check.assertEqual(query['lang'], ['en'])
                table = fixture('search-page-1.html').replace(b'1' * 32, pdf_id.encode('ascii'))
                return fake_response(url, table.replace(b'2' * 32, epub_id.encode('ascii')))
            if path == '/covers/library.jpg':
                return fake_response(url, cover, content_type='image/png')
            if path == f'/md5/{pdf_id}':
                return fake_response(url, b'<div id="md5-panel-downloads"><a download href="/get?key=8">File</a></div>')
            if path == f'/md5/{epub_id}':
                epub_record_reads += 1
                href = '/old.epub' if epub_record_reads == 1 else '/fresh.epub'
                markup = f'<div id="md5-panel-downloads"><a download href="{href}">EPUB</a></div>'
                return fake_response(url, markup.encode('utf-8'))
            if path == '/old.epub' and request.get_header('Range') is None:
                return fake_response(url, code=403, headers={'X-Amz-Error-Code': 'ExpiredToken'})
            if path in {'/old.epub', '/fresh.epub'}:
                return fake_response(url, epub, content_type='application/epub+zip',
                                     headers={'Content-Length': str(len(epub))})
            if path.startswith('/md5/'):
                return fake_response(url, b'<div id="md5-panel-downloads"></div>')
            if path == '/get':
                return fake_response(url, pdf, content_type='application/pdf')
            raise AssertionError(f'Unexpected HTTP request for {path}')

        store.backend.client = HttpClient(Mock(**{'open.side_effect': respond}))
        isbn_book, = list(store.search('978-0-14-143951-8', max_results=1))
        check.assertEqual(isbn_book.formats, 'EPUB')
        first, book = list(store.search('library', max_results=2))
        check.assertIsInstance(book, SearchResult)
        check.assertEqual((first.formats, book.formats), ('EPUB', 'PDF'))
        check.assertEqual(book.cover_url, '')
        check.assertTrue(store.get_details(book))
        check.assertEqual(book.cover_data, cover)
        check.assertEqual(book.downloads, {'PDF': backup + '/get?key=8'})
        downloaded = Path(download_file(book.downloads['PDF'], filename='sample.pdf', create_browser=book.create_browser))
        try:
            check.assertEqual(downloaded.read_bytes(), pdf)
        finally:
            downloaded.unlink()

        check.assertTrue(store.get_details(first))
        check.assertEqual(first.downloads, {'EPUB': backup + '/old.epub'})
        downloaded = Path(download_file(first.downloads['EPUB'], filename='sample.epub', create_browser=first.create_browser))
        library = LibraryDatabase(str(Path(folder) / 'library'))
        try:
            check.assertEqual(downloaded.read_bytes(), epub)
            check.assertEqual(epub_record_reads, 2)
            with downloaded.open('rb') as stream:
                metadata = get_metadata(stream, 'epub')
            ids, duplicates = library.new_api.add_books([(metadata, {'EPUB': str(downloaded)})])
            check.assertEqual(len(ids), 1)
            check.assertFalse(duplicates)
            book_id, = ids
            check.assertEqual(library.new_api.field_for('title', book_id), 'Archive Smoke Test')
            check.assertTrue(library.new_api.has_format(book_id, 'EPUB'))
            check.assertTrue(library.new_api.cover(book_id))
        finally:
            library.close()
            downloaded.unlink()

        with patch.object(annas_archive, 'open_url') as browser:
            store.open(gui=gui, parent=restored, detail_item=book.detail_item, external=True)
            check.assertEqual(browser.call_args[0][0].toString(), book.detail_item)
            store.open(restored, book.detail_item, True)
            check.assertEqual(browser.call_count, 2)

            missing = SearchResult()
            missing.detail_item = backup + '/md5/' + '3' * 32
            missing.formats = 'EPUB'
            check.assertTrue(store.get_details(missing))
            check.assertFalse(missing.downloads)
            store.open(gui=gui, parent=restored, detail_item=missing.detail_item)
            check.assertTrue(browser.call_args[0][0].toString().endswith('#md5-panel-downloads'))

        store.open(gui)
        launch.assert_called_once()
        check.assertEqual(launch.call_args[0][0], 'store-dialog')
        check_search_warning(check, app, store, support)

    print(f'Calibre {get_version()} plugin, Qt feedback, browser fallback and native EPUB import smoke passed')


if __name__ == '__main__':
    main()
