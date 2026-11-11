"""Opt-in rc.5 desktop HTTP client; credentials stay in memory.

No scheduler integration, proxies, redirects, retries, or WebSocket transport.
The caller supplies an official launch URL and must drain desktop stdout itself.
Short-lived HTTP proof only: this does not own an API lifecycle WebSocket.
An accepted prompt/cancel is a receipt, never proof of execution completion.
After UNKNOWN inspect the session log; do not replay the operation blindly.
"""
from __future__ import annotations

import http.client
import json
import math
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from uuid import uuid4


class DesktopConnectionError(RuntimeError):
    """Safe diagnostic: never includes request URL, cookie, or response body."""

    def __init__(self, code, *, outcome="REJECTED", http_status=None):
        super().__init__(f"Desktop HTTP client: {code}; outcome={outcome}")
        self.code = code
        self.outcome = outcome
        self.http_status = http_status


class DesktopRPCError(DesktopConnectionError):
    """A correlated native RPC error; details are available without logging them."""

    def __init__(self, error):
        super().__init__("RPC_ERROR", outcome="ERROR")
        self.remote_code = error["code"]
        self.remote_message = error["message"]
        self.details = error["details"]


def _url(value):
    try:
        if not isinstance(value, str) or not value or any(
            ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value
        ):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https")
                or parts.hostname not in ("127.0.0.1", "localhost", "::1")
                or parts.username is not None or parts.password is not None
                or parts.fragment):
            raise ValueError
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535 or parts.port == 0:
            raise ValueError
        host = parts.hostname
        authority = f"[{host}]" if host == "::1" else host
        default = 443 if parts.scheme == "https" else 80
        if port != default:
            authority += f":{port}"
        return parts, port, f"{parts.scheme}://{authority}"
    except (ValueError, TypeError):
        raise DesktopConnectionError("INVALID_URL") from None


def _identity(value):
    if not isinstance(value, str) or not value.strip():
        raise DesktopConnectionError("INVALID_ID")
    return value


class DesktopConnection:
    """Construct without I/O; connect once, explicitly enabled by the caller.

    Use rpc('session/list', {}) or the convenience methods below. Returned
    values are native result.value objects. This instance is not thread-safe.
    close() forgets credentials locally; it does not claim to revoke the Host's
    API operator (without WS it may remain until Host shutdown).
    """

    _METHODS = frozenset({"session/list", "session/create", "session/prompt",
                          "session/cancel", "session/state", "session/wait", "session/events",
                          "workspace/create", "agentPresets/read"})

    def __init__(self, launch_url, *, enabled=False, timeout=10):
        if enabled is not True:
            raise DesktopConnectionError("OPT_IN_REQUIRED")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise DesktopConnectionError("INVALID_TIMEOUT")
        parts, self._port, self._origin = _url(launch_url)
        try:
            query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise DesktopConnectionError("INVALID_LAUNCH_URL") from None
        if (parts.path not in ("", "/") or len(query) != 1
                or query[0][0] != "token" or not query[0][1]
                or any(ord(c) <= 32 or ord(c) == 127 for c in query[0][1])):
            raise DesktopConnectionError("INVALID_LAUNCH_URL")
        self._scheme, self._host = parts.scheme, parts.hostname
        self._token = query[0][1]
        self._cookie = None
        self._timeout = timeout
        self._state = "NEW"

    @classmethod
    def restore(cls, origin, cookie, *, timeout=10):
        """Restore a previously authenticated loopback session without I/O.

        The controller persists this material only in its private service
        directory and binds it to the desktop process identity separately.
        """
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise DesktopConnectionError("INVALID_TIMEOUT")
        parts, port, normalized = _url(origin)
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise DesktopConnectionError("INVALID_ORIGIN")
        if not isinstance(cookie, str) or not 3 <= len(cookie) <= 8192:
            raise DesktopConnectionError("INVALID_COOKIE")
        parsed = SimpleCookie()
        try:
            parsed.load(cookie)
        except CookieError:
            raise DesktopConnectionError("INVALID_COOKIE") from None
        if len(parsed) != 1 or any(ord(c) < 32 or ord(c) == 127 for c in cookie):
            raise DesktopConnectionError("INVALID_COOKIE")
        morsel = next(iter(parsed.values()))
        if not morsel.value:
            raise DesktopConnectionError("INVALID_COOKIE")
        value = cls.__new__(cls)
        value._scheme, value._host = parts.scheme, parts.hostname
        value._port, value._origin = port, normalized
        value._token = None
        value._cookie = cookie
        value._timeout = timeout
        value._state = "CONNECTED"
        return value

    @property
    def origin(self):
        """Credential-free origin; never a launch URL."""
        return self._origin

    def private_auth(self):
        """Return controller-only persistence material; never expose on a tool."""
        if self._state != "CONNECTED" or not self._cookie:
            raise DesktopConnectionError("NOT_CONNECTED")
        return {"origin": self._origin, "cookie": self._cookie}

    def __repr__(self):
        return f"DesktopConnection(state={self._state!r})"

    def _exchange(self, method, path, body=None):
        # http.client connects directly and never consults environment proxies.
        factory = (http.client.HTTPSConnection if self._scheme == "https"
                   else http.client.HTTPConnection)
        connection = factory(self._host, self._port, timeout=self._timeout)
        headers = {"Accept": "application/json"}
        if self._cookie is not None:
            headers["Cookie"] = self._cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status, response_headers = response.status, response.getheaders()
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise DesktopConnectionError("RESPONSE_TOO_LARGE", outcome="UNKNOWN")
            return status, response_headers, data
        except (OSError, http.client.HTTPException, ValueError):
            raise DesktopConnectionError("TRANSPORT_ERROR", outcome="UNKNOWN") from None
        finally:
            connection.close()

    def connect(self):
        if self._state != "NEW":
            raise DesktopConnectionError("CONNECT_ALREADY_ATTEMPTED")
        self._state = "CONNECTING"
        path = "/connect?" + urlencode({"token": self._token, "interactions": "none"})
        try:
            status, headers, _ = self._exchange("GET", path)
            if status != 302:
                raise DesktopConnectionError("CONNECT_HTTP_ERROR", http_status=status)
            locations = [v for k, v in headers if k.lower() == "location"]
            cookies = [v for k, v in headers if k.lower() == "set-cookie"]
            if len(locations) != 1 or len(cookies) != 1:
                raise DesktopConnectionError("INVALID_BOOTSTRAP")
            location = locations[0]
            # Validate Location but never follow it (it is a WS destination).
            if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in location):
                raise DesktopConnectionError("INVALID_BOOTSTRAP_REDIRECT")
            target, _, origin = _url(urljoin(self.origin + "/connect", location))
            if (origin != self.origin or target.path != "/api/remote.mux"
                    or target.query or target.fragment):
                raise DesktopConnectionError("INVALID_BOOTSTRAP_REDIRECT")
            parsed = SimpleCookie()
            try:
                parsed.load(cookies[0])
            except CookieError:
                raise DesktopConnectionError("INVALID_BOOTSTRAP_COOKIE") from None
            if len(parsed) != 1:
                raise DesktopConnectionError("INVALID_BOOTSTRAP_COOKIE")
            morsel = next(iter(parsed.values()))
            cookie = f"{morsel.key}={morsel.coded_value}"
            if not morsel.value or any(ord(c) < 32 or ord(c) == 127 for c in cookie):
                raise DesktopConnectionError("INVALID_BOOTSTRAP_COOKIE")
            self._cookie = cookie
            self._state = "CONNECTED"
            return self
        except DesktopConnectionError as error:
            self._state = "UNKNOWN" if error.outcome == "UNKNOWN" else "FAILED"
            raise
        finally:
            self._token = None

    def rpc(self, method, request):
        """One unary attempt. No retries, including after UNKNOWN.

        Native HTTP errors and uncorrelatable responses are conservatively
        UNKNOWN. A valid native error is exposed as DesktopRPCError.
        """
        if self._state != "CONNECTED":
            raise DesktopConnectionError("NOT_CONNECTED")
        if method not in self._METHODS or not isinstance(request, dict):
            raise DesktopConnectionError("INVALID_RPC")
        if method == "session/prompt":
            _identity(request.get("requestId"))
        rpc_id = str(uuid4())
        # rc.5 typert.host.js preserves list(_request, signal)'s wire name.
        parameter = "_request" if method == "session/list" else "request"
        args = {parameter: request}
        if method == "agentPresets/read":
            args = {"agentPreset": _identity(request.get("agentPreset"))}
        envelope = {"type": "client-request", "rpcId": rpc_id, "method": method,
                    "payload": {"args": args}}
        try:
            body = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise DesktopConnectionError("INVALID_RPC_INPUT") from None
        status, _, data = self._exchange("POST", "/api/" + method, body)
        if status != 200:
            raise DesktopConnectionError("RPC_HTTP_ERROR", outcome="UNKNOWN", http_status=status)
        try:
            value = json.loads(data)
            if (not isinstance(value, dict) or value.get("type") != "server-response"
                    or value.get("rpcId") != rpc_id):
                raise ValueError
            result = value["result"]
            if not isinstance(result, dict):
                raise ValueError
            if result.get("ok") is True and "value" in result and "error" not in result:
                return result["value"]
            error = result.get("error")
            if (result.get("ok") is False and "value" not in result
                    and isinstance(error, dict) and isinstance(error.get("code"), str)
                    and isinstance(error.get("message"), str)
                    and isinstance(error.get("details"), dict)):
                raise DesktopRPCError(error)
            raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise DesktopConnectionError("RPC_PROTOCOL_ERROR", outcome="UNKNOWN") from None

    def list_sessions(self):
        return self.rpc("session/list", {})

    def resolve_workspace(self, path):
        """Resolve the registered Workspace for a directory, creating if absent."""
        return self.rpc("workspace/create", {"path": _identity(str(path))})

    def create(self, session_id=None, cwd=None, workspace_id=None, agent_preset=None):
        if cwd is not None and workspace_id is not None:
            raise DesktopConnectionError("CONFLICTING_LOCATION")
        request = {key: value for key, value in
                   (("sessionId", session_id), ("cwd", cwd), ("workspaceId", workspace_id),
                    ("agentPreset", agent_preset))
                   if value is not None}
        for value in request.values():
            _identity(value)
        return self.rpc("session/create", request)

    def prompt(self, session_id, text, *, request_id, mode="queue"):
        if mode not in ("queue", "steer") or not isinstance(text, str) or not text.strip():
            raise DesktopConnectionError("INVALID_PROMPT")
        return self.rpc("session/prompt", {"sessionId": _identity(session_id),
                        "requestId": _identity(request_id), "mode": mode,
                        "content": [{"type": "text", "text": text}]})



    def cancel(self, session_id):
        return self.rpc("session/cancel", {"sessionId": _identity(session_id)})

    def state(self, session_id):
        return self.rpc("session/state", {"sessionId": _identity(session_id)})

    def wait(self, session_id, *, timeout_ms=8000, after_seq=-1, max_messages=1):
        if (type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000
                or type(after_seq) is not int or after_seq < -1
                or type(max_messages) is not int or not 1 <= max_messages <= 1000):
            raise DesktopConnectionError("INVALID_WAIT")
        return self.rpc("session/wait", {
            "sessionId": _identity(session_id),
            "timeoutMs": timeout_ms,
            "afterSeq": after_seq,
            "maxMessages": max_messages,
        })

    def events(self, session_id, *, through_seq=None, max_messages=1000,
               min_messages=1, min_turns=1):
        if (
            (through_seq is not None and (type(through_seq) is not int or through_seq < -1))
            or type(max_messages) is not int or not 1 <= max_messages <= 1000
            or type(min_messages) is not int or not 1 <= min_messages <= max_messages
            or type(min_turns) is not int or min_turns < 1
        ):
            raise DesktopConnectionError("INVALID_EVENTS")
        request = {
            "address": {"kind": "session", "sessionId": _identity(session_id)},
            "maxMessages": max_messages,
            "turnWindow": {"minMessages": min_messages, "minTurns": min_turns},
        }
        if through_seq is not None:
            request["throughSeq"] = through_seq
        return self.rpc("session/events", request)

    def close(self):
        self._token = self._cookie = None
        self._state = "CLOSED"

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.close()
