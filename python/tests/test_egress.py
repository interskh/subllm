"""D-4b egress filter + confinement (slice 2 T1a).

Hermetic: no public internet. The only path to the origin server is the
injected connector, so "the origin recorded zero requests" is a real assertion
that nothing was connected to, not merely that the client saw an error.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from subllm.drivers.egress import (
    ALLOWED_PORTS,
    EgressFilterProxy,
    confined_argv,
    judge_target,
    sandbox_argv,
    sandbox_profile,
)
from subllm.errors import ClientError

PUBLIC_IP = "93.184.216.34"
BODY = b"origin-body-8f2a"


class _Origin:
    """A local HTTP origin that counts every request it receives."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                origin.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)

            def log_message(self, *a: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def origin():
    o = _Origin()
    yield o
    o.close()


@pytest.fixture
def connector(origin):
    """Every allowed connection lands on the origin, whatever address was
    validated — so the filter's verdict, not the network, decides."""
    calls: list[tuple[str, int]] = []

    def connect(address: str, port: int) -> socket.socket:
        calls.append((address, port))
        return socket.create_connection(("127.0.0.1", origin.port), timeout=5)

    connect.calls = calls  # type: ignore[attr-defined]
    return connect


def fixed_resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError(f"no such host {host!r}")
        return mapping[host]

    return resolve


def speak(port: int, request: str, *, read: bool = True) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(request.encode())
        if not read:
            return b""
        s.settimeout(5)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
    finally:
        s.close()


def get(port: int, url: str) -> bytes:
    host = url.split("//", 1)[1].split("/", 1)[0]
    return speak(port, f"GET {url} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")


# -- allow path -------------------------------------------------------------


def test_a_public_target_is_allowed_and_the_body_flows_through(origin, connector):
    """Catches a filter that refuses everything (or drops the response body),
    which would look 'secure' while making web-agent mode unusable."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        response = get(proxy.port, "http://good.test/page")
    assert b"200 OK" in response.split(b"\r\n", 1)[0]
    assert BODY in response
    assert origin.requests == ["/page"]
    assert connector.calls == [(PUBLIC_IP, 80)]


def test_an_allowed_connect_tunnel_carries_the_request_and_the_response(origin, connector):
    """THE production path: the real client upgrades every fetch to CONNECT, so
    a broken pump direction would ship while every absolute-URI test passes."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.settimeout(5)
        s.sendall(b"CONNECT good.test:443 HTTP/1.1\r\nHost: good.test\r\n\r\n")
        assert b"200" in s.recv(1024)
        s.sendall(b"GET /t HTTP/1.1\r\nHost: good.test\r\nConnection: close\r\n\r\n")
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        s.close()
    assert BODY in out
    assert origin.requests == ["/t"]
    assert connector.calls == [(PUBLIC_IP, 443)]


def test_bytes_sent_with_the_connect_head_are_forwarded_after_the_tunnel_opens(origin, connector):
    """Catches dropping the buffered `rest`, which for a TLS client would eat
    the ClientHello and hang the session."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.settimeout(5)
        s.sendall(
            b"CONNECT good.test:443 HTTP/1.1\r\n\r\n"
            b"GET /early HTTP/1.1\r\nHost: good.test\r\nConnection: close\r\n\r\n"
        )
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        s.close()
    assert BODY in out
    assert origin.requests == ["/early"]


# -- refusals: nothing connects --------------------------------------------


@pytest.mark.parametrize(
    "url, needle",
    [
        ("http://127.0.0.1/x", "not a public address"),
        ("http://10.0.0.7/x", "not a public address"),
        ("http://169.254.169.254/latest/meta-data/", "not a public address"),
        ("http://localhost/x", "loopback"),
    ],
)
def test_non_public_literals_are_refused_before_any_connection(origin, connector, url, needle):
    """Catches a filter that connects first and judges after — the metadata
    endpoint and a LAN service would already have answered."""
    with EgressFilterProxy(resolve=fixed_resolver({}), connect=connector) as proxy:
        response = get(proxy.port, url)
        attempts = proxy.attempts
    assert b"403" in response.split(b"\r\n", 1)[0]
    assert origin.requests == []
    assert connector.calls == []
    assert len(attempts) == 1 and attempts[0].allowed is False
    assert needle in (attempts[0].reason or "")


def test_a_public_looking_name_that_resolves_to_loopback_is_refused(origin, connector):
    """The exact gap the filter closes: the real client happily issues
    CONNECT for a dotted public-looking name that resolves to loopback."""
    resolve = fixed_resolver({"127.0.0.1.nip.io": ["127.0.0.1"]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        response = get(proxy.port, f"http://127.0.0.1.nip.io:{origin.port}/x")
        attempts = proxy.attempts
    assert b"403" in response.split(b"\r\n", 1)[0]
    assert origin.requests == []
    assert connector.calls == []
    # The reason must name the loopback resolution, not the port (the live G3
    # control runs the origin on an unprivileged port).
    assert "resolves to non-public address 127.0.0.1" in (attempts[0].reason or "")


def test_a_non_80_443_port_is_refused_before_any_connection(origin, connector):
    """Catches a widened port rule that would let a confined client reach an
    SSH or database port on an otherwise public address."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        response = speak(proxy.port, "CONNECT good.test:22 HTTP/1.1\r\n\r\n")
        attempts = proxy.attempts
    assert b"403" in response.split(b"\r\n", 1)[0]
    assert origin.requests == []
    assert connector.calls == []
    assert "port 22 is not one of" in (attempts[0].reason or "")
    assert ALLOWED_PORTS == (80, 443)


def test_a_non_http_scheme_never_reaches_the_allowed_path(origin, connector):
    """A file:// (or any non-http) target must be refused by the filter itself,
    not left to the client's own URL validation."""
    with EgressFilterProxy(resolve=fixed_resolver({}), connect=connector) as proxy:
        response = speak(
            proxy.port, "GET file:///etc/passwd HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        attempts = proxy.attempts
    assert b"403" in response.split(b"\r\n", 1)[0]
    assert origin.requests == []
    assert connector.calls == []
    assert "is not http/https" in (attempts[0].reason or "")


def test_an_origin_form_request_is_refused_rather_than_routed_off_host(origin, connector):
    """Catches routing off the Host header, which is a name the client chose
    and the filter never validated."""
    with EgressFilterProxy(resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=connector) as proxy:
        response = speak(proxy.port, "GET /x HTTP/1.1\r\nHost: good.test\r\nConnection: close\r\n\r\n")
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert origin.requests == []
    assert connector.calls == []


@pytest.mark.parametrize(
    "request_line",
    [
        "GET http://[good.test]/x HTTP/1.1",   # brackets round a name
        "GET http://[::1/x HTTP/1.1",          # unclosed bracket
        "GET http://good.test:99999/x HTTP/1.1",  # port out of range
        "CONNECT [::1:443 HTTP/1.1",
    ],
)
def test_a_malformed_target_is_refused_and_still_recorded(origin, connector, request_line):
    """Catches an unparsable target raising inside the handler: nothing would
    connect, but the G3 evidence log would hold no entry for exactly the inputs
    a hostile page can induce, and the client would see an empty response."""
    with EgressFilterProxy(resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=connector) as proxy:
        response = speak(proxy.port, f"{request_line}\r\nHost: x\r\nConnection: close\r\n\r\n")
        attempts = proxy.attempts
    assert response.split(b"\r\n", 1)[0].split()[1] in (b"400", b"403")
    assert origin.requests == []
    assert connector.calls == []
    assert len(attempts) == 1
    assert attempts[0].allowed is False and attempts[0].connected is False
    assert attempts[0].reason


def test_an_allowed_target_whose_connection_fails_is_logged_as_not_connected(origin, connector):
    """Catches a log that reads the same for 'we connected' and 'we permitted
    it but nothing was reached' — T1b grounds G3 on this distinction."""
    def refuse(address: str, port: int) -> socket.socket:
        raise OSError("no route to host")

    with EgressFilterProxy(resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=refuse) as proxy:
        response = get(proxy.port, "http://good.test/x")
        attempts = proxy.attempts
    assert b"502" in response.split(b"\r\n", 1)[0]
    assert attempts[0].allowed is True and attempts[0].connected is False
    assert origin.requests == []


def test_a_completed_connection_is_logged_as_connected(origin, connector):
    with EgressFilterProxy(resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=connector) as proxy:
        get(proxy.port, "http://good.test/x")
        attempts = proxy.attempts
    assert attempts[0].allowed is True and attempts[0].connected is True


def test_a_hung_upstream_does_not_hold_a_handler_thread_forever(origin):
    """Catches an unbounded pump: one hung fetch would pin a thread (and its
    sockets) for the whole session."""
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead.listen(4)
    accepted: list[socket.socket] = []
    threading.Thread(target=lambda: accepted.append(dead.accept()[0]), daemon=True).start()

    def connect(address: str, port: int) -> socket.socket:
        return socket.create_connection(dead.getsockname(), timeout=5)

    proxy = EgressFilterProxy(
        resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=connect, idle_timeout_s=0.5
    )
    proxy.start()
    try:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.settimeout(10)
        s.sendall(b"CONNECT good.test:443 HTTP/1.1\r\n\r\n")
        assert b"200" in s.recv(1024)
        assert s.recv(1024) == b"", "the proxy never gave up on the hung upstream"
        s.close()
        deadline = time.monotonic() + 5
        while proxy.live_thread_count() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proxy.live_thread_count() == 0
    finally:
        proxy.stop()
        for sock in accepted:
            sock.close()
        dead.close()


# -- TOCTOU -----------------------------------------------------------------


def test_the_connection_is_made_to_the_validated_address_not_a_re_resolution(origin, connector):
    """DNS rebinding: a resolver that answers public then loopback must not
    produce a loopback connection. Catches connecting to the host name, or any
    second resolution after the check."""
    answers = [[PUBLIC_IP], ["127.0.0.1"]]
    calls = {"n": 0}

    def flapping(host: str) -> list[str]:
        i = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        return answers[i]

    with EgressFilterProxy(resolve=flapping, connect=connector) as proxy:
        get(proxy.port, "http://rebind.test/x")
        attempts = proxy.attempts
    assert calls["n"] == 1, "the host was resolved more than once"
    assert connector.calls == [(PUBLIC_IP, 80)]
    assert attempts[0].address == PUBLIC_IP


# -- attempt log ------------------------------------------------------------


def test_the_attempt_log_records_both_an_allow_and_a_refusal_with_its_reason(origin, connector):
    """The log is the G3 evidence T1b asserts on; a filter that blocks without
    recording proves nothing."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP], "bad.test": ["10.1.2.3"]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        get(proxy.port, "http://good.test/a")
        get(proxy.port, "http://bad.test/b")
        attempts = proxy.attempts
        refusals = proxy.refusals
    assert [a.allowed for a in attempts] == [True, False]
    assert attempts[0].host == "good.test" and attempts[0].address == PUBLIC_IP
    assert attempts[0].port == 80 and attempts[0].reason is None
    assert attempts[0].method == "GET" and attempts[0].target == "http://good.test/a"
    assert attempts[0].at > 0
    assert len(refusals) == 1 and "10.1.2.3" in (refusals[0].reason or "")
    assert refusals[0].address is None


# -- lifecycle --------------------------------------------------------------


def test_the_proxy_survives_a_client_that_disconnects_mid_tunnel(origin, connector):
    """Catches a handler whose unhandled OSError kills the serving thread."""
    resolve = fixed_resolver({"good.test": [PUBLIC_IP]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.sendall(b"CONNECT good.test:443 HTTP/1.1\r\n\r\n")
        s.settimeout(5)
        assert b"200" in s.recv(1024)
        s.close()  # abrupt disconnect mid-tunnel
        response = get(proxy.port, "http://good.test/after")
    assert BODY in response
    assert origin.requests == ["/after"]


def test_stop_leaves_no_listening_socket_and_no_live_threads(origin, connector):
    """Catches a proxy that keeps the port (or a handler thread) after stop()."""
    proxy = EgressFilterProxy(resolve=fixed_resolver({"good.test": [PUBLIC_IP]}), connect=connector)
    proxy.start()
    port = proxy.port
    get(port, "http://good.test/x")
    proxy.stop()
    assert proxy.live_thread_count() == 0
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


# -- sandbox profile (pure function) ---------------------------------------


def test_the_profile_denies_all_network_and_allows_exactly_one_port():
    """Catches a dropped deny rule (the profile would then allow everything)
    and a widened port rule."""
    text = sandbox_profile(41999)
    assert "(deny network*)" in text
    assert text.count('(allow network-outbound (remote ip "localhost:') == 1
    assert '(allow network-outbound (remote ip "localhost:41999"))' in text
    assert "41999" in text and text.count("41999") == 1
    for other in ("8080", "*"):
        assert f'remote ip "localhost:{other}"' not in text
    assert text.startswith("(version 1)\n(allow default)\n(deny network*)\n")


@pytest.mark.parametrize("bad", [0, -1, 70000, "443", 443.0, True, None])
def test_the_profile_refuses_a_port_that_is_not_a_real_port_number(bad):
    """A string port could smuggle a wildcard into the one allowed rule."""
    with pytest.raises(ValueError):
        sandbox_profile(bad)


def test_the_argv_runs_the_command_under_the_written_profile(tmp_path):
    p = tmp_path / "x.sb"
    assert sandbox_argv(p, ["curl", "-s", "http://x/"]) == [
        "/usr/bin/sandbox-exec", "-f", str(p), "curl", "-s", "http://x/",
    ]


def test_confined_argv_writes_then_removes_the_profile():
    seen: list[str] = []
    with confined_argv(41999, ["/bin/echo", "hi"]) as argv:
        assert argv[0] == "/usr/bin/sandbox-exec" and argv[1] == "-f"
        seen.append(argv[2])
        assert "41999" in open(argv[2]).read()
    assert not os.path.exists(seen[0])


def test_confinement_fails_closed_when_sandbox_exec_is_missing(tmp_path):
    """Catches a fallback that would run the client unconfined."""
    with pytest.raises(ClientError) as e:
        with confined_argv(41999, ["/bin/echo"], sandbox_exec=str(tmp_path / "nope")):
            pass
    assert "sandbox-exec" in str(e.value)


def test_confinement_fails_closed_when_the_profile_cannot_be_written(monkeypatch):
    def boom(*a: object, **k: object):
        raise OSError("read-only file system")

    monkeypatch.setattr("subllm.drivers.egress.tempfile.mkstemp", boom)
    with pytest.raises(ClientError):
        with confined_argv(41999, ["/bin/echo"]):
            pass


def test_a_profile_that_fails_mid_write_leaves_no_file_behind(monkeypatch):
    """Catches leaking a half-written profile into the temp dir on the failure
    path — it would be an unused but readable description of the boundary."""
    made: list[str] = []
    real = tempfile.mkstemp

    def mkstemp(*a: object, **k: object):
        fd, path = real(*a, **k)
        made.append(path)
        return fd, path

    class Failing:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def write(self, data: str) -> int:
            raise OSError("disk full")

    def fdopen(fd: int, mode: str = "r", *a: object, **k: object):
        os.close(fd)
        return Failing()

    monkeypatch.setattr("subllm.drivers.egress.tempfile.mkstemp", mkstemp)
    monkeypatch.setattr(os, "fdopen", fdopen)
    with pytest.raises(ClientError):
        with confined_argv(41999, ["/bin/echo"]):
            pass
    assert made and not os.path.exists(made[0])


# -- judge_target directly --------------------------------------------------


def test_judge_target_refuses_an_unresolvable_host():
    """Unknown evidence fails closed (publictargets' own contract)."""
    v = judge_target("http://nope.test/", resolve=fixed_resolver({}))
    assert v.address is None and "could not be resolved" in (v.reason or "")


# -- end-to-end confinement -------------------------------------------------

_SANDBOX = "/usr/bin/sandbox-exec"


@pytest.mark.skipif(
    not (os.path.exists(_SANDBOX) and shutil.which("curl")),
    reason="needs sandbox-exec and curl",
)
def test_a_confined_process_reaches_the_origin_only_through_the_filter(origin, connector):
    """The whole D-4b mechanism end to end: unconfined curl reaches the origin
    directly (so the negative control is not vacuous), confined curl cannot,
    and through the proxy a public target flows while a public-looking name
    that resolves to loopback is refused with the origin seeing nothing."""
    direct = f"http://127.0.0.1:{origin.port}/direct"
    unconfined = subprocess.run(["curl", "-sS", "-m", "10", direct], capture_output=True)
    assert unconfined.returncode == 0 and BODY in unconfined.stdout
    assert origin.requests == ["/direct"]

    resolve = fixed_resolver({"good.test": [PUBLIC_IP], "looks-public.test": ["127.0.0.1"]})
    with EgressFilterProxy(resolve=resolve, connect=connector) as proxy:
        with confined_argv(proxy.port, ["curl", "-sS", "-m", "10", direct]) as argv:
            blocked = subprocess.run(argv, capture_output=True)
        assert blocked.returncode != 0, blocked.stdout
        assert origin.requests == ["/direct"], "confinement leaked: the origin was reached directly"

        with confined_argv(proxy.port, ["curl", "-sS", "-m", "10", "--proxy", proxy.proxy_url, "http://good.test/ok"]) as argv:
            allowed = subprocess.run(argv, capture_output=True)
        assert allowed.returncode == 0, allowed.stderr
        assert BODY in allowed.stdout
        assert origin.requests == ["/direct", "/ok"]

        with confined_argv(proxy.port, ["curl", "-sS", "-m", "10", "--proxy", proxy.proxy_url, "http://looks-public.test/leak"]) as argv:
            refused = subprocess.run(argv, capture_output=True)
        assert refused.returncode != 0 or b"refused by subllm egress filter" in refused.stdout
        assert origin.requests == ["/direct", "/ok"], "a loopback-resolving name reached the origin"

        # What the live G3 control actually sends: the client upgrades every
        # fetch to CONNECT, so the refusal must hold on the tunnel path too.
        with confined_argv(proxy.port, ["curl", "-sS", "-m", "10", "--proxy", proxy.proxy_url, "https://looks-public.test/leak"]) as argv:
            tunnel = subprocess.run(argv, capture_output=True)
        assert tunnel.returncode != 0, tunnel.stdout
        assert origin.requests == ["/direct", "/ok"], "a loopback-resolving CONNECT reached the origin"
        assert [a.allowed for a in proxy.attempts] == [True, False, False]
        assert proxy.attempts[2].method == "CONNECT"
        assert "resolves to non-public address 127.0.0.1" in (proxy.attempts[2].reason or "")
