import socket
import unittest
from unittest.mock import Mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from tests.support import BACKUP, MIRROR, FakeResponse, fixture

from calibre_plugins.store_annas_archive.http_client import ArchiveError, HttpClient
from calibre_plugins.store_annas_archive.parsing import parse_html, parse_search, record_path
from calibre_plugins.store_annas_archive.search import Search, search_url
from calibre_plugins.store_annas_archive.settings import search_options

NOW = 1000
LOGGER = 'calibre_plugins.store_annas_archive.mirrors'


class SearchParsingTest(unittest.TestCase):
    def test_table_with_reordered_and_missing_columns(self):
        page = parse_search(parse_html(fixture('search-page-1.html')), MIRROR + '/search?page=1', 1)
        first, second, duplicate = page.books
        self.assertEqual(first.title, 'A library & its history')
        self.assertEqual(first.author, 'Ada Example')
        self.assertEqual(first.formats, ('PDF',))
        self.assertEqual(first.cover, MIRROR + '/covers/library.jpg')
        self.assertEqual((second.author, second.cover, second.formats), ('', '', ('EPUB',)))
        self.assertEqual(first.path, duplicate.path)
        self.assertEqual(page.next_page, 2)

    def test_legacy_headerless_table_uses_primary_title_and_author(self):
        path = '/md5/' + 'a' * 32
        cells = ['', '<span>Latin <em>reader</em><span class="block">Alternative</span></span>',
                 '<span>Jane Smith<span class="block">Also known as</span></span>', 'Publisher', '1998',
                 'File.pdf', 'LG', 'en', 'Book', 'pdf', '3 MB', 'MD5']
        row = ''.join(f'<td><a href="{path}">{cell}</a></td>' for cell in cells)
        document = parse_html(f'<form action="/search"><input name="q"></form>'
                              f'<div id="aarecord-list"><table><tr>{row}</tr></table></div>')
        book, = parse_search(document, MIRROR + '/search', 1).books
        self.assertEqual((book.path, book.title, book.author, book.formats),
                         (path, 'Latin reader', 'Jane Smith', ('PDF',)))

    def test_card_markup_as_fallback(self):
        doc = parse_html(fixture('search-cards.html'))
        book, = parse_search(doc, MIRROR + '/search', 1).books
        self.assertEqual((book.title, book.author, book.formats), ('An illustrated book', 'Eve Author', ('EPUB',)))

    def test_headerless_numeric_title_and_blank_title(self):
        def row(title, author, md5):
            cells = ['', title, author, 'Publisher', '1949', '', '', 'en', 'Book', 'epub', '3 MB', '']
            return '<tr>' + ''.join(f'<td><a href="/md5/{md5}">{cell}</a></td>' for cell in cells) + '</tr>'

        rows = row('1984', 'George Orwell', 'a' * 32) + row('', 'Author without a title', 'b' * 32)
        doc = parse_html(f'<form action="/search"><input name="q"></form>'
                         f'<div id="aarecord-list"><table>{rows}</table></div>')
        book, = parse_search(doc, MIRROR + '/search', 1).books
        self.assertEqual((book.title, book.author), ('1984', 'George Orwell'))

    def test_empty_results_are_a_valid_page(self):
        page = parse_search(parse_html(fixture('search-empty.html')), MIRROR + '/search?page=1', 1)
        self.assertEqual(page.books, ())
        self.assertIsNone(page.next_page)

    def test_record_path_rejects_invalid_hashes_and_traversal(self):
        valid = 'Ab' * 16
        self.assertEqual(record_path(valid), '/md5/' + valid.lower())
        self.assertEqual(record_path('/md5/' + valid + '/?foo=1'), '/md5/' + valid.lower())
        self.assertEqual(record_path('/doi/10.1234/example%20title'), '')  # DOI paths have no spaces.
        self.assertEqual(record_path('/doi/10.1234/a/b'), '/doi/10.1234/a/b')
        for value in ('', None, '/md5/', '/md5/not-a-hash', '/doi/10.1234/%2e%2e/delete'):
            self.assertEqual(record_path(value), '', value)

    def test_search_url_encodes_query_and_repeated_filters(self):
        config = {'search': {'order': 'oldest', 'content': ['book_nonfiction'], 'filetype': ['epub', 'pdf'],
                             'source': 'lgli', 'access': ['external_download'], 'language': ['nl-BE', 'en']}}
        query = 'O’Keeffe & café + % {q}?#'
        url = search_url(MIRROR, query, 2, search_options(config))
        self.assertEqual(urlsplit(url).path, '/search')
        self.assertEqual(parse_qs(urlsplit(url).query), {
            'q': [query], 'page': ['2'], 'display': ['table'], 'sort': ['oldest'],
            'content': ['book_nonfiction'], 'ext': ['epub', 'pdf'], 'src': ['lgli'],
            'acc': ['external_download'], 'lang': ['en', 'nl-BE'],
        })


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.now = NOW
        self.search = Search(HttpClient(self.opener), clock=lambda: self.now)
        self.config = {'mirrors': [MIRROR, BACKUP], 'cache_seconds': 0}

    def run_search(self, limit=10):
        return list(self.search.books(self.config, 'library', limit, 30))

    def respond(self, request, timeout):
        url = request.full_url
        page = parse_qs(urlsplit(url).query)['page'][0]
        return FakeResponse(url, fixture(f'search-page-{page}.html'))

    def test_paginates_through_duplicates_and_stops_at_result_limit(self):
        self.opener.open.side_effect = self.respond
        books = self.run_search(3)
        self.assertEqual([book.title for book in books], ['A library & its history', 'The second book', 'A third book'])
        self.assertEqual(books[-1].formats, ('AZW3', 'EPUB'))
        self.assertEqual(books[-1].cover, '')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_epub_priority_respects_result_limit_and_avoids_extra_pages(self):
        self.config['prefer_epub'] = True
        self.opener.open.side_effect = self.respond
        book, = self.run_search(1)
        self.assertEqual(book.title, 'The second book')
        self.opener.open.assert_called_once()

    def test_pagination_recovers_when_the_first_mirror_redirects_to_page_one(self):
        def respond(request, timeout):
            url = request.full_url
            if url.startswith(MIRROR) and 'page=2' in url:
                return FakeResponse(url, code=302, headers={'Location': '?page=1'})
            return self.respond(request, timeout)
        self.opener.open.side_effect = respond
        with self.assertLogs(LOGGER, 'WARNING'):
            books = self.run_search()
        self.assertEqual(len(books), 4)
        self.assertEqual(books[-1].mirror, BACKUP)
        self.assertEqual(self.opener.open.call_count, 4)

    def test_timeout_and_dns_fail_over_with_cooldown(self):
        third = 'https://mirror-three.example'
        self.config['mirrors'].append(third)

        def respond(request, timeout):
            if request.full_url.startswith(MIRROR):
                raise URLError(TimeoutError())
            if request.full_url.startswith(BACKUP):
                raise URLError(socket.gaierror())
            return self.respond(request, timeout)

        self.opener.open.side_effect = respond
        with self.assertLogs(LOGGER, 'WARNING') as logs:
            self.assertEqual(self.run_search(1)[0].mirror, third)
        self.assertIn('request timed out', '\n'.join(logs.output))
        self.assertIn('DNS lookup failed', '\n'.join(logs.output))
        self.assertEqual(self.config['mirrors'], [MIRROR, BACKUP, third])
        self.assertEqual(self.search.mirrors.preferred(tuple(self.config['mirrors'])), third)
        self.run_search(1)
        self.assertEqual(self.opener.open.call_count, 4)
        self.now += 121
        self.opener.open.side_effect = self.respond
        self.assertEqual(self.run_search(1)[0].mirror, MIRROR)

    def test_remembered_mirror_can_be_removed(self):
        self.opener.open.side_effect = self.respond
        self.run_search(1)
        self.config['mirrors'] = [BACKUP]
        self.assertEqual(self.search.mirrors.preferred((BACKUP,)), BACKUP)
        self.assertEqual(self.run_search(1)[0].mirror, BACKUP)

    def test_empty_mirrors_and_zero_limit(self):
        self.config['mirrors'] = []
        self.assertEqual(self.run_search(0), [])
        with self.assertRaisesRegex(ArchiveError, 'No Anna’s Archive mirrors are configured'):
            self.run_search()
        self.opener.open.assert_not_called()

    def test_http_404_and_200_parking_page_are_failures(self):
        self.opener.open.side_effect = [
            FakeResponse(MIRROR, code=404), FakeResponse(BACKUP + '/search?page=1', b'<h1>Domain for sale</h1>'),
        ]
        with self.assertLogs(LOGGER, 'WARNING'):
            with self.assertRaisesRegex(ArchiveError, 'mirror-one.example.*HTTP 404.*mirror-two.example.*HTML'):
                self.run_search()
        with self.assertRaisesRegex(ArchiveError, 'retry in'):
            self.run_search()
        self.assertEqual(self.opener.open.call_count, 2)

    def test_challenge_page_fails_over(self):
        for response in [
            FakeResponse(MIRROR, b'<title>Just a moment...</title>'),
            FakeResponse(MIRROR, headers={'CF-Mitigated': 'challenge'}),
            FakeResponse(MIRROR, code=302, headers={'Location': '/search?check=1'}),
        ]:
            with self.subTest(response=response):
                self.search.reset()
                self.opener.reset_mock()
                self.opener.open.side_effect = [response, FakeResponse(BACKUP + '/search', fixture('search-empty.html'))]
                with self.assertLogs(LOGGER, 'WARNING') as logs:
                    self.assertEqual(self.run_search(), [])
                self.assertIn('[restriction]', '\n'.join(logs.output))
                self.assertEqual(self.opener.open.call_count, 2)

    def test_rate_limit_cooldown_respects_retry_after(self):
        self.opener.open.side_effect = [
            FakeResponse(MIRROR, code=429, headers={'Retry-After': '600'}),
            FakeResponse(BACKUP + '/search', fixture('search-empty.html')),
            FakeResponse(BACKUP + '/search', fixture('search-empty.html')),
        ]
        with self.assertLogs(LOGGER, 'WARNING'):
            self.run_search()
        self.now += 150
        self.run_search()
        self.assertEqual(self.opener.open.call_args.args[0].host, 'mirror-two.example')

    def test_whole_search_cache_expires_and_is_keyed_by_filters(self):
        self.config['cache_seconds'] = 5
        self.opener.open.side_effect = self.respond
        first = self.run_search(2)
        self.now += 4
        self.assertEqual(self.run_search(2), first)
        self.assertEqual(self.opener.open.call_count, 1)
        self.now += 1
        self.assertEqual(self.run_search(2), first)
        self.assertEqual(self.opener.open.call_count, 2)
        self.config['search'] = {'language': ['en']}
        self.run_search(2)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_pagination_failure_returns_partial_results_without_caching(self):
        self.config.update(mirrors=[MIRROR], cache_seconds=30)

        def respond(request, timeout):
            if 'page=2' in request.full_url:
                return FakeResponse(request.full_url, code=503)
            return self.respond(request, timeout)

        self.opener.open.side_effect = respond
        with self.assertLogs('calibre_plugins.store_annas_archive', 'WARNING'):
            self.assertEqual(len(self.run_search()), 2)
        self.assertFalse(self.search.cache)
