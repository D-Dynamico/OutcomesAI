# The HTTP provider client maps every failure to a fixed error_class and never keeps
# provider text (design section 4, section 7; DECISIONS.md D5).
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


def test_client_timeout_is_enforced_against_a_hanging_provider():
    # The worker's own timeout, independent of the provider
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Hang(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(3)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hang)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = HttpSummaryClient(f"http://127.0.0.1:{server.server_port}", 0.3)
        start = time.monotonic()
        with pytest.raises(TransientError) as exc:
            c.generate_summary("t")
        assert exc.value.error_class == "ai_timeout"
        assert time.monotonic() - start < 2
    finally:
        server.shutdown()
