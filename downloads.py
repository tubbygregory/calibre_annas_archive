"""Resolve and verify public ebook links from an Archive record."""

import hashlib
import logging
import re
from urllib.parse import unquote, urlsplit

from .constants import FORMATS
from .http_client import ArchiveError, Deadline, origin, read_body, web_url
from .parsing import download_panel, read_html, text

logger = logging.getLogger(__name__)
SAMPLE_SIZE = 1024
HTML_TYPES = {'text/html', 'application/xhtml+xml'}
BINARY_TYPES = {'application/octet-stream', 'binary/octet-stream', 'application/force-download'}
FORMAT_TYPES = {
    'EPUB': {'application/epub+zip', 'application/zip'},
    'PDF': {'application/pdf', 'application/x-pdf'},
    'MOBI': {'application/x-mobipocket-ebook', 'application/vnd.amazon.ebook'},
    'AZW': {'application/x-mobipocket-ebook', 'application/vnd.amazon.ebook'},
    'AZW3': {'application/x-mobipocket-ebook', 'application/vnd.amazon.ebook'},
    'CBR': {'application/vnd.comicbook-rar', 'application/x-cbr', 'application/x-rar-compressed', 'application/vnd.rar'},
    'CBZ': {'application/vnd.comicbook+zip', 'application/x-cbz', 'application/zip'},
    'FB2': {'application/x-fictionbook+xml', 'application/fb2+xml', 'application/xml', 'text/xml'},
    'DJVU': {'image/vnd.djvu', 'image/x-djvu'},
    'RTF': {'application/rtf', 'text/rtf', 'application/x-rtf'},
    'TXT': {'text/plain'},
}
ARCHIVE_BROWSER_PATHS = {
    'account', 'b', 'book', 'browser_verification', 'donate', 'dyn', 'fast_download', 'fast_download_no_more',
    'fast_download_not_member', 'ipfs_downloads', 'login', 'member', 'refer', 'scidb', 'slow_download',
    'torrents', 'view',
}
MAX_SOURCES = 8
MAX_PROVIDER_LINKS = 3


def url_extension(url):
    """Get a recognised ebook extension from the URL path, independent of its query."""
    try:
        return _filename_extension(unquote(urlsplit(url).path.rsplit('/', 1)[-1]))
    except ValueError:
        return ''


def _filename_extension(filename):
    extension = filename.rpartition('.')[2].upper() if '.' in filename else ''
    return extension if extension in FORMATS else ''


def _signatures(prefix, media_type):
    data = prefix.lstrip(b'\xef\xbb\xbf \t\r\n')
    if (not data or data.startswith(b'<!--')
            or re.match(rb'<(?:!doctype\s+html|html|head|body|title|script|form|meta)\b', data, re.I)
            or data.startswith((b'{', b'[')) and not data.startswith(b'{\\rtf')):
        return ()
    if data.startswith(b'%PDF-'):
        return ('PDF',)
    if data.startswith(b'PK\x03\x04'):
        return ('EPUB',) if b'mimetypeapplication/epub+zip' in data else ('CBZ',)
    if prefix[60:68] in (b'BOOKMOBI', b'TEXtREAd'):
        return ('MOBI', 'AZW', 'AZW3')
    if data.startswith(b'Rar!\x1a\x07'):
        return ('CBR',)
    if data.startswith(b'AT&TFORM'):
        return ('DJVU',)
    if re.match(rb'(?:<\?xml[^>]*>\s*)?<FictionBook\b', data, re.I):
        return ('FB2',)
    if data.startswith(b'{\\rtf'):
        return ('RTF',)
    if media_type == 'text/plain' and not data.startswith(b'<') and b'\x00' not in data:
        return ('TXT',)
    return ()


def verified_format(response, prefix, formats, options):
    media = response.headers.get_content_type() if response.headers.get('Content-Type') else ''
    if media in HTML_TYPES or media.endswith(('/json', '+json', '/javascript')):
        return ''
    extension = url_extension(response.geturl())
    filename = _filename_extension(response.headers.get_filename() or '')
    signatures = _signatures(prefix, media)
    if options['url_extension'] and extension and extension not in formats:
        return ''
    for fmt in (filename, extension, *formats):
        if fmt not in formats or fmt not in signatures:
            continue
        if media and media not in BINARY_TYPES and media not in FORMAT_TYPES[fmt]:
            if options['content_type'] or media.startswith(('text/', 'image/', 'audio/', 'video/')):
                continue
        if options['content_type'] and not media:
            continue
        # Several Kindle formats share the Mobipocket header.
        if len(signatures) > 1 and fmt not in (filename, extension) and media not in FORMAT_TYPES[fmt]:
            continue
        return fmt
    return ''


def _provider(url, label):
    host = urlsplit(url).hostname
    labels = set(host.split('.'))
    if 'libgen' in labels or host == 'library.lol' or label.startswith(('libgen.li', 'libgen.rs')):
        return 'libgen'
    if 'sci-hub' in labels or label.startswith('sci-hub'):
        return 'scihub'
    if labels & {'z-lib', 'zlibrary', 'singlelogin'} or label == 'z-library':
        return 'zlib'
    return ''


def _provider_targets(document, provider):
    if provider == 'libgen':
        return [a.get('href') for a in document.xpath('//a[@href]') if text(a).casefold() == 'get']
    if provider == 'scihub':
        return document.xpath(
            '//embed[@id="pdf" or @type="application/pdf"]/@src'
            ' | //iframe[@id="pdf" or @type="application/pdf"]/@src'
            ' | //object[@type="application/pdf"]/@data'
            ' | //a[@id="pdf"]/@href'
        )
    return document.xpath(
        '//a[contains(concat(" ", normalize-space(@class), " "), " addDownloadedBook ") or @download]/@href'
    )


def _probe(client, url, formats, options, deadline):
    with client.open(url, deadline, byte_range=f'bytes=0-{SAMPLE_SIZE - 1}') as response:
        prefix = read_body(response, SAMPLE_SIZE)
        fmt = verified_format(response, prefix, formats, options)
        return (fmt, response.geturl()) if fmt else None


def _resolve(client, url, provider, formats, options, deadline):
    if not provider or url_extension(url):
        return _probe(client, url, formats, options, deadline)
    with client.open(url, deadline) as response:
        prefix = read_body(response, SAMPLE_SIZE)
        fmt = verified_format(response, prefix, formats, options)
        destination = response.geturl()
        if fmt:
            return fmt, destination
        if response.headers.get_content_type() not in HTML_TYPES:
            return None
        document = read_html(response, prefix=prefix)
    if document.xpath('//input[@type="password"]'):
        raise ArchiveError('restriction', 'source requires sign-in')

    targets = dict.fromkeys(web_url(href, destination) for href in _provider_targets(document, provider))
    targets.pop('', None)
    for target in list(targets)[:MAX_PROVIDER_LINKS]:
        if destination.startswith('https:') and target.startswith('http:'):
            continue
        try:
            verified = _probe(client, target, formats, options, deadline)
        except ArchiveError as error:
            logger.debug('Anna’s Archive provider link [%s]: %s', error.category, error)
            continue
        if verified:
            return verified
    return None


def resolve_downloads(client, document, page_url, formats, options, deadline):
    panel = download_panel(document)
    links = panel.xpath(
        './/a[@href and (@download or contains(concat(" ", normalize-space(@class), " "), " js-download-link "))]'
    )
    downloads = {}
    visited = set()
    for anchor in links:
        url = web_url(anchor.get('href'), page_url)
        if not url or url in visited or (page_url.startswith('https:') and url.startswith('http:')):
            continue
        label = text(anchor).casefold()
        provider = _provider(url, label)
        path = unquote(urlsplit(url).path).lower().strip('/').split('/', 1)[0]
        same_site = origin(url) == origin(page_url)
        if (same_site and path in ARCHIVE_BROWSER_PATHS) or label.startswith(('partner server', 'bulk torrent')):
            continue
        direct = 'download' in anchor.attrib or label in {'download', 'direct download', 'download file'}
        if not (provider or url_extension(url) or direct or same_site):
            continue
        if not deadline.remaining() or len(visited) >= MAX_SOURCES:
            break
        visited.add(url)
        try:
            verified = _resolve(client, url, provider, formats, options, deadline)
        except ArchiveError as error:
            logger.debug('Anna’s Archive download [%s] %s: %s', error.category, urlsplit(url).hostname, error)
            continue
        if verified:
            fmt, destination = verified
            # Calibre uses the dictionary key as the imported file extension.
            downloads.setdefault(fmt, destination)
            if len(downloads) == len(formats):
                break
    return downloads


class _VerifiedFile:
    """Replay the sample and verify the complete file as Calibre copies it."""

    def __init__(self, response, prefix, expected_md5):
        self.response = response
        self.prefix = prefix
        self.expected_md5 = expected_md5
        self.total = 0
        self.digest = None
        if expected_md5:
            try:
                self.digest = hashlib.md5(usedforsecurity=False)
            except TypeError:
                # Python 3.8 in Calibre 5 predates the usedforsecurity argument.
                self.digest = hashlib.md5()
        length = response.headers.get('Content-Length')
        if length is not None and not re.fullmatch(r'[0-9]{1,20}', length.strip()):
            raise ArchiveError('integrity', 'download returned an invalid Content-Length')
        self.length = int(length) if length is not None else None

    def read(self, size=-1):
        if size == 0:
            return b''
        if size is None or size < 0:
            data = self.prefix + read_body(self.response, -1)
            self.prefix = b''
        else:
            data, self.prefix = self.prefix[:size], self.prefix[size:]
            if len(data) < size:
                data += read_body(self.response, size - len(data))
        self.total += len(data)
        if self.digest:
            self.digest.update(data)
        if not data or size is None or size < 0 or self.total == self.length:
            if self.length is not None and self.total != self.length:
                raise ArchiveError('integrity', 'download is incomplete or has an unexpected length')
            if self.digest and self.digest.hexdigest() != self.expected_md5:
                raise ArchiveError('integrity', 'download does not match the Archive record')
        elif self.length is not None and self.total > self.length:
            raise ArchiveError('integrity', 'download exceeds the advertised length')
        return data

    def info(self):
        return self.response.headers

    def geturl(self):
        return self.response.geturl()

    def close(self):
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()


class DownloadBrowser:
    """The browser methods Calibre uses when downloading a search result."""

    def __init__(self, client, downloads, options, *, refresh=None, expected_md5=None):
        self.client = client
        self.formats = {url: fmt for fmt, url in downloads.items()}
        self.options = options
        self.refresh = refresh
        self.expected_md5 = expected_md5

    def set_debug_http(self, enabled):
        # Calibre enables raw HTTP debugging on its browser. URLs may be signed.
        pass

    def _open_file(self, url, formats, deadline):
        response = self.client.open(url, deadline, file_download=True)
        try:
            prefix = read_body(response, SAMPLE_SIZE)
            if not verified_format(response, prefix, formats, self.options):
                raise ArchiveError('restriction', 'File link is no longer available. Open the record in your browser.')
            return _VerifiedFile(response, prefix, self.expected_md5)
        except Exception:
            response.close()
            raise

    def open(self, url):
        fmt = self.formats.get(url)
        formats = (fmt,) if fmt else FORMATS
        deadline = Deadline(30)
        try:
            return self._open_file(url, formats, deadline)
        except ArchiveError as error:
            if error.category != 'expired' or not self.refresh or not fmt:
                raise
        try:
            replacement = self.refresh(fmt, deadline)
        except ArchiveError:
            replacement = None
        if not replacement:
            raise ArchiveError('expired', 'File link has expired. Open the record in your browser.')
        return self._open_file(replacement, formats, deadline)
