# The HTTP provider client maps every failure to a fixed error_class and never keeps
# provider text (design section 4, section 7; DECISIONS.md D5).
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.summary.client import HttpSummaryClient, TransientError

LEAKY = "Patient says their name is Jane and ..."


def client_for(handler):
    return HttpSummaryClient("http://provider", 1, transport=httpx.MockTransport(handler))


def test_success_returns_summary():
    c = client_for(lambda req: httpx.Response(200, json={"summary": "S"}))
    assert c.generate_summary("t") == "S"


@pytest.mark.parametrize("status,error_class", [
    (429, "rate_limited"),
    (503, "ai_unavailable"),
    (504, "ai_timeout"),
    (500, "ai_unavailable"),
    (400, "internal"),
])
def test_status_codes_map_to_fixed_classes(status, error_class):
    c = client_for(lambda req: httpx.Response(status, text=LEAKY))
    with pytest.raises(TransientError) as exc:
        c.generate_summary("t")
    assert exc.value.error_class == error_class
    assert LEAKY not in str(exc.value)


@pytest.mark.parametrize("error,error_class", [
    (httpx.ReadTimeout(LEAKY), "ai_timeout"),
    (httpx.ConnectTimeout(LEAKY), "ai_timeout"),
    (httpx.ConnectError(LEAKY), "ai_unavailable"),
    (ValueError(LEAKY), "internal"),
])
def test_transport_failures_map_to_fixed_classes(error, error_class):
    def handler(req):
        raise error
    with pytest.raises(TransientError) as exc:
        client_for(handler).generate_summary("t")
    assert exc.value.error_class == error_class
    assert exc.value.__suppress_context__  # provider exception never printed in a traceback
    assert LEAKY not in str(exc.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"other": 1}),
    httpx.Response(200, json={"summary": 5}),
])
def test_malformed_success_is_internal(response):
    with pytest.raises(TransientError) as exc:
        client_for(lambda req: response).generate_summary("t")
    assert exc.value.error_class == "internal"


# ---------------------------------------------------------------- the total deadline (D21)
# Real sockets, so the timeouts under test are the real ones. Each misbehaving server would
# take about 3 s; the client's deadline is 0.5 s.

class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Hang(_Quiet):
    def do_POST(self):
        time.sleep(3)


class TrickleBody(_Quiet):
    # Headers promptly, then one body byte every 0.1 s: each read is well inside the per-read
    # timeout, so only a total deadline stops it
    def do_POST(self):
        body = b'{"summary": "' + b"x" * 30 + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for i in range(len(body)):
            self.wfile.write(body[i:i + 1])
            self.wfile.flush()
            time.sleep(0.1)


class TrickleHeaders(_Quiet):
    # The status line and headers themselves arrive one byte every 0.1 s
    def do_POST(self):
        body = b'{"summary": "s"}'
        crlf = bytes([13, 10])
        raw = (b"HTTP/1.1 200 OK" + crlf + b"Content-Type: application/json" + crlf
               + b"X-Padding: " + b"p" * 30 + crlf
               + b"Content-Length: " + str(len(body)).encode() + crlf + crlf + body)
        for i in range(len(raw)):
            self.wfile.write(raw[i:i + 1])
            self.wfile.flush()
            time.sleep(0.1)


@pytest.fixture
def serve():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


@pytest.mark.parametrize("handler", [Hang, TrickleBody, TrickleHeaders])
def test_total_deadline_bounds_the_whole_call(serve, handler):
    c = HttpSummaryClient(serve(handler), 0.5)
    start = time.monotonic()
    with pytest.raises(TransientError) as exc:
        c.generate_summary("t")
    assert exc.value.error_class == "ai_timeout"
    assert time.monotonic() - start < 1.5


def test_a_slow_but_in_time_response_succeeds(serve):
    class Slowish(_Quiet):
        def do_POST(self):
            time.sleep(0.2)
            body = b'{"summary": "ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    assert HttpSummaryClient(serve(Slowish), 1).generate_summary("t") == "ok"
