"""Local HTTP fixtures only; no desktop, credentials, or model requests."""
import json
import os
import socket
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from collab.desktop_connection import (
    DesktopConnection, DesktopConnectionError, DesktopRPCError,
)
from tests.support import fixture


class DesktopConnectionTests(unittest.TestCase):
    def test_launch_url_validation_without_network(self):
        with self.assertRaises(DesktopConnectionError) as caught:
            DesktopConnection("http://127.0.0.1/?token=fixture")
        self.assertEqual(caught.exception.code, "OPT_IN_REQUIRED")
        invalid = [
            "ftp://127.0.0.1/?token=x", "http://example.org/?token=x",
            "http://127.0.0.2/?token=x", "http://127.0.0.1.evil/?token=x",
            "http://user:secret@localhost/?token=x", "http://localhost/?token=",
            "http://localhost/?token=x&token=y", "http://localhost/?token=x&other=y",
            "http://localhost/?token=x#fragment", "http://localhost/path?token=x",
            "http://localhost:0/?token=x", "http://localhost:99999/?token=x",
            "http://localhost/\n?token=x", "http://localhost\\evil/?token=x",
        ]
        with patch("collab.desktop_connection.http.client.HTTPConnection") as network:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(DesktopConnectionError):
                    DesktopConnection(url, enabled=True)
            network.assert_not_called()
        for url in ("http://localhost/?token=x", "https://[::1]/?token=x"):
            DesktopConnection(url, enabled=True).close()

    def test_bootstrap_envelopes_and_proxy_bypass(self):
        with fixture() as (url, records), patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
        }):
            client = DesktopConnection(url, enabled=True)
            self.assertEqual(records, [])
            self.assertNotIn("fixture-launch", repr(client))
            with client:
                self.assertEqual(client.origin, url.split("/?")[0])
                self.assertEqual(client.list_sessions(), {"items": []})
                client.create(session_id="s1", cwd="C:/fixture")
                client.create(workspace_id="workspace-1")
                self.assertEqual(client.prompt("s1", "调研", request_id="stable-prompt"),
                                 {"accepted": True})
                client.state("s1")
                client.wait("s1")
                client.cancel("s1")
            self.assertIsNone(client._cookie)
            self.assertIsNone(client._token)
            self.assertEqual(len(records), 8)
            self.assertEqual(parse_qs(urlsplit(records[0][1]).query),
                             {"token": ["fixture-launch"], "interactions": ["none"]})
            self.assertNotIn("Cookie", records[0][2])
            for _, path, headers, body in records[1:]:
                self.assertEqual(headers["Cookie"], "fixture=memory-only")
                payload = json.loads(body)
                self.assertEqual(payload["type"], "client-request")
                self.assertEqual(path, "/api/" + payload["method"])
                self.assertEqual(set(payload["payload"]), {"args"})
                expected = {"_request"} if payload["method"] == "session/list" else {"request"}
                self.assertEqual(set(payload["payload"]["args"]), expected)
                self.assertTrue(payload["rpcId"])
            self.assertEqual(json.loads(records[1][3])["payload"], {"args": {"_request": {}}})
            requests = [next(iter(json.loads(r[3])["payload"]["args"].values())) for r in records[1:]]
            self.assertEqual(requests[1], {"sessionId": "s1", "cwd": "C:/fixture"})
            self.assertEqual(requests[2], {"workspaceId": "workspace-1"})
            self.assertEqual(requests[3], {"sessionId": "s1", "requestId": "stable-prompt",
                                         "mode": "queue", "content": [{"type": "text", "text": "调研"}]})
            self.assertNotIn("completed", client.__dict__)

    def test_method_wire_envelopes(self):
        with self.subTest(method="agentPresets/read"):
            def responder(handler, records):
                if handler.command == "GET":
                    return 302, {"Location": "./api/remote.mux", "Set-Cookie": "fixture=private"}, b""
                envelope = json.loads(records[-1][3])
                self.assertEqual(envelope["payload"]["args"], {"agentPreset": "standard"})
                return 200, {}, json.dumps({
                    "type": "server-response", "rpcId": envelope["rpcId"],
                    "result": {"ok": True, "value": {"agentPreset": "standard", "content": "fixture"}},
                }).encode()
            with fixture(responder) as (url, _records), DesktopConnection(url, enabled=True) as client:
                self.assertEqual(client.rpc("agentPresets/read", {"agentPreset": "standard"})["content"],
                                 "fixture")

        with self.subTest(method="workspace/create"):
            with fixture() as (url, records), DesktopConnection(url, enabled=True) as client:
                client.resolve_workspace("C:/fixture")
                envelope = json.loads(records[-1][3])
                self.assertEqual(envelope["method"], "workspace/create")
                self.assertEqual(envelope["payload"], {"args": {"request": {"path": "C:/fixture"}}})

        with self.subTest(method="session/list"):
            with fixture() as (url, records), DesktopConnection(url, enabled=True) as client:
                for wrong_args in ({"request": {}}, {"request": {}, "_request": {}}):
                    wrong = {"type": "client-request", "rpcId": "wrong-list-key",
                             "method": "session/list", "payload": {"args": wrong_args}}
                    status, _, data = client._exchange("POST", "/api/session/list", json.dumps(wrong).encode())
                    response = json.loads(data)
                    self.assertEqual(status, 200)
                    self.assertEqual(response["rpcId"], "wrong-list-key")
                    self.assertIs(response["result"]["ok"], False)
                    self.assertEqual(response["result"]["error"]["code"], "gateway/arguments-invalid")
                self.assertEqual(client.rpc("session/list", {}), {"items": []})
                self.assertEqual(client.list_sessions(), {"items": []})
                for record in records[-2:]:
                    self.assertEqual(json.loads(record[3])["payload"], {"args": {"_request": {}}})

    def test_bootstrap_failures_are_validated_and_redacted(self):
        for location in ("http://127.0.0.1:1/api/remote.mux",
                         "http://example.org/api/remote.mux",
                         "//example.org/api/remote.mux",
                         "/wrong", "/api/remote.mux?token=echo"):
            with self.subTest(kind="redirect", location=location), fixture(
                lambda _h, _r, location=location: (
                    302, {"Location": location, "Set-Cookie": "fixture=x"}, b"")
            ) as (url, records):
                client = DesktopConnection(url, enabled=True)
                with self.assertRaises(DesktopConnectionError):
                    client.connect()
                self.assertEqual(len(records), 1)
                self.assertIsNone(client._cookie)
                self.assertIsNone(client._token)

        for status, headers in ((401, {}), (302, {"Location": "/api/remote.mux"}),
                                (302, {"Location": "/api/remote.mux", "Set-Cookie": "fixture="})):
            with self.subTest(kind="auth", status=status), fixture(
                lambda _h, _r, status=status, headers=headers: (status, headers, b"fixture-launch")
            ) as (url, records):
                client = DesktopConnection(url, enabled=True)
                with self.assertRaises(DesktopConnectionError) as caught:
                    client.connect()
                self.assertNotIn("fixture-launch", str(caught.exception))
                with self.assertRaises(DesktopConnectionError):
                    client.connect()
                self.assertEqual(len(records), 1)

        with self.subTest(kind="transport"):
            client = DesktopConnection("http://localhost/?token=fixture-secret", enabled=True)
            with patch("collab.desktop_connection.http.client.HTTPConnection") as factory:
                factory.return_value.request.side_effect = OSError("fixture-secret cookie=secret")
                with self.assertRaises(DesktopConnectionError) as caught:
                    client.connect()
                self.assertEqual(caught.exception.outcome, "UNKNOWN")
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                factory.return_value.request.assert_called_once()
                factory.return_value.close.assert_called_once()
            self.assertIsNone(client._token)

    def test_rpc_protocol_and_http_failures_are_unknown_without_retry(self):
        with fixture() as (url, records):
            with DesktopConnection(url, enabled=True) as client:
                for body in (b"not JSON", b"{}", json.dumps({
                    "type": "server-response", "rpcId": "wrong",
                    "result": {"ok": True, "value": {}},
                }).encode()):
                    with self.subTest(kind="body", body=body):
                        with patch.object(client, "_exchange", return_value=(200, [], body)) as exchange:
                            with self.assertRaises(DesktopConnectionError) as caught:
                                client.list_sessions()
                            self.assertEqual(caught.exception.outcome, "UNKNOWN")
                            exchange.assert_called_once()

                for result in ({"ok": 1, "value": {}}, {"ok": True},
                               {"ok": False, "error": {}},
                               {"ok": True, "value": {}, "error": {}}):
                    def response(_method, _path, data, result=result):
                        return 200, [], json.dumps({
                            "type": "server-response", "rpcId": json.loads(data)["rpcId"],
                            "result": result,
                        }).encode()
                    with self.subTest(kind="result", result=result):
                        with patch.object(client, "_exchange", side_effect=response):
                            with self.assertRaises(DesktopConnectionError) as caught:
                                client.list_sessions()
                            self.assertEqual(caught.exception.outcome, "UNKNOWN")

                for status in (302, 401, 429, 500):
                    with self.subTest(kind="http", status=status):
                        with patch.object(client, "_exchange", return_value=(
                                status, [("Location", "http://example.org")], b"")) as exchange:
                            with self.assertRaises(DesktopConnectionError) as caught:
                                client.prompt("s1", "fixture", request_id="stable-id")
                            self.assertEqual(caught.exception.outcome, "UNKNOWN")
                            self.assertEqual(caught.exception.http_status, status)
                            exchange.assert_called_once()
            self.assertEqual(len(records), 1)

    def test_correlated_native_error_preserved_without_logging_message(self):
        with fixture() as (url, _records), DesktopConnection(url, enabled=True) as client:
            def response(_method, _path, data):
                return 200, [], json.dumps({
                    "type": "server-response", "rpcId": json.loads(data)["rpcId"],
                    "result": {"ok": False, "error": {
                        "code": "session/not-found", "message": "private fixture detail",
                        "details": {"sessionId": "s1"}}},
                }).encode()
            with patch.object(client, "_exchange", side_effect=response):
                with self.assertRaises(DesktopRPCError) as caught:
                    client.state("s1")
                self.assertEqual(caught.exception.remote_code, "session/not-found")
                self.assertEqual(caught.exception.details, {"sessionId": "s1"})
                self.assertNotIn("private fixture detail", str(caught.exception))

    def test_lost_response_after_submission_unknown_no_retry(self):
        def respond(handler, _records):
            if handler.command == "GET":
                return 302, {"Location": "/api/remote.mux", "Set-Cookie": "fixture=x"}, b""
            handler.send_response(200)
            handler.send_header("Content-Length", "999")
            handler.end_headers()
            handler.wfile.write(b'{"type":')
            handler.wfile.flush()
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return None
        with fixture(respond) as (url, records), DesktopConnection(url, enabled=True) as client:
            with self.assertRaises(DesktopConnectionError) as caught:
                client.prompt("s1", "fixture", request_id="stable-id")
            self.assertEqual(caught.exception.outcome, "UNKNOWN")
            self.assertEqual(len(records), 2)
            self.assertEqual(json.loads(records[-1][3])["payload"]["args"]["request"]["requestId"],
                             "stable-id")

    def test_private_auth_round_trip_and_invalid_restore_material(self):
        with fixture() as (url, _records), DesktopConnection(url, enabled=True) as client:
            auth = client.private_auth()
            self.assertEqual(auth["origin"], url.split("/?")[0])
            self.assertIn("fixture=", auth["cookie"])
            restored = DesktopConnection.restore(auth["origin"], auth["cookie"])
            self.assertEqual(restored.list_sessions(), {"items": []})
            restored.close()
        for origin, cookie in (
            ("http://example.org", "fixture=x"),
            ("http://127.0.0.1/?token=x", "fixture=x"),
            ("http://127.0.0.1", ""),
            ("http://127.0.0.1", "fixture=x; other=y"),
        ):
            with self.subTest(origin=origin, cookie=cookie):
                with self.assertRaises(DesktopConnectionError):
                    DesktopConnection.restore(origin, cookie)

    def test_invalid_operations_rejected_before_send(self):
        with fixture() as (url, records), DesktopConnection(url, enabled=True) as client:
            with self.assertRaises(DesktopConnectionError):
                client.create(cwd="C:/fixture", workspace_id="workspace-1")
            with self.assertRaises(DesktopConnectionError):
                client.prompt("s1", "fixture", request_id="")
            with self.assertRaises(DesktopConnectionError):
                client.rpc("http://example.org", {})
            with self.assertRaises(DesktopConnectionError):
                client.rpc("session/prompt", {"sessionId": "s1"})
            self.assertEqual(len(records), 1)
        with self.assertRaises(DesktopConnectionError):
            client.list_sessions()

    def test_desktop_state_wait_and_events_contract(self):
        calls = []
        with fixture() as (url, records), DesktopConnection(url, enabled=True) as client:
            values = {
                "session/state": {"cursor": 5, "active": True, "running": True, "queue": [], "pendingInput": []},
                "session/wait": {"reason": "idle", "state": {"cursor": 9, "active": True, "running": False, "queue": [], "pendingInput": []}, "output": {}},
                "session/events": {"cursor": 9, "records": [{"type": "event", "event": {"seq": 9, "type": "turn/end"}}], "hasMore": False},
            }

            def response(method, path, data):
                envelope = json.loads(data)
                calls.append(envelope)
                return 200, [], json.dumps({
                    "type": "server-response", "rpcId": envelope["rpcId"],
                    "result": {"ok": True, "value": values[envelope["method"]]},
                }).encode()

            with patch.object(client, "_exchange", side_effect=response):
                self.assertEqual(client.state("s1")["cursor"], 5)
                waited = client.wait("s1", timeout_ms=3000, after_seq=4, max_messages=2)
                self.assertEqual(waited["reason"], "idle")
                events = client.events("s1", through_seq=9)
                self.assertEqual(events["cursor"], 9)
            self.assertEqual([x["method"] for x in calls],
                             ["session/state", "session/wait", "session/events"])
            self.assertEqual(calls[1]["payload"]["args"]["request"],
                             {"sessionId": "s1", "timeoutMs": 3000, "afterSeq": 4, "maxMessages": 2})
            self.assertEqual(calls[2]["payload"]["args"]["request"]["address"],
                             {"kind": "session", "sessionId": "s1"})
            for invalid in (
                lambda: client.wait("s1", timeout_ms=0),
                lambda: client.wait("s1", after_seq=-2),
                lambda: client.events("s1", through_seq=-2),
                lambda: client.events("s1", min_messages=0),
            ):
                with self.assertRaises(DesktopConnectionError):
                    invalid()
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
