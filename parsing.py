"""Parse Anna's Archive search and record HTML without executing page scripts."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from lxml import etree, html

from .constants import FORMATS
from .http_client import MAX_PAGE_BYTES, ArchiveError, origin, read_body, web_url

MD5 = re.compile(r'[0-9a-fA-F]{32}')
FIELDS = {
    'title': {'title', 'book title', 'name'},
    'author': {'author', 'authors', 'creator'},
    'formats': {'ext', 'extension', 'file extension', 'format', 'file format', 'formats', 'filetype'},
}
NEXT_LINKS = (
    '//a[@href and (contains(concat(" ", normalize-space(@rel), " "), " next ")'
    ' or contains(@class, "js-pagination-next-page") or ancestor::nav[@aria-label="Pagination"])]'
)


@dataclass(frozen=True)
class Book:
    path: str
    title: str
    author: str
    formats: tuple
    cover: str
    mirror: str


@dataclass(frozen=True)
class SearchPage:
    books: tuple
    next_page: Optional[int] = None


def text(element):
    return ' '.join(element.text_content().split()) if element is not None else ''


def parse_html(data, encoding=None):
    try:
        document = html.fromstring(data, parser=html.HTMLParser(encoding=encoding, no_network=True))
    except (ValueError, LookupError, etree.LxmlError):
        raise ArchiveError('parse', 'invalid or empty HTML') from None
    headings = ' '.join(document.xpath('//title/text() | //h1/text()')).casefold()
    blocked = (
        'just a moment', 'attention required', 'verify you are human', 'access denied',
        'checking your browser', 'browser verification', 'ddos-guard', 'captcha',
    )
    if (any(phrase in headings for phrase in blocked) or
            document.xpath('//*[@id="challenge-form" or @id="cf-challenge-running" or @id="captcha-form"]')):
        raise ArchiveError('restriction', 'browser verification or access approval required')
    return document


def read_html(response, *, prefix=b''):
    content_type = response.headers.get_content_type()
    if content_type not in ('text/html', 'application/xhtml+xml'):
        raise ArchiveError('parse', 'expected an HTML page')
    data = prefix + read_body(response, MAX_PAGE_BYTES + 1 - len(prefix))
    if len(data) > MAX_PAGE_BYTES:
        raise ArchiveError('parse', 'HTML page exceeds the size limit')
    return parse_html(data, response.headers.get_content_charset())


def record_path(value):
    """Canonicalise a supported Archive record path or a legacy bare MD5."""
    if not isinstance(value, str):
        return ''
    if MD5.fullmatch(value):
        return f'/md5/{value.lower()}'
    try:
        path = unquote(urlsplit(value).path).strip('/')
    except ValueError:
        return ''
    kind, _, identifier = path.partition('/')
    if kind == 'md5' and MD5.fullmatch(identifier):
        return f'/md5/{identifier.lower()}'
    if kind in {'ia', 'nexusstc_download'} and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}', identifier):
        return f'/{kind}/{identifier}'
    if (kind == 'doi' and re.fullmatch(r'10\.\d{4,9}/[^\s?#\\\x00-\x1f\x7f]{1,400}', identifier)
            and not set(identifier.split('/')) & {'.', '..', ''}):
        return '/doi/' + quote(identifier, safe='/')
    return ''


def normalise_formats(value):
    if not isinstance(value, str):
        return ()
    items = re.split(r'[,/;·|\s]+', value)
    return tuple(dict.fromkeys(
        item.lstrip('.').upper() for item in items if item.lstrip('.').upper() in FORMATS
    ))


def _record_link(anchor, base):
    url = web_url(anchor.get('href'), base)
    return record_path(url) if url and origin(url) == origin(base) else ''


def _primary_text(node):
    # In the site's table, nested block spans are alternate metadata.
    return ' '.join(''.join(node.xpath(
        './/text()[not(ancestor::span[contains(concat(" ", normalize-space(@class), " "), " block ")])]'
    )).split())


def _field(value):
    name = ' '.join(value.casefold().replace('_', ' ').replace('-', ' ').split())
    return next((field for field, labels in FIELDS.items() if name in labels), None)


def _table_books(table, base):
    headers = {}
    for row in table.xpath('./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr'):
        heading_cells = row.xpath('./th')
        cells = row.xpath('./td')
        if heading_cells and not cells:
            headers = {}
            for index, cell in enumerate(heading_cells):
                kind = _field(cell.get('data-field', '') or text(cell))
                if kind:
                    headers[index] = kind
                    if cell.get('id'):
                        headers[cell.get('id')] = kind
            continue

        fields = {}
        for index, cell in enumerate(cells):
            kind = (_field(cell.get('data-label', '') or cell.get('data-field', ''))
                    or headers.get(cell.get('headers')) or headers.get(index))
            if kind:
                fields[kind] = index
        links = []
        for index, cell in enumerate(cells):
            for anchor in cell.xpath('.//a[@href]'):
                path = _record_link(anchor, base)
                if path:
                    links.append((index, anchor, path))
        if not links:
            continue

        title_index = fields.get('title')
        if title_index is None and len(cells) == 12 and not _primary_text(cells[0]):
            title_index = 1
        if title_index is not None:
            title = _primary_text(cells[title_index])
            path = next((path for index, _, path in links if index == title_index), links[0][2])
        else:
            title_link = next(((index, _primary_text(anchor), path) for index, anchor, path in links
                               if _primary_text(anchor)), None)
            if title_link is None:
                continue
            title_index, title, path = title_link
        if not title or MD5.fullmatch(title):
            continue

        author_index = fields.get('author')
        # The legacy headerless table has a cover followed by title and author.
        # Leave the author blank if an unlabelled row no longer matches this layout.
        if author_index is None and not headers and (len(cells), title_index) in {(12, 1), (11, 0)}:
            author_index = title_index + 1
        author = _primary_text(cells[author_index]) if author_index is not None else ''
        formats = ()
        if 'formats' in fields:
            formats = normalise_formats(_primary_text(cells[fields['formats']]))
        else:
            for index in reversed(range(title_index + 1, len(cells))):
                if index == author_index:
                    continue
                label = _primary_text(cells[index])
                parts = list(filter(None, re.split(r'[,/;·|\s]+', label)))
                if parts and all(part.lstrip('.').upper() in FORMATS for part in parts):
                    formats = normalise_formats(label)
                    break

        images = row.xpath('.//img')
        cover = _cover(images, base)
        yield Book(path, title, author, formats, cover, origin(base))


def _cover(images, base):
    for image in images:
        cover = web_url(image.get('data-src') or image.get('src'), base)
        if cover and (urlsplit(base).scheme != 'https' or urlsplit(cover).scheme == 'https'):
            return cover
    return ''


def _card_books(root, base):
    for anchor in root.xpath('.//a[contains(concat(" ", normalize-space(@class), " "), " js-vim-focus ")]'):
        path = _record_link(anchor, base)
        if not path:
            continue
        headings = anchor.xpath('.//h2 | .//h3 | .//h4')
        title = text(headings[0]) if headings else text(anchor)
        if not title:
            continue
        row = anchor.getparent()
        authors = row.xpath('.//*[@data-field="author"] | .//a[.//span[contains(@class,"mdi--user-edit")]]'
                            ' | .//div[contains(concat(" ", normalize-space(@class), " "), " italic ")]')
        meta = row.xpath('.//*[@data-field="format"] | .//div[contains(@class, "text-gray-500")]'
                         ' | .//div[contains(@class, "font-semibold")]')
        formats = ()
        for node in meta:
            tokens = [part.strip() for part in re.split(r'[,·]', text(node))]
            formats = next((normalise_formats(part) for part in tokens if normalise_formats(part)), ())
            if formats:
                break
        yield Book(path, title, text(authors[0]) if authors else '', formats,
                   _cover(row.xpath('.//img'), base), origin(base))


def _next_page(document, url, current):
    pages = set()
    for link in document.xpath(NEXT_LINKS):
        target = web_url(link.get('href'), url)
        parts = urlsplit(target)
        if not target or origin(target) != origin(url) or parts.path.rstrip('/') != '/search':
            continue
        number = parse_qs(parts.query).get('page', [''])[0]
        if re.fullmatch(r'[0-9]{1,6}', number) and int(number) > current:
            pages.add(int(number))
    return min(pages) if pages else None


def parse_search(document, url, page):
    parts = urlsplit(url)
    served_page = parse_qs(parts.query).get('page', ['1'])[0]
    if parts.path.rstrip('/') != '/search' or served_page != str(page):
        raise ArchiveError('parse', 'redirect did not return the requested search page')
    page_numbers = document.xpath('//nav[@aria-label="Pagination"]//a[@aria-current="page"]/text()')
    if page_numbers and all(number.strip() != str(page) for number in page_numbers):
        raise ArchiveError('parse', 'search returned a different page')

    roots = document.xpath('//*[@id="aarecord-list" or @id="md5-list"]')
    root = roots[0] if roots else document
    forms = document.xpath('//form[.//input[@name="q"] and '
                           '(contains(@class, "js-search-form") or @role="search" or @action="/search")]')
    books = tuple(book for table in root.xpath('.//table') for book in _table_books(table, url))
    if not books:
        books = tuple(_card_books(root, url))
    title = ' '.join(document.xpath('//title/text()')).casefold().replace('’', "'")
    no_matches = document.xpath('//*[contains(@class, "js-not-found-additional")]') or any(
        phrase in text(root).casefold() for phrase in ('no results found', 'no matching results', 'no files found')
    )
    if not forms or not (roots or (("anna's archive" in title) and (books or no_matches))):
        raise ArchiveError('parse', 'HTML did not match an Anna’s Archive search page')
    return SearchPage(books, _next_page(document, url, page))


def download_panel(document):
    panels = document.xpath('//*[@id="md5-panel-downloads" or @id="download-links"]')
    if not panels:
        if document.xpath('//input[@type="password"]'):
            raise ArchiveError('restriction', 'sign-in required')
        raise ArchiveError('parse', 'record page has no recognised download panel')
    return panels[0]
