"""Bounded HTTP requests with a shared URL and redirect policy."""

import ipaddress
import re
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RESTRICTED_STATUS = {401, 403, 407, 429, 451}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
BLOCKED_PATHS = {
    'auth', 'browser_verification', 'captcha', 'cdn-cgi', 'challenge', 'login',
    'register', 'sign-in', 'signin',
}
DOMAIN_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
MAX_PAGE_BYTES = 5 * 1024 * 1024


class ArchiveError(RuntimeError):
    def __init__(self, category, message, *, retry_after=0):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


def web_url(value, base=''):
    """Resolve and validate a public HTTP(S) URL. Return an empty string on failure."""
    if not isinstance(value, str):
        return ''
    value = value.strip(' ')
    if not value or re.search(r'[\x00-\x1f\x7f\\]', value):
        return ''
    try:
        parts = urlsplit(urljoin(base, value))
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username is not None:
            return ''
        host = parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port
    except (UnicodeError, ValueError):
        return ''
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(labels) < 2 or
                labels[-1] in {'internal', 'local', 'localhost', 'onion', 'home', 'lan'} or
                labels[-1].isdigit() or any(not DOMAIN_LABEL.fullmatch(label) for label in labels)):
            return ''
        # Some clients accept legacy hexadecimal or shorthand IPv4 literals.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            return ''
    else:
        if not address.is_global or address.is_multicast:
            return ''
        host = address.compressed
        if address.version == 6:
            host = f'[{host}]'
    if port == 0 or (':' in parts.netloc and parts.netloc.endswith(':')):
        return ''
    default_port = 443 if parts.scheme == 'https' else 80
    netloc = f'{host}:{port}' if port not in (None, default_port) else host
    path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    return urlunsplit((parts.scheme, netloc, path, query, ''))


def origin(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def mirror_url(value):
    if not isinstance(value, str) or '#' in value or '?' in value:
        return ''
    url = web_url(value)
    return origin(url) if url and urlsplit(url).path == '/' else ''


def _restricted_url(url, archive):
    parts = urlsplit(url)
    return (
        bool(set(unquote(parts.path).lower().split('/')) & BLOCKED_PATHS)
        or (archive and parse_qs(parts.query).get('check') == ['1'])
    )


def _retry_after(value):
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, OverflowError):
            return 0


def _network_error(error):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError):
        return ArchiveError('timeout', 'request timed out')
    if isinstance(reason, socket.gaierror):
        return ArchiveError('network', 'DNS lookup failed')
    return ArchiveError('network', f'connection failed ({type(reason).__name__})')


class Deadline:
    def __init__(self, seconds, *, clock=monotonic):
        self.clock = clock
        self.end = clock() + max(0, seconds)

    def remaining(self):
        return max(0, self.end - self.clock())

    def timeout(self):
        remaining = self.remaining()
        if not remaining:
            raise ArchiveError('timeout', 'time budget exhausted')
        # Leave time for another mirror if one connection stalls.
        return min(8, remaining)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class HttpClient:
    def __init__(self, opener=None):
        self.opener = opener if opener is not None else build_opener(_NoRedirect())

    def open(self, url, deadline, *, byte_range=None, archive_mirrors=()):
        url = web_url(url)
        if not url:
            raise ArchiveError('url', 'invalid HTTP URL')
        allowed = set(archive_mirrors) | {origin(url)}
        visited = set()
        for _ in range(5):
            if url in visited:
                raise ArchiveError('http', 'redirect loop')
            visited.add(url)
            if _restricted_url(url, bool(archive_mirrors)):
                raise ArchiveError('restriction', 'browser verification or sign-in required')
            headers = {'User-Agent': 'Calibre-Annas-Archive', 'Accept-Encoding': 'identity'}
            if byte_range:
                headers['Range'] = byte_range
            try:
                response = self.opener.open(Request(url, headers=headers), timeout=deadline.timeout())
            except HTTPError as error:
                # With automatic redirects disabled, urllib exposes these as responses.
                response = error
            except (OSError, HTTPException) as error:
                raise _network_error(error) from None
            status = response.code
            if response.headers.get('CF-Mitigated', '').lower() == 'challenge':
                response.close()
                raise ArchiveError('restriction', 'browser verification required')
            if status == 200 or (byte_range and status == 206):
                return response

            with response:
                if status not in REDIRECT_STATUS:
                    category = 'restriction' if status in RESTRICTED_STATUS else 'http'
                    raise ArchiveError(
                        category, f'HTTP {status}', retry_after=_retry_after(response.headers.get('Retry-After'))
                    )
                # A missing or unsafe Location is an error, even on a 3xx response.
                destination = web_url(response.headers.get('Location'), url)
            if not destination:
                raise ArchiveError('url', 'unsafe or missing redirect destination')
            current, target = urlsplit(url), urlsplit(destination)
            if current.scheme == 'https' and target.scheme != 'https':
                raise ArchiveError('url', 'HTTPS redirect downgraded to HTTP')
            local_upgrade = (
                current.scheme == 'http' and target.scheme == 'https' and current.netloc == target.netloc
            )
            if archive_mirrors and origin(destination) not in allowed and not local_upgrade:
                raise ArchiveError('restriction', 'redirected outside the configured mirrors')
            url = destination
        raise ArchiveError('http', 'too many redirects')


def read_body(response, limit):
    try:
        return response.read(limit)
    except (OSError, HTTPException) as error:
        raise _network_error(error) from None
