"""Bounded HTTP requests with a shared URL and redirect policy."""

import ipaddress
import re
import socket
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import ExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from math import ceil
from threading import Condition, Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

RESTRICTED_STATUS = {401, 403, 407, 429, 451}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
STATUS_REASONS = {401: 'sign-in required', 403: 'access denied', 429: 'rate limited', 503: 'service unavailable'}
BLOCKED_PATHS = {
    'auth', 'browser_verification', 'captcha', 'cdn-cgi', 'challenge', 'login',
    'register', 'sign-in', 'signin',
}
DOMAIN_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
MAX_PAGE_BYTES = 5 * 1024 * 1024
HOST_CONNECTIONS = 2
EXPIRED_CODES = {'expiredtoken', 'requestexpired', 'signatureexpired'}


class ArchiveError(RuntimeError):
    def __init__(self, category, message, *, retry_after=0):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


def _is_public(address):
    if address.version == 6:
        if address.is_site_local:
            return False
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


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
        if not _is_public(address):
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
        return max(0, float(int(value)))
    except (ValueError, OverflowError):
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


def _public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise socket.gaierror('no address records')
    if any(not _is_public(ipaddress.ip_address(sockaddr[0])) for _, _, _, _, sockaddr in addresses):
        raise ArchiveError('url', 'destination resolves to a non-public IP address')
    return addresses


def _connect_public(address, timeout, source_address=None):
    # Connect to the checked address from this lookup. HTTP Host and TLS SNI still use the hostname.
    end = monotonic() + timeout if timeout is not None else None
    addrs = _public_addresses(*address)
    last_error = None
    for family, kind, protocol, _, destination in addrs:
        remaining = None if end is None else end - monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError()
        try:
            connection = socket.socket(family, kind, protocol)
        except OSError as error:
            last_error = error
            continue
        try:
            connection.settimeout(remaining)
            if source_address:
                connection.bind(source_address)
            connection.connect(destination)
        except OSError as error:
            connection.close()
            last_error = error
        else:
            return connection
    raise last_error


def _public_connection(connection_class, host, **kwargs):
    connection = connection_class(host, **kwargs)
    connection._create_connection = _connect_public
    return connection


def _check_proxy_destination(request):
    target = urlsplit(request.full_url)
    try:
        _public_addresses(target.hostname, target.port or (443 if target.scheme == 'https' else 80))
    except socket.gaierror:
        # A configured mirror can be resolvable only through the system proxy.
        # Extracted cover and download hosts must have a local public DNS answer.
        if not getattr(request, 'configured_mirror', False):
            raise


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        if request.has_proxy():
            _check_proxy_destination(request)
            return super().http_open(request)
        return self.do_open(partial(_public_connection, HTTPConnection), request)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        if request._tunnel_host or request.has_proxy():
            _check_proxy_destination(request)
            return super().https_open(request)
        return self.do_open(partial(_public_connection, HTTPSConnection), request, context=self._context)


def _expired_file(response):
    for header in ('X-Amz-Error-Code', 'X-Ms-Error-Code', 'X-Error-Code'):
        if response.headers.get(header, '').strip().lower() in EXPIRED_CODES:
            return True
    content_type = response.headers.get_content_type()
    if content_type not in {'application/xml', 'text/xml', 'application/json', 'text/plain'}:
        return False
    data = read_body(response, 4096).lower()
    return bool(re.search(
        rb'(?:<code>\s*|"(?:code|error)"\s*:\s*")(?:expiredtoken|requestexpired|signatureexpired)(?:\s*</code>|")'
        rb'|<message>\s*(?:the )?(?:request|signature|token) has expired[.!]?\s*</message>',
        data,
    ))


class _Response:
    def __init__(self, response, cleanup):
        self._response = response
        self._cleanup = cleanup

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        self._cleanup.close()


class _HostConnections:
    def __init__(self, clock):
        self.clock = clock
        self.ready = Condition()
        self.active = {}
        self.retry_until = {}

    def acquire(self, host, deadline, background):
        with self.ready:
            while True:
                now = self.clock()
                self.retry_until = {name: until for name, until in self.retry_until.items() if until > now}
                remaining = self.retry_until.get(host, now) - now
                if remaining:
                    raise ArchiveError('restriction', f'server asked to retry in {ceil(remaining)}s',
                                       retry_after=remaining)
                if not background:
                    return
                if self.active.get(host, 0) < HOST_CONNECTIONS:
                    self.active[host] = self.active.get(host, 0) + 1
                    return partial(self.release, host)
                wait = deadline.remaining()
                if not wait:
                    raise ArchiveError('timeout', 'time budget exhausted waiting for a connection')
                self.ready.wait(wait)

    def release(self, host):
        with self.ready:
            remaining = self.active[host] - 1
            if remaining:
                self.active[host] = remaining
            else:
                self.active.pop(host)
            self.ready.notify_all()

    def pause(self, host, seconds):
        with self.ready:
            self.retry_until[host] = max(self.retry_until.get(host, 0), self.clock() + seconds)
            self.ready.notify_all()


class InFlightLookups:
    """Share work that is in progress and discard its result when it finishes."""

    def __init__(self):
        self.lock = Lock()
        self.pending = {}

    def run(self, key, deadline, load):
        with self.lock:
            previous = self.pending.get(key)
            if previous is None:
                result = self.pending[key] = Future()
        if previous is not None:
            try:
                return previous.result(timeout=deadline.remaining())
            except FutureTimeout:
                raise ArchiveError('timeout', 'time budget exhausted waiting for another lookup') from None
        try:
            value = load()
        except BaseException as error:
            result.set_exception(error)
            raise
        else:
            result.set_result(value)
            return value
        finally:
            with self.lock:
                self.pending.pop(key)


class HttpClient:
    def __init__(self, opener=None, *, clock=monotonic):
        self.opener = opener if opener is not None else build_opener(
            _NoRedirect(), _PublicHTTPHandler(), _PublicHTTPSHandler()
        )
        self.hosts = _HostConnections(clock)

    def open(self, url, deadline, *, byte_range=None, archive_mirrors=(), file_download=False):
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

            current = urlsplit(url)
            with ExitStack() as cleanup:
                release = self.hosts.acquire(current.hostname, deadline, background=not file_download)
                if release:
                    cleanup.callback(release)
                request = Request(url, headers=headers)
                request.configured_mirror = bool(archive_mirrors)
                try:
                    response = self.opener.open(request, timeout=deadline.timeout())
                except HTTPError as error:
                    # With automatic redirects disabled, urllib exposes these as responses.
                    response = error
                except (OSError, HTTPException) as error:
                    raise _network_error(error) from None
                cleanup.callback(response.close)
                status = response.code
                if response.headers.get('CF-Mitigated', '').lower() == 'challenge':
                    raise ArchiveError('restriction', 'browser verification required')
                if status == 200 or (byte_range and status == 206):
                    return _Response(response, cleanup.pop_all())

                if status not in REDIRECT_STATUS:
                    retry_after = _retry_after(response.headers.get('Retry-After'))
                    if retry_after and status in {429, 503}:
                        self.hosts.pause(current.hostname, retry_after)
                    if file_download and status in {400, 401, 403, 410} and _expired_file(response):
                        raise ArchiveError('expired', 'file link has expired')
                    category = 'restriction' if status in RESTRICTED_STATUS else 'http'
                    reason = STATUS_REASONS.get(status)
                    message = f'HTTP {status} ({reason})' if reason else f'HTTP {status}'
                    raise ArchiveError(category, message, retry_after=retry_after)
                # A missing or unsafe Location is an error, even on a 3xx response.
                destination = web_url(response.headers.get('Location'), url)
            if not destination:
                raise ArchiveError('url', 'unsafe or missing redirect destination')
            target = urlsplit(destination)
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
