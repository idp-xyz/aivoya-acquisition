"""Offline egress tests. DNS and TCP are scripted; nothing here dials the network."""
import io
import os
import socket
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aivoya_worker.main import api_request, main
from aivoya_worker.policy import (
    PolicyBlocked,
    ROBOTS_CACHE,
    controlled_get,
    egress_proxy,
    enforce,
    validate_url,
)

# Sentinels only. Do not substitute this machine's proxy address.
AMBIENT = "http://10.9.9.9:8888"
EGRESS = "http://10.8.8.8:3128"
_PROXY_KEYS = (
    "AIVOYA_EGRESS_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _http(status, reason, extra, body=b""):
    headers = {"Content-Length": str(len(body)), "Connection": "close"}
    headers.update(extra)
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _rows(addresses, port):
    port_n = int(port) if port is not None else 0
    rows = []
    for ip in addresses:
        if ":" in ip:
            sockaddr = (ip, port_n, 0, 0)
            family = socket.AF_INET6
        else:
            sockaddr = (ip, port_n)
            family = socket.AF_INET
        rows.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return rows


class ScriptedResolver:
    """First answer is public unless a test says otherwise. Later calls can rebind."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append(host)
        planned = self.answers.get(host)
        if planned is None:
            raise AssertionError(f"unexpected DNS query for {host}")
        if callable(planned):
            ips = planned(host, self.calls)
        else:
            ips = planned
        return _rows(ips, port)


class ScriptedSockets:
    def __init__(self, responses, fail=()):
        self.responses = list(responses)
        self.fail = set(fail)
        self.attempts = []
        self.payloads = []

    def __call__(self, family, sock_type, proto=0):
        log = self

        class _Sock:
            def __init__(self):
                self.sent = b""

            def settimeout(self, timeout):
                return None

            def gettimeout(self):
                return None

            def setsockopt(self, *args, **kwargs):
                return None

            def bind(self, address):
                return None

            def connect(self, address):
                log.attempts.append(address)
                if address[0] in log.fail:
                    raise OSError("network unreachable")

            def sendall(self, data):
                self.sent += bytes(data)

            def makefile(self, *args, **kwargs):
                log.payloads.append(self.sent)
                if not log.responses:
                    raise AssertionError("connection had no scripted response")
                return io.BytesIO(log.responses.pop(0))

            def close(self):
                return None

            def shutdown(self, how):
                return None

        return _Sock()


@contextmanager
def _patched(resolver, sockets, **env):
    """Drop ambient proxy variables, then apply only what the test sets."""
    with patch.dict(os.environ, {}, clear=False):
        saved = {key: os.environ.get(key) for key in _PROXY_KEYS}
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)
        try:
            with patch("socket.getaddrinfo", resolver), patch("socket.socket", sockets):
                yield
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


def _ok(body=b"hello"):
    return _http(200, "OK", {"Content-Type": "text/html"}, body)


class TestPinnedConnection(unittest.TestCase):
    def test_rebinding_connects_only_to_first_address(self):
        def answers(host, calls):
            return ["1.2.3.4"] if len(calls) == 1 else ["10.0.0.1"]

        dns = ScriptedResolver({"news.example": answers})
        socks = ScriptedSockets([_ok()])
        with _patched(dns, socks, HTTP_PROXY=AMBIENT, HTTPS_PROXY=AMBIENT):
            payload, status, final, _media = controlled_get("http://news.example/article")
        self.assertEqual(payload, b"hello")
        self.assertEqual(status, 200)
        self.assertEqual(final, "http://news.example/article")
        self.assertEqual(dns.calls, ["news.example"])
        self.assertEqual([item[0] for item in socks.attempts], ["1.2.3.4"])
        self.assertEqual(socks.attempts[0][1], 80)
        sent = socks.payloads[0].decode()
        self.assertTrue(sent.startswith("GET /article HTTP/1.1"))
        self.assertIn("Host: news.example", sent)
        self.assertNotIn("10.0.0.1", sent)

    def test_mixed_public_and_private_addresses_blocked(self):
        dns = ScriptedResolver({"news.example": ["1.2.3.4", "10.1.0.5"]})
        socks = ScriptedSockets([])
        with _patched(dns, socks):
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/article")
        self.assertIn("private or non-global", str(caught.exception))
        self.assertEqual(socks.attempts, [])
        self.assertEqual(dns.calls, ["news.example"])

    def test_redirect_to_privately_resolved_host_blocked(self):
        dns = ScriptedResolver({
            "news.example": ["1.2.3.4"],
            "evil.example": ["10.1.0.5"],
        })
        redirect = _http(302, "Found", {"Location": "http://evil.example/secret"})
        socks = ScriptedSockets([redirect])
        with _patched(dns, socks):
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/start")
        self.assertIn("private or non-global", str(caught.exception))
        self.assertEqual([item[0] for item in socks.attempts], ["1.2.3.4"])
        self.assertIn("evil.example", dns.calls)
        self.assertNotIn("10.1.0.5", [item[0] for item in socks.attempts])

    def test_tls_wraps_original_hostname(self):
        def answers(host, calls):
            return ["1.2.3.4"] if len(calls) == 1 else ["10.0.0.1"]

        dns = ScriptedResolver({"news.example": answers})
        socks = ScriptedSockets([_ok()])
        seen = []

        def wrap(self, sock, *args, **kwargs):
            seen.append(kwargs.get("server_hostname"))
            return sock

        with _patched(dns, socks):
            with patch("ssl.SSLContext.wrap_socket", wrap):
                controlled_get("https://News.Example/article")
        self.assertEqual(dns.calls, ["news.example"])
        self.assertEqual(socks.attempts, [("1.2.3.4", 443)])
        self.assertEqual(seen, ["News.Example"])
        sent = socks.payloads[0].decode()
        self.assertIn("Host: News.Example", sent)
        self.assertTrue(sent.startswith("GET /article HTTP/1.1"))

    def test_ambient_proxy_environment_ignored(self):
        dns = ScriptedResolver({"news.example": ["1.2.3.4"]})
        socks = ScriptedSockets([_ok()])
        with _patched(
            dns,
            socks,
            HTTP_PROXY=AMBIENT,
            HTTPS_PROXY=AMBIENT,
            http_proxy=AMBIENT,
            https_proxy=AMBIENT,
            ALL_PROXY=AMBIENT,
            all_proxy=AMBIENT,
        ):
            controlled_get("http://news.example/article")
        self.assertEqual(dns.calls, ["news.example"])
        self.assertEqual([item[0] for item in socks.attempts], ["1.2.3.4"])
        self.assertNotIn("10.9.9.9", dns.calls)
        self.assertTrue(socks.payloads[0].startswith(b"GET /article HTTP/1.1"))

    def test_unreachable_ipv6_falls_back_in_order(self):
        dns = ScriptedResolver({"news.example": ["2606:4700:4700::1111", "1.2.3.4"]})
        socks = ScriptedSockets([_ok()], fail={"2606:4700:4700::1111"})
        with _patched(dns, socks):
            payload, status, _final, _media = controlled_get("http://news.example/article")
        self.assertEqual(payload, b"hello")
        self.assertEqual(status, 200)
        self.assertEqual(dns.calls, ["news.example"])
        self.assertEqual(socks.attempts[0][0], "2606:4700:4700::1111")
        self.assertEqual(socks.attempts[1][0], "1.2.3.4")
        self.assertEqual(socks.attempts[1][1], 80)

    def test_literal_public_ip_is_not_resolved(self):
        def boom(host, *args, **kwargs):
            raise AssertionError(f"resolver consulted for {host}")

        socks = ScriptedSockets([_ok(b"ip")])
        with _patched(boom, socks):
            payload, status, final, _media = controlled_get("http://1.2.3.4/x")
        self.assertEqual((payload, status, final), (b"ip", 200, "http://1.2.3.4/x"))
        self.assertEqual(socks.attempts, [("1.2.3.4", 80)])
        self.assertIn("Host: 1.2.3.4", socks.payloads[0].decode())

    def test_allow_private_pins_resolved_address(self):
        def answers(host, calls):
            return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.1"]

        dns = ScriptedResolver({"localhost": answers})
        socks = ScriptedSockets([_ok(b"local")])
        with _patched(dns, socks):
            payload, _status, _final, _media = controlled_get("http://localhost/a", allow_private=True)
        self.assertEqual(payload, b"local")
        self.assertEqual(dns.calls, ["localhost"])
        self.assertEqual(socks.attempts[0][0], "127.0.0.1")

    def test_resolve_false_does_not_allow_names(self):
        def boom(host, *args, **kwargs):
            raise AssertionError(f"resolver consulted for {host}")

        with patch("socket.getaddrinfo", boom):
            with self.assertRaises(PolicyBlocked):
                validate_url("https://news.example/a", resolve=False)
            with self.assertRaises(PolicyBlocked):
                validate_url("http://127.0.0.1/a", resolve=False)

    def test_http_status_404_message_unchanged(self):
        dns = ScriptedResolver({"news.example": ["1.2.3.4"]})
        socks = ScriptedSockets([_http(404, "Not Found", {})])
        with _patched(dns, socks):
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/missing")
        self.assertIn("HTTP status 404", str(caught.exception))

    def test_robots_fetch_is_pinned(self):
        from aivoya_worker.policy import robots_permits

        dns = ScriptedResolver({"news.example": ["1.2.3.4"]})
        body = b"User-agent: *\nDisallow: /private\n"
        socks = ScriptedSockets([_http(200, "OK", {"Content-Type": "text/plain"}, body)])
        ROBOTS_CACHE.clear()
        try:
            with _patched(dns, socks):
                allowed = robots_permits("http://news.example/article")
        finally:
            ROBOTS_CACHE.clear()
        self.assertTrue(allowed)
        self.assertEqual(dns.calls, ["news.example"])
        self.assertEqual(socks.attempts[0][0], "1.2.3.4")
        self.assertTrue(socks.payloads[0].startswith(b"GET /robots.txt HTTP/1.1"))

    def test_same_host_redirect_pinned_per_hop(self):
        answers = iter([["1.2.3.4"], ["8.8.8.8"]])

        def per_hop(host, calls):
            return next(answers)

        dns = ScriptedResolver({"news.example": per_hop})
        redirect = _http(302, "Found", {"Location": "http://news.example/next"})
        socks = ScriptedSockets([redirect, _ok(b"next")])
        with _patched(dns, socks):
            payload, status, final, _media = controlled_get("http://news.example/start")
        self.assertEqual(payload, b"next")
        self.assertEqual(status, 200)
        self.assertEqual(final, "http://news.example/next")
        self.assertEqual(dns.calls, ["news.example", "news.example"])
        self.assertEqual([item[0] for item in socks.attempts], ["1.2.3.4", "8.8.8.8"])

    def test_cross_origin_redirect_blocked(self):
        dns = ScriptedResolver({
            "news.example": ["1.2.3.4"],
            "other.example": ["8.8.8.8"],
        })
        redirect = _http(302, "Found", {"Location": "http://other.example/x"})
        socks = ScriptedSockets([redirect])
        with _patched(dns, socks):
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/start")
        self.assertIn("Cross-origin redirect blocked", str(caught.exception))
        self.assertEqual([item[0] for item in socks.attempts], ["1.2.3.4"])


class TestProxyControls(unittest.TestCase):
    def test_explicit_proxy_routes_and_blocks_names(self):
        def dns(host, port, *args, **kwargs):
            calls.append(host)
            if host == "news.example":
                raise AssertionError("destination was resolved in proxy mode")
            if host == "10.8.8.8":
                return _rows(["10.8.8.8"], port)
            raise AssertionError(f"unexpected DNS query for {host}")

        calls = []
        socks = ScriptedSockets([_ok(b"via-proxy")])
        env = dict(
            AIVOYA_EGRESS_PROXY=EGRESS,
            HTTP_PROXY=AMBIENT,
            HTTPS_PROXY=AMBIENT,
            NO_PROXY="*",
            no_proxy="*",
        )
        with _patched(dns, socks, **env):
            payload, status, final, _media = controlled_get("http://news.example/path")
            self.assertEqual(payload, b"via-proxy")
            self.assertEqual(status, 200)
            self.assertEqual(final, "http://news.example/path")
            blocked = (
                "http://10.1.2.3/",
                "http://127.0.0.1/",
                "http://169.254.169.254/",
                "http://localhost/a",
                "http://printer.local/a",
                "https://svc.internal/a",
            )
            for url in blocked:
                with self.subTest(url=url):
                    before = len(socks.attempts)
                    with self.assertRaises(PolicyBlocked):
                        controlled_get(url)
                    self.assertEqual(len(socks.attempts), before)
            redirect = _http(302, "Found", {"Location": "http://127.0.0.1/secret"})
            socks.responses.append(redirect)
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/start")
            self.assertIn("private or non-global", str(caught.exception))
        self.assertNotIn("news.example", calls)
        self.assertNotIn("10.9.9.9", calls)
        self.assertTrue(all(item[0] == "10.8.8.8" and item[1] == 3128 for item in socks.attempts))
        sent = socks.payloads[0].decode()
        self.assertTrue(sent.startswith("GET http://news.example/path HTTP/1.1"))
        self.assertIn("Host: news.example", sent)

    def test_https_proxy_tunnels_and_keeps_origin_hostname(self):
        calls = []

        def dns(host, port, *args, **kwargs):
            calls.append(host)
            if host != "10.8.8.8":
                raise AssertionError(f"resolved {host}")
            return _rows(["10.8.8.8"], port)

        connect_ok = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        socks = ScriptedSockets([connect_ok, _ok(b"tunneled")])
        seen = []

        def wrap(self, sock, *args, **kwargs):
            seen.append(kwargs.get("server_hostname"))
            return sock

        with _patched(dns, socks, AIVOYA_EGRESS_PROXY=EGRESS, HTTP_PROXY=AMBIENT):
            with patch("ssl.SSLContext.wrap_socket", wrap):
                payload, status, final, _media = controlled_get("https://news.example/article")
        self.assertEqual(payload, b"tunneled")
        self.assertEqual(status, 200)
        self.assertEqual(final, "https://news.example/article")
        self.assertEqual(calls, ["10.8.8.8"])
        self.assertEqual(socks.attempts, [("10.8.8.8", 3128)])
        self.assertEqual(seen, ["news.example"])
        self.assertTrue(socks.payloads[0].startswith(b"CONNECT news.example:443 "))
        sent = socks.payloads[-1].decode()
        self.assertIn("GET /article HTTP/1.1", sent)
        self.assertIn("Host: news.example", sent)

    def test_proxy_allows_same_host_redirect_without_local_dns(self):
        calls = []

        def dns(host, port, *args, **kwargs):
            calls.append(host)
            if host != "10.8.8.8":
                raise AssertionError(f"resolved {host}")
            return _rows(["10.8.8.8"], port)

        redirect = _http(302, "Found", {"Location": "http://news.example/next"})
        socks = ScriptedSockets([redirect, _ok(b"next")])
        with _patched(dns, socks, AIVOYA_EGRESS_PROXY=EGRESS, NO_PROXY="*"):
            payload, _status, final, _media = controlled_get("http://news.example/start")
        self.assertEqual(payload, b"next")
        self.assertEqual(final, "http://news.example/next")
        self.assertEqual(calls, ["10.8.8.8", "10.8.8.8"])
        self.assertEqual(len(socks.attempts), 2)
        other = _http(302, "Found", {"Location": "http://other.example/x"})
        socks.responses.append(other)
        with _patched(dns, socks, AIVOYA_EGRESS_PROXY=EGRESS):
            with self.assertRaises(PolicyBlocked) as caught:
                controlled_get("http://news.example/start")
        self.assertIn("Cross-origin redirect blocked", str(caught.exception))

    def test_invalid_egress_proxy_rejected(self):
        rejected = (
            "https://proxy.example:3128",
            "http://user:pass@proxy.example:3128",
            "http://user@proxy.example:3128",
            "socks5://127.0.0.1:1080",
            "http://proxy.example",
            "http://proxy.example:3128/extra",
            "http://proxy.example:3128?x=1",
            "http://proxy.example:3128#frag",
            "ftp://proxy.example:21",
            "not-a-url",
        )
        for value in rejected:
            with self.subTest(value=value):
                with patch.dict(os.environ, {"AIVOYA_EGRESS_PROXY": value}):
                    with self.assertRaises(PolicyBlocked) as caught:
                        egress_proxy()
                    self.assertIn("AIVOYA_EGRESS_PROXY", str(caught.exception))
                    with self.assertRaises(PolicyBlocked):
                        controlled_get("http://news.example/")
        with patch.dict(os.environ, {"AIVOYA_EGRESS_PROXY": "   "}):
            self.assertIsNone(egress_proxy())
        with patch.dict(os.environ, {"AIVOYA_EGRESS_PROXY": "http://10.0.0.5:3128"}):
            self.assertEqual(egress_proxy(), "http://10.0.0.5:3128")
        with patch.dict(os.environ, {"AIVOYA_EGRESS_PROXY": "http://[::1]:3128"}):
            self.assertEqual(egress_proxy(), "http://[::1]:3128")
        with patch.dict(os.environ, {
            "AIVOYA_WORKER_TOKEN": "x" * 24,
            "AIVOYA_FETCH_ENGINE": "stdlib",
            "AIVOYA_ENV": "development",
            "AIVOYA_EGRESS_PROXY": "https://bad.example:3128",
        }):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertIn("AIVOYA_EGRESS_PROXY", str(caught.exception))

    def test_startup_logs_delegated_enforcement(self):
        with patch.dict(os.environ, {
            "AIVOYA_WORKER_TOKEN": "x" * 24,
            "AIVOYA_FETCH_ENGINE": "stdlib",
            "AIVOYA_ENV": "development",
            "AIVOYA_EGRESS_PROXY": EGRESS,
        }):
            with patch("aivoya_worker.main.api_request", side_effect=RuntimeError("stop")):
                with self.assertLogs("aivoya.worker", level="INFO") as logs:
                    with self.assertRaises(RuntimeError):
                        main()
        self.assertTrue(any(
            "destination-address enforcement is delegated to the egress proxy" in line
            for line in logs.output
        ))

    def test_control_plane_bypasses_proxies(self):
        calls = []

        def dns(host, port, *args, **kwargs):
            calls.append((host, int(port)))
            if host != "api":
                raise AssertionError(f"control plane resolved {host}")
            return _rows(["172.16.5.5"], port)

        socks = ScriptedSockets([_http(200, "OK", {"Content-Type": "application/json"}, b"{}")])
        with _patched(dns, socks, HTTP_PROXY=AMBIENT, HTTPS_PROXY=AMBIENT, AIVOYA_EGRESS_PROXY=EGRESS):
            body = api_request("http://api:8080", "t" * 24, "/v1/jobs/claim")
        self.assertEqual(body, {})
        self.assertEqual(calls, [("api", 8080)])
        self.assertEqual(socks.attempts, [("172.16.5.5", 8080)])
        self.assertTrue(socks.payloads[0].startswith(b"POST /v1/jobs/claim HTTP/1.1"))

    def test_enforce_skips_dns_when_proxy_set(self):
        def boom(host, *args, **kwargs):
            raise AssertionError(f"resolved {host}")

        job = {
            "schema_version": "1.0",
            "rights": "full_text_licensed",
            "method": "html_static",
            "url": "https://news.example/a",
        }
        with patch.dict(os.environ, {"AIVOYA_EGRESS_PROXY": EGRESS}):
            with patch("socket.getaddrinfo", boom), patch("aivoya_worker.policy.robots_permits", return_value=True):
                self.assertEqual(enforce(job), job["url"])


if __name__ == "__main__":
    unittest.main()
