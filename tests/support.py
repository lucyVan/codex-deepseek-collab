"""Shared local test fixtures. No desktop, credentials, network, or model calls."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from collab.common import CollabError


@contextmanager
def fixture(responder=None, sessions=None):
    """Loopback HTTP fixture that mimics the DSH rc.5 generated RPC contract."""
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            records.append((self.command, self.path, dict(self.headers), body))
            if responder is not None:
                response = responder(self, records)
                if response is None:
                    return
                status, headers, data = response
            elif self.command == "GET":
                status, headers, data = 302, {
                    "Location": "./api/remote.mux",
                    "Set-Cookie": "fixture=memory-only; Path=/; HttpOnly; SameSite=Strict",
                }, b""
            else:
                envelope = json.loads(body)
                value = (
                    {"accepted": True} if envelope["method"] == "session/prompt"
                    else {"items": sessions or []} if envelope["method"] == "session/list"
                    else {"items": []}
                )
                # Contract transcribed from rc.5's generated typert.host.js:
                # list(_request: SessionListRequest, signal: AbortSignal).
                expected = {"_request"} if envelope["method"] == "session/list" else {"request"}
                if (set(envelope["payload"]) != {"args"}
                        or set(envelope["payload"]["args"]) != expected):
                    result = {"ok": False, "error": {
                        "code": "gateway/arguments-invalid",
                        "message": "args fields do not match the descriptor", "details": {},
                    }}
                else:
                    result = {"ok": True, "value": value}
                data = json.dumps({"type": "server-response", "rpcId": envelope["rpcId"],
                                   "result": result}).encode()
                status, headers = 200, {}
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin + "/?token=fixture-launch", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_collab_code(testcase, code, function, *args, **kwargs):
    """Call a CollabError-raising function and assert its fixed public code."""
    with testcase.assertRaises(CollabError) as caught:
        function(*args, **kwargs)
    testcase.assertEqual(caught.exception.code, code)
    return caught.exception
