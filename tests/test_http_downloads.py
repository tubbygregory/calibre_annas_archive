import hashlib
import io
import unittest
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError

from tests.support import EPUB, MIRROR, PDF, FakeResponse, fixture

from calibre_plugins.store_annas_archive.downloads import DownloadBrowser, resolve_downloads, url_extension, verified_format
from calibre_plugins.store_annas_archive.http_client import ArchiveError, Deadline, HttpClient, mirror_url, web_url
from calibre_plugins.store_annas_archive.parsing import MAX_COVER_BYTES, fetch_cover, parse_html

STRICT = {'url_extension': True, 'content_type': True}
DEFAULT = {'url_extension': True, 'content_type': False}
RECORD_URL = MIRROR + '/md5/' + '1' * 32


class HttpClientTest(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.client = HttpClient(self.opener)
        self.deadline = Deadline(10)

    def test_url_schemes_and_local_destinations(self):
        for value in (
            'file:///tmp/private', 'ftp://example.com/file', 'javascript:alert(1)', 'https://user:pw@example.com',
            'http://127.0.0.1', 'https://some.local', 'http://localhost', 'http://[::1]', 'http://2130706433',
            'http://0177.0.0.1', 'https://www.example.com\\@evil.example/path', 'https://example.com\n/x',
            'http://example.com:65536', 'http://sub.example.com:0', 'https://book.example.onion',
        ):
            with self.subTest(value=value):
                self.assertEqual(web_url(value), '')
        self.assertEqual(web_url('next/My Book.PDF?key=a%2Fb#preview', MIRROR + '/folder/record'),
                         MIRROR + '/folder/next/My%20Book.PDF?key=a%2Fb')
        self.assertEqual(mirror_url('https://Mirror-One.EXAMPLE:443/'), MIRROR)
        self.assertEqual(mirror_url(MIRROR + '/?q=1'), '')
        self.assertEqual(mirror_url(MIRROR + '/private'), '')

    def test_http_error_is_classified_and_closed(self):
        body = io.BytesIO()
        self.opener.open.side_effect = HTTPError(MIRROR, 403, 'untrusted details', Message(), body)
        with self.assertRaises(ArchiveError) as error:
            self.client.open(MIRROR, self.deadline)
        self.assertEqual((error.exception.category, str(error.exception)), ('restriction', 'HTTP 403 (access denied)'))
        self.assertTrue(body.closed)

    def test_safe_redirects_do_not_follow_unsafe_or_unconfigured_locations(self):
        locations = ('http://mirror-one.example/search', 'file:///tmp/file', '//127.1/secret', '//outside.example/search')
        for location in locations:
            with self.subTest(location=location):
                self.opener.reset_mock()
                response = FakeResponse(MIRROR + '/search', code=302, headers={'Location': location})
                self.opener.open.return_value = response
                with self.assertRaises(ArchiveError):
                    self.client.open(MIRROR + '/search', self.deadline, archive_mirrors=(MIRROR,))
                self.assertTrue(response.closed)
                self.opener.open.assert_called_once()

    def test_redirect_loop_and_expired_budget(self):
        self.opener.open.return_value = FakeResponse(MIRROR + '/path', code=302, headers={'Location': '/path'})
        with self.assertRaisesRegex(ArchiveError, 'redirect loop'):
            self.client.open(MIRROR + '/path', self.deadline)
        self.opener.open.assert_called_once()
        with self.assertRaisesRegex(ArchiveError, 'time budget exhausted'):
            self.client.open(MIRROR, Deadline(0))
        self.opener.open.assert_called_once()


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.client = HttpClient(self.opener)
        self.deadline = Deadline(20)

    def test_recognised_extension_handles_case_encoding_and_query(self):
        self.assertEqual(url_extension('https://files.example/Great%20Book%2eEPUB?k=.pdf#page=2'), 'EPUB')
        self.assertEqual(url_extension('https://files.example/file.PdF?download=1'), 'PDF')
        self.assertEqual(url_extension('https://files.example/get.php?file=book.pdf'), '')
        self.assertEqual(url_extension('https://files.example/download?a=.epub'), '')
        with FakeResponse(MIRROR + '/files/Book%2eEPUB?k=a', EPUB, content_type='application/epub+zip') as resp:
            self.assertEqual(verified_format(resp, EPUB, ('EPUB',), STRICT), 'EPUB')

    def test_extension_mismatch_policy_and_strict_content_type(self):
        with FakeResponse(MIRROR + '/different.pdf', EPUB, content_type='application/epub+zip') as resp:
            self.assertEqual(verified_format(resp, EPUB, ('EPUB',), DEFAULT), '')
            self.assertEqual(verified_format(resp, EPUB, ('EPUB',), {**DEFAULT, 'url_extension': False}), 'EPUB')
        with FakeResponse(MIRROR + '/get', PDF, content_type=None) as resp:
            self.assertEqual(verified_format(resp, PDF, ('PDF',), STRICT), '')
            self.assertEqual(verified_format(resp, PDF, ('PDF',), DEFAULT), 'PDF')

    def test_download_shaped_urls_and_headers_do_not_make_html_downloadable(self):
        invalid = (
            ('text/html', b'<title>Sign in</title>'),
            ('application/octet-stream', b'<!DOCTYPE html><title>Challenge</title>'),
            ('application/epub+zip', b'<form id="challenge-form"></form>'),
            ('application/octet-stream', b'{"error": "rate limit"}'),
            ('application/octet-stream', b'PK\x03\x04' + b'malicious.zip'),
        )
        for mime, body in invalid:
            with self.subTest(mime=mime, body=body):
                resp = FakeResponse(MIRROR + '/ebook.epub', body, content_type=mime,
                                    headers={'Content-Disposition': 'attachment; filename=ebook.epub'})
                self.assertEqual(verified_format(resp, body, ('EPUB',), DEFAULT), '')

    def test_relative_provider_links_redirects_and_isolated_failures(self):
        responses = {
            'https://libgen.li/ads.php?md5=example': dict(code=403),
            'https://library.lol/main/example': dict(body=b'<a href="../get.php?key=1"><h2>GET</h2></a>'),
            'https://library.lol/get.php?key=1': dict(
                code=302, headers={'Location': 'https://files.example/Great%20Book.EPUB?sig=x'}
            ),
            'https://files.example/Great%20Book.EPUB?sig=x': dict(body=EPUB, code=206, content_type='application/epub+zip'),
            'https://sci-hub.example/10.1234/1234': dict(body=b'<iframe id="pdf" src="../files/book?paper=1#view">'),
            'https://sci-hub.example/files/book?paper=1': dict(body=PDF, content_type='application/octet-stream'),
            'https://z-lib.example/book/one': dict(body=b'<a href="../download?id=3" class="addDownloadedBook">Load</a>'),
            'https://z-lib.example/download?id=3': dict(body=b'{\\rtf1 example}', content_type='application/rtf'),
        }

        def respond(request, timeout):
            url = request.full_url
            if '/get.php?' in url or '/files/' in url or '/download?' in url:
                self.assertEqual(request.get_header('Range'), 'bytes=0-1023')
            return FakeResponse(url, **responses[url])

        self.opener.open.side_effect = respond
        document = parse_html(fixture('downloads.html'))
        downloads = resolve_downloads(self.client, document, RECORD_URL, ('PDF', 'EPUB', 'RTF'), STRICT, self.deadline)
        self.assertEqual(downloads, {
            'EPUB': 'https://files.example/Great%20Book.EPUB?sig=x',
            'PDF': 'https://sci-hub.example/files/book?paper=1',
            'RTF': 'https://z-lib.example/download?id=3',
        })
        self.assertEqual(self.opener.open.call_count, len(responses))

    def test_relative_direct_url_and_extensionless_redirect(self):
        self.opener.open.side_effect = [
            FakeResponse(MIRROR + '/file/download.php?edition=2', code=307, headers={'Location': '/get?token=abc'}),
            FakeResponse(MIRROR + '/get?token=abc', EPUB, content_type='application/octet-stream'),
        ]
        document = parse_html('<div id="md5-panel-downloads"><a download href="../file/download.php?edition=2">File</a></div>')
        self.assertEqual(resolve_downloads(self.client, document, RECORD_URL, ('EPUB',), STRICT, self.deadline),
                         {'EPUB': MIRROR + '/get?token=abc'})

    def test_login_and_bad_provider_markup_leave_no_direct_links(self):
        document = parse_html(fixture('downloads.html'))
        self.opener.open.side_effect = [
            FakeResponse('https://libgen.li/ads.php', code=503),
            FakeResponse('https://library.lol/main/example', b'<a href="/advert">Advertising</a>'),
            FakeResponse('https://sci-hub.example/10.1234/1234', b'<title>Just a moment...</title>'),
            FakeResponse('https://z-lib.example/book/one', b'<form><input type="password"></form>'),
        ]
        self.assertEqual(resolve_downloads(self.client, document, RECORD_URL, ('PDF',), DEFAULT, self.deadline), {})
        self.assertEqual(self.opener.open.call_count, 4)

    def test_calibre_browser_rechecks_file_without_consuming_it(self):
        url = MIRROR + '/download?signature=abc'
        self.opener.open.side_effect = [
            FakeResponse(url, PDF, content_type='application/pdf'),
            FakeResponse(url, b'<title>Expired</title>', content_type='text/html'),
        ]
        browser = DownloadBrowser(self.client, {'PDF': url}, STRICT)
        browser.set_debug_http(True)
        with browser.open(url) as stream:
            self.assertEqual(stream.read(), PDF)
        with self.assertRaisesRegex(ArchiveError, 'File link is no longer available'):
            browser.open(url)

    def test_explicitly_expired_link_refreshes_once(self):
        old = MIRROR + '/download?key=old'
        new = MIRROR + '/download?key=new'
        expired = b'<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>'
        self.opener.open.side_effect = [
            FakeResponse(old, expired, code=403, content_type='application/xml'),
            FakeResponse(new, PDF, content_type='application/pdf'),
        ]
        refresh = Mock(return_value=new)
        browser = DownloadBrowser(self.client, {'PDF': old}, STRICT, refresh=refresh)
        with browser.open(old) as stream:
            self.assertEqual(stream.read(), PDF)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], 'PDF')
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list], [old, new])

        refresh.reset_mock()
        self.opener.open.side_effect = [
            FakeResponse(url, code=403, headers={'X-Amz-Error-Code': 'ExpiredToken'}) for url in (old, new)
        ]
        with self.assertRaisesRegex(ArchiveError, 'expired'):
            browser.open(old)
        refresh.assert_called_once()

    def test_sign_in_and_verification_responses_never_refresh(self):
        url = MIRROR + '/download?key=old'
        responses = (
            FakeResponse(url, b'<title>Sign in</title>', code=403),
            FakeResponse(url, code=403, headers={'CF-Mitigated': 'challenge', 'X-Amz-Error-Code': 'ExpiredToken'}),
        )
        refresh = Mock()
        browser = DownloadBrowser(self.client, {'PDF': url}, STRICT, refresh=refresh)
        for response in responses:
            with self.subTest(response=response):
                self.opener.open.return_value = response
                with self.assertRaises(ArchiveError) as error:
                    browser.open(url)
                self.assertEqual(error.exception.category, 'restriction')
                self.assertTrue(response.closed)
        refresh.assert_not_called()

    def test_file_stream_checks_length_and_record_hash(self):
        url = MIRROR + '/book.pdf'
        content = PDF * 100
        digest = hashlib.md5(content).hexdigest()
        browser = DownloadBrowser(self.client, {'PDF': url}, STRICT, expected_md5=digest)
        self.opener.open.side_effect = [
            FakeResponse(url, data, content_type='application/pdf', headers={'Content-Length': str(len(content))})
            for data in (content, content[:-1], content[:-1] + b'x')
        ]
        with browser.open(url) as stream:
            self.assertEqual(stream.read(0), b'')
            self.assertEqual(stream.read(20) + stream.read(1000) + stream.read(), content)
        with browser.open(url) as stream, self.assertRaisesRegex(ArchiveError, 'length'):
            while stream.read(400):
                pass
        with browser.open(url) as stream, self.assertRaisesRegex(ArchiveError, 'record'):
            stream.read()

    def test_cover_uses_guarded_get_and_rejects_html_or_large_responses(self):
        url = MIRROR + '/covers/book.png'
        jpeg_url = 'https://covers.example/image.jpg'
        jpeg = b'\xff\xd8\xffsynthetic'
        self.opener.open.side_effect = [
            FakeResponse(url, code=302, headers={'Location': jpeg_url}),
            FakeResponse(jpeg_url, jpeg, content_type='image/jpeg'),
            FakeResponse(url, b'<title>Sign in</title>', content_type='image/png'),
            FakeResponse(url, b'\x89PNG\r\n\x1a\n' + b'x' * MAX_COVER_BYTES, content_type='image/png'),
        ]
        self.assertEqual(fetch_cover(self.client, url, self.deadline), jpeg)
        self.assertIsNone(fetch_cover(self.client, url, self.deadline))
        self.assertIsNone(fetch_cover(self.client, url, self.deadline))
