# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""End-to-end tests for router.psgi, driven through a real public-inbox-netd.

router.psgi is Perl and the rest of this suite is Python, which is how a
reverse proxy that silently dropped chunked request bodies managed to ship:
nothing here exercised it at all. Rather than keep two harnesses, these
tests run the real daemon the container runs, point it at a stub backend
that reports exactly what it received, and speak HTTP to it over a plain
socket.

The socket matters. Every HTTP client worth using sends Content-Length,
and the bug was only reachable with `Transfer-Encoding: chunked', so a
test written with requests or urllib would have passed against the broken
router. Framing the request by hand is the whole point.

The stub answering with a digest of the body, rather than just a status,
is borrowed from public-inbox's own t/httpd-corner.t `/sha1' endpoint. A
proxy that forwards an empty body still gets a 200 back; only comparing
what arrived against what was sent catches it.
"""

import contextlib
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, List, NamedTuple, Tuple

import pytest

NETD = shutil.which('public-inbox-netd')
# What the archive fixture builds a real inbox with.
PI_TOOLS = ('public-inbox-init', 'public-inbox-mda')

# router.psgi loads these itself, and netd answers 500 rather than failing
# to start when one is absent -- which reads as a router bug instead of a
# missing package. Check for them up front so the skip says what to
# install. There is no CI for the Perl side, so a silent skip here is a
# test nobody runs.
PERL_MODULES = ('Plack::Middleware::ReverseProxy',)


def _missing() -> List[str]:
    missing: List[str] = [
        f'{tool} (package: public-inbox)' for tool in ('public-inbox-netd', *PI_TOOLS) if not shutil.which(tool)
    ]
    for module in PERL_MODULES:
        if subprocess.run(['perl', f'-M{module}', '-e1'], capture_output=True).returncode:
            missing.append(module)
    return missing


MISSING = _missing()

pytestmark = pytest.mark.skipif(
    bool(MISSING),
    reason='router.psgi needs these on the host: ' + ', '.join(MISSING),
)

ROUTER = Path(__file__).resolve().parent.parent / 'router.psgi'

# Long enough that a hang is unambiguous, short enough that a broken
# router fails the suite instead of stalling it. router.psgi's own proxy
# timeout is 300s, so anything that would have hung trips this first.
TIMEOUT = 15

# How long the stub holds a server-initiated stream open. It only has to
# outlast TIMEOUT for the hang to be real; the fixture releases it early on
# teardown so a passing run never waits for it.
HOLD = 60

# What the router fixture sets MCP_PROXY_DEADLINE to. Small, because the
# trickle test has to outlive it and the suite should not.
DEADLINE = 3
# What the router fixtures set ARCHIVE_UPSTREAM to, unless a test is about
# leaving it empty.
UPSTREAM = 'https://lore.kernel.org/all/'


class _BackendServer(http.server.ThreadingHTTPServer):
    """The stub server, with the two attributes its handler reaches for.

    Declared rather than stuck on at fixture time so a reader (and a type
    checker) can see that `seen' and `halt' are part of the contract
    between the fixture, the handler and the tests.
    """

    seen: List[Dict[str, Any]]
    halt: threading.Event


class _Backend(http.server.BaseHTTPRequestHandler):
    """Reports what it received, so a test can tell a dropped body apart
    from a delivered one. Deliberately does not de-chunk: the router is
    supposed to have done that and to have sent a Content-Length, so a
    request still framed as chunked when it arrives here is a bug worth
    seeing rather than quietly accommodating."""

    protocol_version = 'HTTP/1.1'

    @property
    def backend(self) -> _BackendServer:
        """BaseHTTPRequestHandler types self.server as BaseServer, which has
        neither of the two attributes the handler and the tests share."""
        assert isinstance(self.server, _BackendServer)
        return self.server

    def _respond(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        self.backend.seen.append(
            {
                'method': self.command,
                'path': self.path,
                'headers': {k.lower(): v for k, v in self.headers.items()},
                'body': body,
            }
        )
        payload = json.dumps(
            {
                'sha256': hashlib.sha256(body).hexdigest(),
                'length': len(body),
            }
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _trickle(self) -> None:
        """A response that never ends but is never silent.

        This is the case that took the container down and that no timeout
        caught: HTTP::Tiny applies its read timeout per read, so a backend
        dribbling a byte at a time resets it forever and the proxying
        worker is pinned for as long as the backend cares to continue.
        Promising more body than is ever sent is what a stalled streaming
        response looks like from the outside."""
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '1000000')
        self.end_headers()
        deadline = time.monotonic() + HOLD
        while not self.backend.halt.is_set() and time.monotonic() < deadline:
            try:
                self.wfile.write(b'.')
                self.wfile.flush()
            except OSError:  # the router gave up on us, which is the point
                return
            time.sleep(0.2)

    def do_GET(self) -> None:
        """GET /mcp stands in for the server-initiated stream. The real MCP
        SDK opens one whether or not it has anything to send and then holds
        it open indefinitely, so a stub that answered promptly would test
        the status code and miss the thing that actually killed the
        container: a response that never ends, blocking the process that
        proxied it. Hold it here, and let the fixture release it."""
        if self.path.startswith('/mcp'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.backend.halt.wait(HOLD)
            return
        self._respond()

    def do_POST(self) -> None:
        if 'trickle' in self.path:
            return self._trickle()
        self._respond()

    do_DELETE = _respond

    def log_message(self, format: str, *args: Any) -> None:  # keep the test output readable
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope='module')
def backend() -> Iterator[_BackendServer]:
    server = _BackendServer(('127.0.0.1', 0), _Backend)
    server.daemon_threads = True
    server.seen = []
    server.halt = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.halt.set()
    server.shutdown()
    server.server_close()


@contextlib.contextmanager
def _netd(
    backend: _BackendServer, tmp: Path, pi_config: Path, **settings: str
) -> Generator[Tuple[str, int], None, None]:
    """A real public-inbox-netd serving router.psgi, proxying to the stub.

    -W0 on purpose: one process makes a blocked worker impossible to
    mistake for a busy one, and there is no concurrency being tested here.
    WEB_WORKERS is what the container tunes; this is a single request at a
    time by design.
    """
    host, port = str(backend.server_address[0]), str(backend.server_address[1])
    listen = _free_port()

    env = dict(os.environ)
    env.update(
        {
            'PI_CONFIG': str(pi_config),
            'GROKMIRROR_TOPLEVEL': str(tmp / 'repos'),
            'DASHBOARD_HOST': host,
            'DASHBOARD_PORT': port,
            'MCP_HOST': host,
            'MCP_PORT': port,
            'MCP_PROXY_DEADLINE': str(DEADLINE),
            'ARCHIVE_UPSTREAM': UPSTREAM,
            'PULL_STAMP_PATH': str(tmp / 'lore-updated'),
            **settings,
        }
    )
    assert NETD  # the module-level skip already covers its absence
    proc = subprocess.Popen(
        [NETD, '-W0', '-l', f'http://127.0.0.1:{listen}/?env.PI_CONFIG={pi_config},psgi={ROUTER}'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    for _ in range(100):
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ''
            pytest.fail(f'netd exited: {out}')
        try:
            with socket.create_connection(('127.0.0.1', listen), 0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail('netd never started listening')

    try:
        yield ('127.0.0.1', listen)
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope='module')
def router(backend: _BackendServer, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[str, int]]:
    tmp = tmp_path_factory.mktemp('router')
    pi_config = tmp / 'pi_config'  # deliberately absent: /lore 404s, / still works
    with _netd(backend, tmp, pi_config) as router:
        yield router


def request(router: Tuple[str, int], raw: bytes) -> Tuple[int, Dict[str, str], bytes]:
    """Send handcrafted request bytes and read one response back.

    Written against a socket rather than an HTTP client library because
    the framing is the thing under test -- no client will emit a chunked
    body for a payload this small, and that is exactly the case that broke.
    """
    with socket.create_connection(router, TIMEOUT) as sock:
        sock.settimeout(TIMEOUT)
        sock.sendall(raw)
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get('content-length') or 0)
        while len(body) < length:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        return status, headers, body


def chunked_post(path: str, payload: bytes, host: str) -> bytes:
    """A POST framed the way a real MCP client frames it: no Content-Length,
    body delimited by chunk sizes and a terminating zero-length chunk."""
    return (
        (
            f'POST {path} HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            'Content-Type: application/json\r\n'
            'Accept: application/json, text/event-stream\r\n'
            'Transfer-Encoding: chunked\r\n'
            '\r\n'
            f'{len(payload):x}\r\n'
        ).encode()
        + payload
        + b'\r\n0\r\n\r\n'
    )


def sized_post(path: str, payload: bytes, host: str) -> bytes:
    return (
        f'POST {path} HTTP/1.1\r\n'
        f'Host: {host}\r\n'
        'Content-Type: application/json\r\n'
        'Accept: application/json, text/event-stream\r\n'
        f'Content-Length: {len(payload)}\r\n'
        '\r\n'
    ).encode() + payload


PAYLOAD = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()


# Forwarding request bodies


def test_a_chunked_post_arrives_with_its_body_intact(router: Tuple[str, int], backend: _BackendServer) -> None:
    """A chunked request has no Content-Length. Sizing the body from one
    forwards nothing at all -- the backend then waits for a body that is
    never coming, holding a worker until the proxy timeout gives up."""
    host = f'{router[0]}:{router[1]}'
    status, _, body = request(router, chunked_post('/mcp', PAYLOAD, host))

    assert status == 200
    assert json.loads(body) == {
        'sha256': hashlib.sha256(PAYLOAD).hexdigest(),
        'length': len(PAYLOAD),
    }


def test_a_sized_post_arrives_with_its_body_intact(router: Tuple[str, int], backend: _BackendServer) -> None:
    """The control: the framing that always worked, and the reason the bug
    survived every test done by hand. curl sends this one."""
    host = f'{router[0]}:{router[1]}'
    status, _, body = request(router, sized_post('/mcp', PAYLOAD, host))

    assert status == 200
    assert json.loads(body)['sha256'] == hashlib.sha256(PAYLOAD).hexdigest()


def test_a_chunked_body_larger_than_one_chunk_is_forwarded_whole(
    router: Tuple[str, int], backend: _BackendServer
) -> None:
    """Reading to EOF has to keep reading. A single-chunk body would pass
    even if only the first read were kept."""
    payload = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'pad': 'x' * 200000}}).encode()
    host = f'{router[0]}:{router[1]}'
    half = len(payload) // 2
    raw = (
        (
            f'POST /mcp HTTP/1.1\r\nHost: {host}\r\n'
            'Content-Type: application/json\r\n'
            'Transfer-Encoding: chunked\r\n\r\n'
            f'{half:x}\r\n'
        ).encode()
        + payload[:half]
        + (f'\r\n{len(payload) - half:x}\r\n'.encode())
        + payload[half:]
        + b'\r\n0\r\n\r\n'
    )

    status, _, body = request(router, raw)

    assert status == 200
    assert json.loads(body)['length'] == len(payload)
    assert json.loads(body)['sha256'] == hashlib.sha256(payload).hexdigest()


def test_an_empty_body_is_not_invented(router: Tuple[str, int], backend: _BackendServer) -> None:
    """A GET carries no body and must not acquire one on the way through."""
    host = f'{router[0]}:{router[1]}'
    request(router, f'GET / HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert backend.seen[-1]['body'] == b''


# Hop-by-hop framing


def test_the_clients_framing_headers_are_not_forwarded(router: Tuple[str, int], backend: _BackendServer) -> None:
    """Transfer-Encoding describes how the client framed its body on its
    own connection. public-inbox has de-chunked by the time we forward, and
    passing `chunked' along beside the Content-Length HTTP::Tiny computes
    describes the same bytes two contradictory ways."""
    host = f'{router[0]}:{router[1]}'
    request(router, chunked_post('/mcp', PAYLOAD, host))
    seen = backend.seen[-1]['headers']

    assert 'transfer-encoding' not in seen
    assert seen['content-length'] == str(len(PAYLOAD))


# The server-initiated stream


def test_the_router_refuses_to_proxy_a_server_initiated_stream(
    router: Tuple[str, int], backend: _BackendServer
) -> None:
    """Streamable HTTP lets a client open GET /mcp for messages the server
    starts by itself. This one never does, and the SDK behind it opens the
    stream anyway and holds it open forever -- which, proxied, blocks the
    process for the whole proxy timeout. The spec's answer is 405."""
    host = f'{router[0]}:{router[1]}'
    before = len(backend.seen)
    status, headers, _ = request(router, f'GET /mcp HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert status == 405
    assert 'POST' in headers['allow']
    assert len(backend.seen) == before, 'GET /mcp must never reach the backend'


def test_the_router_still_answers_after_a_refused_stream(router: Tuple[str, int], backend: _BackendServer) -> None:
    """The failure this guards against was not one bad response but a dead
    container: the blocked process stopped serving the dashboard, cgit, git
    clones and the mail listeners too."""
    host = f'{router[0]}:{router[1]}'
    request(router, f'GET /mcp HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
    status, _, _ = request(router, f'GET / HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert status == 200


def test_a_trickling_backend_is_cut_off_at_the_deadline(router: Tuple[str, int]) -> None:
    """The outage this exists to prevent.

    A backend that never finishes but never goes quiet defeats a read
    timeout completely, and proxy_to blocks the worker it runs in, so two
    of these once stopped the archive, cgit, git clones and the mail
    listeners at once. The router has to give up on its own.
    """
    host = f'{router[0]}:{router[1]}'
    started = time.monotonic()
    status, _, _ = request(router, sized_post('/mcp?trickle=1', PAYLOAD, host))
    elapsed = time.monotonic() - started
    # 504 rather than a truncated 200: nothing had been written yet, so the
    # status line was still ours to choose and it should say what happened.
    assert status == 504
    assert elapsed >= DEADLINE, f'gave up after {elapsed:.1f}s, before the deadline'
    assert elapsed < DEADLINE + 5, f'took {elapsed:.1f}s to give up on a {DEADLINE}s deadline'


def test_the_worker_is_free_again_after_a_trickle_is_cut_off(router: Tuple[str, int]) -> None:
    """Cutting the response off is only half of it.

    netd runs here with -W0, so there is exactly one process to block. If
    the deadline closed the client's connection but left the worker
    reading from the backend, this request would hang -- which is what the
    container actually did.
    """
    host = f'{router[0]}:{router[1]}'
    status, _, body = request(router, sized_post('/mcp', PAYLOAD, host))
    assert status == 200
    assert json.loads(body)['length'] == len(PAYLOAD)


def test_a_bare_lore_redirects_to_the_archive(router: Tuple[str, int]) -> None:
    """Without the trailing slash, public-inbox's WWW gets an empty path,
    matches none of its routes and falls back to cgit -- so /lore showed
    the cgit index instead of the mail archive."""
    host = f'{router[0]}:{router[1]}'
    status, headers, _ = request(router, f'GET /lore HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert status == 301
    assert headers['location'] == '/lore/'


def test_a_bare_lore_redirect_keeps_the_query(router: Tuple[str, int]) -> None:
    host = f'{router[0]}:{router[1]}'
    status, headers, _ = request(router, f'GET /lore?q=foo HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert status == 301
    assert headers['location'] == '/lore/?q=foo'


def test_the_archive_itself_is_not_redirected(router: Tuple[str, int]) -> None:
    host = f'{router[0]}:{router[1]}'
    status, _, _ = request(router, f'GET /lore/ HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())

    assert status != 301


def test_an_ordinary_mcp_request_is_not_cut_short(router: Tuple[str, int]) -> None:
    """The deadline must not be a ceiling on normal traffic: the stub
    answers instantly and has to come back whole."""
    host = f'{router[0]}:{router[1]}'
    status, _, body = request(router, chunked_post('/mcp', PAYLOAD, host))
    assert status == 200
    assert json.loads(body)['sha256'] == hashlib.sha256(PAYLOAD).hexdigest()


# Saying the archive is partial
#
# The archive only has the tracked subsystems, so every /lore answer says
# so and names where the rest is (X-Archive-Coverage, X-Archive-Upstream).
# A client that gets a 404 here then knows to ask upstream instead of
# reporting the thread as missing.

MSGID = 'm1@example.org'
MESSAGE = f"""\
From: A Maintainer <a@example.org>
To: test@example.org
Subject: a thread the mirror has
Message-ID: <{MSGID}>
Date: Thu, 01 Oct 2026 00:00:00 +0000

body
"""


class Lore(NamedTuple):
    addr: Tuple[str, int]
    stamp: Path


@pytest.fixture(scope='module')
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A public-inbox config with one real inbox, holding one message.

    Real rather than stubbed, because the headers have to reach responses
    PublicInbox::WWW builds itself, streamed ones included, and only WWW
    knows how it builds them.
    """
    tmp = tmp_path_factory.mktemp('archive')
    pi_config = tmp / 'config'
    env = {**os.environ, 'PI_CONFIG': str(pi_config)}
    subprocess.run(
        ['public-inbox-init', '-V2', 'test', str(tmp / 'test'), 'http://example.org/test', 'test@example.org'],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['public-inbox-mda', '--no-precheck'],
        env={**env, 'ORIGINAL_RECIPIENT': 'test@example.org'},
        input=MESSAGE.encode(),
        check=True,
        capture_output=True,
    )
    return pi_config


@pytest.fixture(scope='module')
def lore(backend: _BackendServer, archive: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lore]:
    tmp = tmp_path_factory.mktemp('lore')
    with _netd(backend, tmp, archive) as addr:
        yield Lore(addr, tmp / 'lore-updated')


@pytest.fixture
def stamp(lore: Lore) -> Iterator[Path]:
    """The pull stamp the lore router reads, gone again after the test."""
    yield lore.stamp
    lore.stamp.unlink(missing_ok=True)


def write_stamp(path: Path, text: str) -> None:
    """Replace the stamp the way kgl-pull-loop.sh does, by a rename."""
    new = path.with_name(path.name + '.new')
    new.write_text(text, encoding='utf-8')
    new.replace(path)


def get(router: Tuple[str, int], path: str, method: str = 'GET') -> Tuple[int, Dict[str, str]]:
    host = f'{router[0]}:{router[1]}'
    status, headers, _ = request(
        router, f'{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode()
    )
    return status, headers


@pytest.mark.parametrize(
    'path, expected',
    [
        pytest.param('/lore', 301, id='bare-lore-redirect'),
        pytest.param('/lore/test/', 200, id='inbox-index'),
        # Both of these are streamed, a code-ref response rather than a
        # finished one, which is where a header wrapper usually goes wrong.
        pytest.param(f'/lore/test/{MSGID}/raw', 200, id='raw-message'),
        pytest.param(f'/lore/test/{MSGID}/t.mbox.gz', 200, id='thread-mbox'),
        # The one that matters most: "not here" has to say "but maybe there".
        pytest.param('/lore/nosuch/', 404, id='not-found'),
    ],
)
def test_every_lore_answer_says_the_archive_is_partial(lore: Lore, path: str, expected: int) -> None:
    status, headers = get(lore.addr, path)

    assert status == expected
    assert headers['x-archive-coverage'] == 'partial'
    assert headers['x-archive-upstream'] == UPSTREAM


def test_a_head_on_lore_has_the_headers_too(lore: Lore) -> None:
    """liblore asks with HEAD when it only wants to know if a thread is here."""
    status, headers = get(lore.addr, f'/lore/test/{MSGID}/raw', 'HEAD')

    assert status == 200
    assert headers['x-archive-coverage'] == 'partial'


@pytest.mark.parametrize(
    'method, path',
    [
        pytest.param('GET', '/cgit/', id='cgit'),
        pytest.param('GET', '/', id='dashboard'),
        pytest.param('GET', '/api/summary', id='dashboard-api'),
        pytest.param('DELETE', '/mcp', id='mcp'),
    ],
)
def test_nothing_outside_lore_says_it(lore: Lore, method: str, path: str) -> None:
    """The dashboard, cgit and MCP aren't mail archives. A client that
    took the headers from them would learn an upstream for the wrong
    thing."""
    _, headers = get(lore.addr, path, method)

    assert 'x-archive-coverage' not in headers
    assert 'x-archive-upstream' not in headers


def test_an_empty_upstream_sends_neither_header(backend: _BackendServer, archive: Path, tmp_path: Path) -> None:
    """ARCHIVE_UPSTREAM='' is the way back to looking like a full archive."""
    write_stamp(tmp_path / 'lore-updated', '1790000000\n')
    with _netd(backend, tmp_path, archive, ARCHIVE_UPSTREAM='') as addr:
        for path in ('/lore', '/lore/test/', '/lore/nosuch/'):
            _, headers = get(addr, path)

            assert 'x-archive-coverage' not in headers, path
            assert 'x-archive-upstream' not in headers, path


def test_the_pull_stamp_is_sent_as_updated(lore: Lore, stamp: Path) -> None:
    write_stamp(stamp, '1790000000\n')

    _, headers = get(lore.addr, '/lore/nosuch/')

    assert headers['x-archive-coverage'] == 'partial; updated=1790000000'


def test_a_new_stamp_is_seen_at_once(lore: Lore, stamp: Path) -> None:
    """The router caches the stamp, and the two writes here land in the
    same second -- so an mtime alone can't tell them apart."""
    write_stamp(stamp, '1790000000\n')
    get(lore.addr, '/lore/nosuch/')
    write_stamp(stamp, '1790000600\n')

    _, headers = get(lore.addr, '/lore/nosuch/')

    assert headers['x-archive-coverage'] == 'partial; updated=1790000600'


def test_no_stamp_means_no_updated(lore: Lore, stamp: Path) -> None:
    """No good pull yet. Leaving `updated' out is safe: the client asks
    upstream."""
    write_stamp(stamp, '1790000000\n')
    get(lore.addr, '/lore/nosuch/')
    stamp.unlink()

    _, headers = get(lore.addr, '/lore/nosuch/')

    assert headers['x-archive-coverage'] == 'partial'


@pytest.mark.parametrize(
    'junk',
    [
        pytest.param('', id='empty'),
        pytest.param('soon\n', id='a-word'),
        pytest.param('1790000000abc\n', id='trailing-junk'),
        pytest.param('-1790000000\n', id='negative'),
        pytest.param('1790000000\n1790000600\n', id='two-lines'),
    ],
)
def test_a_junk_stamp_means_no_updated(lore: Lore, stamp: Path, junk: str) -> None:
    write_stamp(stamp, junk)

    _, headers = get(lore.addr, '/lore/nosuch/')

    assert headers['x-archive-coverage'] == 'partial'
