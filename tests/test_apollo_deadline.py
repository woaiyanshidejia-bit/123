"""Loopback-only deadline contracts; no Apollo network or real credential use."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from yingba.apollo import (ApolloClient, ApolloPause, ApolloUnknown, HOST,
                          build_accounts_request, build_search_request)
from tests.test_apollo_transport import response as organization_response
from tests.test_apollo_free import accounts_response


class ApolloDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.mode = 'fast'
        self.calls = []
        self.stop = threading.Event()
        self.reply = organization_response()
        self.timeout = .18
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                case.calls.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.dumps(case.reply).encode()
                try:
                    if case.mode == 'header_drip':
                        header = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                  b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n')
                        for byte in header:
                            self.connection.sendall(bytes([byte]))
                            if case.stop.wait(.015):
                                return
                        self.connection.sendall(body)
                    else:
                        # HTTP/1.0 transfers the socket to HTTPResponse and
                        # clears conn.sock. The watchdog must retain its handle.
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        if case.mode == 'body_drip':
                            for byte in body:
                                self.wfile.write(bytes([byte]))
                                self.wfile.flush()
                                if case.stop.wait(.015):
                                    return
                        else:
                            self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.original_getaddrinfo = socket.getaddrinfo

        def connection(host, *, timeout, context):
            self.assertEqual(host, HOST)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            return http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=timeout)

        self.connection_patch = patch('yingba.apollo.http.client.HTTPSConnection', side_effect=connection)
        self.connection_patch.start()
        self.client = ApolloClient('synthetic-deadline-fixture-key', enabled=True, timeout=self.timeout)
        self.request = build_search_request(1, 2, {'q_organization_name': 'fixture'})

    def tearDown(self):
        self.stop.set()
        self.connection_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def assert_bounded_paid_failure(self):
        start = time.monotonic()
        with self.assertRaises(ApolloUnknown) as caught:
            self.client.search(self.request, authorized=True)
        elapsed = time.monotonic() - start
        self.assertTrue(caught.exception.billing_unknown)
        self.assertNotIn('synthetic-deadline-fixture-key', str(caught.exception))
        self.assertLess(elapsed, .85, 'inactivity timeout must not replace total deadline')
        return elapsed

    def test_header_drip_cannot_extend_the_absolute_request_deadline(self):
        self.mode = 'header_drip'
        self.assert_bounded_paid_failure()
        self.assertEqual(len(self.calls), 1)

    def test_body_drip_cannot_extend_the_absolute_request_deadline(self):
        self.mode = 'body_drip'
        self.assert_bounded_paid_failure()
        self.assertEqual(len(self.calls), 1)

    def test_free_read_deadline_pauses_without_paid_uncertainty(self):
        self.mode = 'body_drip'
        self.reply = accounts_response()
        start = time.monotonic()
        with self.assertRaises(ApolloPause) as caught:
            self.client.search_accounts(build_accounts_request(1, 2))
        self.assertLess(time.monotonic() - start, .85)
        self.assertFalse(caught.exception.billing_unknown)
        self.assertEqual(len(self.calls), 1)

    def test_blocked_dns_returns_on_deadline_and_cannot_send_after_release(self):
        release, finished = threading.Event(), threading.Event()
        workers = []
        records = self.original_getaddrinfo('127.0.0.1', self.server.server_port, type=socket.SOCK_STREAM)

        def delayed_dns(*args, **kwargs):
            workers.append(threading.current_thread())
            release.wait(2)
            finished.set()
            return records

        try:
            with patch('yingba.apollo.socket.getaddrinfo', side_effect=delayed_dns):
                self.assert_bounded_paid_failure()
                self.assertEqual(self.calls, [])
                self.assertEqual(len(workers), 1)
                self.assertTrue(workers[0].daemon)
                release.set()
                self.assertTrue(finished.wait(.5))
                workers[0].join(timeout=.5)
                self.assertEqual(self.calls, [])
        finally:
            release.set()

    def test_dns_returning_after_deadline_cannot_start_tcp_or_send(self):
        records = self.original_getaddrinfo('127.0.0.1', self.server.server_port, type=socket.SOCK_STREAM)

        def late_resolver(*args):
            self.stop.wait(self.timeout + .06)
            return records

        with patch('yingba.public_web.PublicFetcher.resolve', side_effect=late_resolver):
            with patch('yingba.apollo.socket.socket', wraps=socket.socket) as sockets:
                self.assert_bounded_paid_failure()
                self.assertFalse(sockets.called)
        self.assertEqual(self.calls, [])

    def test_fast_response_uses_one_bounded_dns_lookup_and_retains_results(self):
        with patch('yingba.apollo.socket.getaddrinfo', wraps=self.original_getaddrinfo) as dns:
            page = self.client.search(self.request, authorized=True)
        self.assertEqual(dns.call_count, 1, 'numeric TCP connect must not resolve again')
        self.assertEqual(len(page.records), 2)
        self.assertIsNone(page.to_payload()['actual_points'])
        self.assertEqual(len(self.calls), 1)

    def test_original_https_host_and_verified_context_still_wrap_resolved_socket(self):
        # Keep the real HTTPSConnection class, replacing only TLS bytes with a
        # loopback fixture to inspect SNI/context forwarding without real TLS.
        self.connection_patch.stop()
        context = ssl.create_default_context()
        records = self.original_getaddrinfo('127.0.0.1', self.server.server_port, type=socket.SOCK_STREAM)
        wrapped = []

        def wrap(sock, *, server_hostname):
            wrapped.append((server_hostname, context.check_hostname, context.verify_mode))
            return sock

        with patch('yingba.apollo.socket.getaddrinfo', return_value=records) as dns:
            with patch('yingba.apollo.ssl.create_default_context', return_value=context):
                with patch.object(context, 'wrap_socket', side_effect=wrap):
                    self.assertEqual(len(self.client.search(self.request, authorized=True).records), 2)
        self.assertEqual(dns.call_args.args, (HOST, 443))
        self.assertEqual(wrapped, [(HOST, True, ssl.CERT_REQUIRED)])
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
