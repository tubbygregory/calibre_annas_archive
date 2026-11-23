import io
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, call, patch
from urllib.request import ProxyHandler, Request, build_opener

from tests.support import BACKUP, MIRROR, FakeResponse

from calibre_plugins.store_annas_archive.http_client import (
    ArchiveError, Deadline, HttpClient, InFlightLookups,
    _NoRedirect, _PublicHTTPHandler, _check_proxy_destination,
)


def address_records(value, port):
    family = socket.AF_INET6 if ':' in value else socket.AF_INET
    sockaddr = (value, port, 0, 0) if family == socket.AF_INET6 else (value, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr)]


class NetworkPolicyTest(unittest.TestCase):
    def test_direct_redirect_resolves_every_hop_and_connects_to_the_checked_address(self):
        opener = build_opener(ProxyHandler({}), _PublicHTTPHandler(), _NoRedirect())
        client = HttpClient(opener)
        wire = io.BytesIO(
            b'HTTP/1.1 302 Found\r\nLocation: http://other.example/cover.png\r\nContent-Length: 0\r\n\r\n'
        )
        with patch('socket.getaddrinfo') as dns, patch('socket.socket') as sockets:
            dns.side_effect = [address_records('93.184.216.34', 80), address_records('192.168.1.2', 80)]
            sockets.return_value.makefile.return_value = wire
            with self.assertRaisesRegex(ArchiveError, 'non-public IP address'):
                client.open('http://cover.example/cover.png', Deadline(5))
        self.assertEqual(dns.call_args_list, [
            call('cover.example', 80, type=socket.SOCK_STREAM), call('other.example', 80, type=socket.SOCK_STREAM),
        ])
        sockets.assert_called_once()
        sockets.return_value.connect.assert_called_once_with(('93.184.216.34', 80))
        self.assertIn(b'Host: cover.example', sockets.return_value.sendall.call_args.args[0])

    def test_proxy_preflight_rejects_private_and_mapped_addresses(self):
        request = Request(MIRROR + '/cover.png')
        with patch('socket.getaddrinfo') as dns:
            for private in ('127.0.0.1', '10.23.45.67', '::ffff:10.0.0.1', '::1', 'fec0::1'):
                with self.subTest(private=private):
                    dns.return_value = address_records(private, 443)
                    with self.assertRaisesRegex(ArchiveError, 'non-public IP address'):
                        _check_proxy_destination(request)
            dns.return_value = address_records('93.184.216.34', 443) + address_records('127.0.0.1', 443)
            with self.assertRaisesRegex(ArchiveError, 'non-public IP address'):
                _check_proxy_destination(request)
            dns.side_effect = socket.gaierror()
            with self.assertRaises(socket.gaierror):
                _check_proxy_destination(request)
            request.configured_mirror = True
            _check_proxy_destination(request)

    def test_connection_slots_stay_occupied_until_background_responses_close(self):
        opener = Mock()
        client = HttpClient(opener)
        opener.open.side_effect = [FakeResponse(MIRROR) for _ in range(4)]
        with client.open(MIRROR, Deadline(2)), client.open(MIRROR, Deadline(2)):
            with self.assertRaisesRegex(ArchiveError, 'waiting for a connection'):
                client.open(MIRROR, Deadline(0.02))
            self.assertEqual(opener.open.call_count, 2)
            with client.open(MIRROR, Deadline(2), file_download=True):
                self.assertEqual(opener.open.call_count, 3)
        with client.open(MIRROR, Deadline(2)):
            self.assertEqual(opener.open.call_count, 4)

    def test_provider_retry_after_is_shared_across_urls_until_it_expires(self):
        now = 100
        opener = Mock()
        client = HttpClient(opener, clock=lambda: now)
        opener.open.side_effect = [
            FakeResponse(MIRROR, code=429, headers={'Retry-After': '90'}),
            FakeResponse(BACKUP),
            FakeResponse(MIRROR),
        ]
        with self.assertRaisesRegex(ArchiveError, 'rate limited'):
            client.open(MIRROR + '/file-a', Deadline(2))
        now += 30
        with self.assertRaisesRegex(ArchiveError, 'retry in 60s'):
            client.open(MIRROR + '/file-b', Deadline(2), file_download=True)
        self.assertEqual(opener.open.call_count, 1)
        with client.open(BACKUP, Deadline(2)):
            self.assertEqual(opener.open.call_count, 2)
        now += 60
        with client.open(MIRROR + '/file-b', Deadline(2)):
            self.assertEqual(opener.open.call_count, 3)


class InFlightTest(unittest.TestCase):
    def test_duplicate_waiters_get_one_result_and_subsequent_requests_run_again(self):
        lookup = InFlightLookups()
        loading = Event()
        waiting = Event()
        complete = Event()

        def load():
            loading.set()
            if not complete.wait(5):
                raise AssertionError('lookup did not start waiting')
            return 'first result'

        def remaining():
            waiting.set()
            return 5

        duplicate = Mock(return_value='second result')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(lookup.run, 'record', Deadline(5), load)
            self.assertTrue(loading.wait(5))
            second = pool.submit(lookup.run, 'record', Mock(remaining=remaining), duplicate)
            self.assertTrue(waiting.wait(5))
            complete.set()
            self.assertEqual(first.result(5), 'first result')
            self.assertEqual(second.result(5), 'first result')
        duplicate.assert_not_called()
        self.assertEqual(lookup.run('record', Deadline(5), duplicate), 'second result')
