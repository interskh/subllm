"""D-4b: a subllm-run egress filter, plus the confinement that makes it the
only network path (slice 2, for the Claude client whose fetch runs locally).

The filter is a forward proxy on 127.0.0.1: it judges every destination with
`subllm.publictargets.classify_url` BEFORE connecting, connects to the address
it validated (never re-resolving, so a name cannot flip to loopback between the
check and the dial), and records every attempt for the caller to read back as
G3 evidence. No TLS interception: a CONNECT is either refused or tunnelled
opaquely.

The confinement wrapper builds a `sandbox-exec` profile that denies all network
except one loopback port. The property it gives, stated exactly: **the only
reachable TCP network destination is the filter's port.** Unix-domain sockets
and name resolution stay available (measured: a confined `curl --unix-socket`
still reaches a local service), because the client's own IPC and DNS need them
— so the profile is not what confines the model. What stops the model using
them is the proven tool set (D-3a): it is offered no tool that can speak to a
unix socket. This filter closes the one path it does have, its web fetch.
"""
from __future__ import annotations

import os
import select
import socket
import socketserver
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

from subllm.errors import ClientError
from subllm.publictargets import (
    CODE_NON_PUBLIC,
    CODE_RESOLVED_NON_PUBLIC,
    CODE_UNRESOLVED,
    CachingResolver,
    Resolver,
    classify_url_detail,
    default_resolver,
)

#: Re-exported so a consumer judging an Attempt imports every code from here.
RESOLVED_NON_PUBLIC = CODE_RESOLVED_NON_PUBLIC

#: Reason codes this module adds on top of `publictargets`' own.
CODE_PORT = "port"
CODE_UNPARSABLE = "unparsable"

#: D-4b judges public http/https targets; nothing else needs another port, and
#: a wider rule would let a confined client reach a service on a public address
#: that is not the web (SSH, a database, a mail relay).
ALLOWED_PORTS: tuple[int, ...] = (80, 443)

#: Hardcoded, never looked up on PATH: the confinement boundary must not be
#: satisfiable by a binary an earlier PATH entry supplies.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_MAX_HEADER_BYTES = 64 * 1024
_HEADER_TIMEOUT_S = 15.0
_CONNECT_TIMEOUT_S = 30.0
_PUMP_TICK_S = 0.5
#: An open connection with no traffic either way for this long is abandoned, so
#: a hung upstream cannot hold a handler thread for the session's lifetime.
_IDLE_TIMEOUT_S = 180.0
_CHUNK = 64 * 1024

#: (address, port) -> connected socket. Injected in tests so the filter can be
#: exercised without touching the public internet.
Connector = Callable[[str, int], socket.socket]


@dataclass(frozen=True)
class Attempt:
    """One destination the confined process asked for."""

    at: float
    method: str
    target: str
    host: str | None
    port: int | None
    address: str | None
    allowed: bool
    reason: str | None
    #: True only once a connection to `address` actually succeeded. `allowed`
    #: alone would read the same for a refused-nothing 502, and the log is the
    #: G3 evidence: "we permitted it" and "we connected" must be separable.
    connected: bool = False
    #: Machine-readable reason for a refusal: `non_public | unresolved | port |
    #: scheme | unparsable | missing`, None when allowed. A consumer must judge
    #: on this, never on the prose — a transient DNS miss (`unresolved`) is a
    #: delivery failure, while `non_public` is an isolation violation.
    code: str | None = None


@dataclass(frozen=True)
class _Verdict:
    host: str | None
    port: int | None
    address: str | None
    reason: str | None
    code: str | None = None
    #: every address the check validated, in resolution order. All passed the
    #: same check, so trying them in turn does not widen the guard — it only
    #: keeps a dual-stack name working when one family has no route.
    addresses: tuple[str, ...] = ()


class _OneShotResolver:
    """Resolve a host at most once and keep what came back.

    `classify_url` takes a resolver but returns only a verdict; the proxy needs
    the addresses that verdict was reached on, because those — not the name —
    are what it connects to.
    """

    def __init__(self, resolve: Resolver) -> None:
        self._inner = CachingResolver(resolve)
        self.addresses: list[str] = []

    def __call__(self, host: str) -> list[str]:
        got = self._inner(host)
        self.addresses = list(got)
        return got


def _default_connect(address: str, port: int) -> socket.socket:
    return socket.create_connection((address, port), timeout=_CONNECT_TIMEOUT_S)


def judge_target(
    url: str,
    *,
    resolve: Resolver = default_resolver,
    allowed_ports: Sequence[int] = ALLOWED_PORTS,
) -> _Verdict:
    """Decide whether `url` may be connected to, and to which address.

    Total: any parse failure returns a refusing verdict rather than raising, so
    a malformed target a hostile page induced is refused AND recorded, never a
    hole in the evidence log.
    """
    try:
        parts = urlsplit(url)
        _ = parts.hostname, parts.port
    except ValueError as e:
        return _Verdict(None, None, None, f"unparsable target ({e}): {url!r}", CODE_UNPARSABLE)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return _Verdict(None, None, None, f"scheme {parts.scheme or '(none)'!r} is not http/https: {url!r}", "scheme")
    try:
        port = parts.port
    except ValueError:
        return _Verdict(parts.hostname, None, None, f"invalid port in {url!r}", CODE_UNPARSABLE)
    if port is None:
        port = 443 if scheme == "https" else 80
    # Publicness is judged BEFORE the port rule: the live G3 control fetches a
    # public-looking name that resolves to loopback on an unprivileged port, and
    # the recorded reason must name the loopback resolution, not the port. The
    # cost is one resolution for a target the port rule then refuses anyway.
    recorder = _OneShotResolver(resolve)
    reason, code = classify_url_detail(url, resolve=recorder)
    if reason is not None:
        return _Verdict(parts.hostname, port, None, reason, code or CODE_NON_PUBLIC)
    if port not in tuple(allowed_ports):
        return _Verdict(parts.hostname, port, None, f"port {port} is not one of {tuple(allowed_ports)}: {url!r}", CODE_PORT)
    # A literal host never reaches the resolver; it is its own validated address.
    validated = tuple(recorder.addresses) or ((parts.hostname,) if parts.hostname else ())
    if not validated:
        return _Verdict(parts.hostname, port, None, f"no address for {url!r}", CODE_UNRESOLVED)
    return _Verdict(parts.hostname, port, validated[0], None, None, validated)


def _connect_url(authority: str) -> str | None:
    """Synthesize the URL a `CONNECT host:port` target stands for."""
    if any(c in authority for c in "/?#@") or not authority:
        return None
    hostpart, sep, portpart = authority.rpartition(":")
    if not sep or not hostpart:
        return None
    try:
        port = int(portpart)
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    scheme = "https" if port == 443 else "http"
    return f"{scheme}://{hostpart}:{port}/"


class _BadRequest(Exception):
    pass


def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    """Read the request head; return (head, bytes already read past it)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > _MAX_HEADER_BYTES:
            raise _BadRequest("request head too large")
        try:
            chunk = conn.recv(_CHUNK)
        except (socket.timeout, OSError) as e:
            raise _BadRequest(f"could not read request head ({e})") from e
        if not chunk:
            raise _BadRequest("client closed before sending a request head")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _origin_form(parts) -> str:
    path = parts.path or "/"
    return urlunsplit(("", "", path, parts.query, ""))


def _rebuild_request(head: bytes, method: str, url: str, version: str) -> bytes:
    parts = urlsplit(url)
    lines = head.split(b"\r\n")[1:]
    out = [f"{method} {_origin_form(parts)} {version}".encode()]
    for line in lines:
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"proxy-connection", b"proxy-authorization", b"connection"):
            continue
        out.append(line)
    out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


def _pump(
    a: socket.socket,
    b: socket.socket,
    stopping: threading.Event,
    idle_timeout_s: float,
) -> None:
    for s in (a, b):
        s.settimeout(None)
    last = time.monotonic()
    while not stopping.is_set():
        try:
            ready, _, _ = select.select([a, b], [], [], _PUMP_TICK_S)
        except (OSError, ValueError):
            return
        if not ready:
            # A hung upstream must not pin a handler thread for the life of the
            # session; the caller's call-level timeout is too coarse for that.
            if idle_timeout_s and time.monotonic() - last >= idle_timeout_s:
                return
            continue
        last = time.monotonic()
        for s in ready:
            other = b if s is a else a
            try:
                data = s.recv(_CHUNK)
            except OSError:
                return
            if not data:
                return
            try:
                other.sendall(data)
            except OSError:
                return


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # server_close() must not join handler threads unboundedly: stop() closes
    # every tracked socket first and then joins with its own timeout.
    block_on_close = False
    allow_reuse_address = True
    proxy: "EgressFilterProxy"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: C901 — one linear request path
        proxy: EgressFilterProxy = self.server.proxy  # type: ignore[attr-defined]
        conn: socket.socket = self.request
        thread = threading.current_thread()
        proxy._track(conn, thread)
        try:
            conn.settimeout(_HEADER_TIMEOUT_S)
            try:
                head, rest = _read_head(conn)
            except _BadRequest as e:
                proxy._record(Attempt(time.time(), "?", "", None, None, None, False, str(e), code=CODE_UNPARSABLE))
                _reply(conn, 400, str(e))
                return
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            bits = line.split()
            if len(bits) != 3:
                proxy._record(Attempt(time.time(), "?", line[:200], None, None, None, False, "malformed request line", code=CODE_UNPARSABLE))
                _reply(conn, 400, "malformed request line")
                return
            method, target, version = bits[0].upper(), bits[1], bits[2]
            if method == "CONNECT":
                url = _connect_url(target)
                if url is None:
                    proxy._record(Attempt(time.time(), method, target, None, None, None, False, "malformed CONNECT authority", code=CODE_UNPARSABLE))
                    _reply(conn, 400, "malformed CONNECT authority")
                    return
            else:
                # Absolute-URI only. An origin-form request carries no target we
                # may trust (routing it off `Host` would judge a name the client
                # chose, not the destination), so it is refused.
                if "://" not in target:
                    proxy._record(Attempt(time.time(), method, target, None, None, None, False, "request target is not an absolute URI", code=CODE_UNPARSABLE))
                    _reply(conn, 400, "request target is not an absolute URI")
                    return
                url = target
            verdict = judge_target(url, resolve=proxy._resolve, allowed_ports=proxy._allowed_ports)
            index = proxy._record(
                Attempt(
                    at=time.time(),
                    method=method,
                    target=target,
                    host=verdict.host,
                    port=verdict.port,
                    address=verdict.address,
                    allowed=verdict.reason is None,
                    reason=verdict.reason,
            code=verdict.code,
                )
            )
            if verdict.reason is not None:
                _reply(conn, 403, f"refused by subllm egress filter: {verdict.reason}")
                return
            assert verdict.address is not None and verdict.port is not None
            upstream = None
            last: OSError | None = None
            for address in verdict.addresses or (verdict.address,):
                try:
                    upstream = proxy._connect(address, verdict.port)
                    break
                except OSError as e:
                    last = e
            if upstream is None:
                _reply(conn, 502, f"upstream connect failed: {last}")
                return
            proxy._mark_connected(index)
            proxy._track(upstream, thread)
            try:
                if method == "CONNECT":
                    conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if rest:
                        upstream.sendall(rest)
                else:
                    upstream.sendall(_rebuild_request(head, method, url, version))
                    if rest:
                        upstream.sendall(rest)
                _pump(conn, upstream, proxy._stopping, proxy._idle_timeout_s)
            except OSError:
                return
            finally:
                proxy._untrack(upstream)
                _shut(upstream)
        finally:
            proxy._untrack(conn)
            _shut(conn)
            proxy._retire(thread)


def _shut(s: socket.socket) -> None:
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _reply(conn: socket.socket, status: int, message: str) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
    body = message.encode("utf-8", "replace")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(head + body)
    except OSError:
        pass


class EgressFilterProxy:
    """A filtering forward proxy on 127.0.0.1, startable from Python.

    Usage:
        with EgressFilterProxy() as proxy:
            ...  # run the confined client with http_proxy=127.0.0.1:proxy.port
            attempts = proxy.attempts
    """

    def __init__(
        self,
        *,
        resolve: Resolver = default_resolver,
        connect: Connector | None = None,
        allowed_ports: Sequence[int] = ALLOWED_PORTS,
        idle_timeout_s: float = _IDLE_TIMEOUT_S,
    ) -> None:
        self._resolve = resolve
        self._connect = connect or _default_connect
        self._allowed_ports = tuple(allowed_ports)
        self._idle_timeout_s = idle_timeout_s
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._attempts: list[Attempt] = []
        self._live: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "EgressFilterProxy":
        if self._server is not None:
            raise ClientError("egress filter already started")
        server = _ProxyServer(("127.0.0.1", 0), _Handler)
        server.proxy = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="subllm-egress")
        self._thread.start()
        return self

    def stop(self, *, join_timeout_s: float = 5.0) -> None:
        if self._server is None:
            return
        self._stopping.set()
        self._server.shutdown()
        with self._lock:
            live = list(self._live)
            workers = list(self._workers)
        for s in live:
            _shut(s)
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
        for w in workers:
            w.join(timeout=join_timeout_s)
        self._server = None
        self._thread = None

    def __enter__(self) -> "EgressFilterProxy":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- inspection --------------------------------------------------------
    @property
    def port(self) -> int:
        if self._server is None:
            raise ClientError("egress filter is not running")
        return int(self._server.server_address[1])

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def attempts(self) -> tuple[Attempt, ...]:
        with self._lock:
            return tuple(self._attempts)

    @property
    def refusals(self) -> tuple[Attempt, ...]:
        return tuple(a for a in self.attempts if not a.allowed)

    def live_thread_count(self) -> int:
        with self._lock:
            return sum(1 for w in self._workers if w.is_alive())

    # -- internals ---------------------------------------------------------
    def _record(self, attempt: Attempt) -> int:
        with self._lock:
            self._attempts.append(attempt)
            return len(self._attempts) - 1

    def _mark_connected(self, index: int) -> None:
        with self._lock:
            self._attempts[index] = replace(self._attempts[index], connected=True)

    def _track(self, s: socket.socket, thread: threading.Thread) -> None:
        with self._lock:
            self._live.add(s)
            self._workers.add(thread)

    def _untrack(self, s: socket.socket) -> None:
        with self._lock:
            self._live.discard(s)

    def _retire(self, thread: threading.Thread) -> None:
        with self._lock:
            self._workers.discard(thread)


# -- confinement -----------------------------------------------------------

#: Measured-good profile (Darwin 25.5, verified with curl and the real client):
#: everything but network is left alone, all TCP egress is denied, and exactly
#: one loopback port — the filter's — is reachable over TCP. The unix-socket
#: rules are what keep name resolution and the client's own IPC working under
#: `deny network*`; they also mean unix sockets stay reachable, so this profile
#: bounds TCP destinations only (see the module docstring).
_PROFILE = """(version 1)
(allow default)
(deny network*)
(allow network-outbound (remote unix-socket))
(allow network-bind (local unix-socket))
(allow network-bind (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:{port}"))
"""


def sandbox_profile(port: int) -> str:
    """The sandbox-exec profile confining a process to one loopback port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"port must be an int in 1..65535, got {port!r}")
    return _PROFILE.format(port=port)


def sandbox_argv(
    profile_path: str | os.PathLike[str],
    command: Sequence[str],
    *,
    sandbox_exec: str = SANDBOX_EXEC,
) -> list[str]:
    if not command:
        raise ValueError("command must be non-empty")
    return [sandbox_exec, "-f", str(profile_path), *command]


@contextmanager
def confined_argv(
    port: int,
    command: Sequence[str],
    *,
    sandbox_exec: str = SANDBOX_EXEC,
) -> Iterator[list[str]]:
    """Yield the argv that runs `command` confined to the proxy `port`.

    Fails closed (`ClientError`) when sandbox-exec is absent or the profile
    cannot be written: an unconfined client is not an acceptable fallback.
    """
    text = sandbox_profile(port)
    if not os.path.isfile(sandbox_exec) or not os.access(sandbox_exec, os.X_OK):
        raise ClientError(f"sandbox-exec not available at {sandbox_exec}; web-agent mode cannot confine the client")
    path: str | None = None
    try:
        fd, path = tempfile.mkstemp(prefix="subllm-egress-", suffix=".sb")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except OSError as e:
        if path is not None:  # a half-written profile must not be left behind
            Path(path).unlink(missing_ok=True)
        raise ClientError(f"could not write the sandbox profile ({e}); web-agent mode cannot confine the client") from e
    try:
        yield sandbox_argv(path, command, sandbox_exec=sandbox_exec)
    finally:
        Path(path).unlink(missing_ok=True)
