"""A dependency-free Streamable HTTP MCP server for the RenderDoc Python API.

The embedded Python shipped with RenderDoc is intentionally used directly. This
module therefore avoids the external MCP SDK and uses only Python 3.6-compatible
standard-library modules.
"""

from __future__ import print_function

import base64
import http.server
import json
import os
import socketserver
import threading
import traceback

import renderdoc as rd

from .renderdoc_api import RenderDocTools


SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")
MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_PORT = 8765
try:
    DEFAULT_PORT = int(os.environ.get("RENDERDOC_MCP_PORT", DEFAULT_PORT))
except ValueError:
    DEFAULT_PORT = 8765


TOOLS = [
    {
        "name": "get_capture_info",
        "description": "Return metadata and the current event for the active capture.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_actions",
        "description": "List frame actions, including draw, dispatch, copy, and marker actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draws_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
            },
        },
    },
    {
        "name": "list_resources",
        "description": "List textures and buffers in the active capture.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["all", "textures", "buffers"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
            },
        },
    },
    {
        "name": "get_resource_usage",
        "description": "Return the event-level uses recorded for a resource.",
        "inputSchema": {
            "type": "object",
            "required": ["resource"],
            "properties": {"resource": {"type": "string"}},
        },
    },
    {
        "name": "get_pipeline_state",
        "description": "Return the current pipeline outputs, shader IDs, and bound resources.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_texture_data",
        "description": "Read raw bytes from a texture subresource as base64.",
        "inputSchema": {
            "type": "object",
            "required": ["resource"],
            "properties": {
                "resource": {"type": "string"},
                "mip": {"type": "integer", "minimum": 0, "default": 0},
                "slice": {"type": "integer", "minimum": 0, "default": 0},
                "plane": {"type": "integer", "minimum": 0, "default": 0},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216, "default": 4194304},
            },
        },
    },
    {
        "name": "get_buffer_data",
        "description": "Read raw bytes from a buffer as base64.",
        "inputSchema": {
            "type": "object",
            "required": ["resource"],
            "properties": {
                "resource": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 16777216, "default": 4096},
            },
        },
    },
    {
        "name": "get_pixel_history",
        "description": "Return the recorded modifications for one texture pixel.",
        "inputSchema": {
            "type": "object",
            "required": ["resource", "x", "y"],
            "properties": {
                "resource": {"type": "string"},
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "mip": {"type": "integer", "minimum": 0, "default": 0},
                "slice": {"type": "integer", "minimum": 0, "default": 0},
                "plane": {"type": "integer", "minimum": 0, "default": 0},
                "event_id": {"type": "integer", "minimum": 1},
            },
        },
    },
]


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "RenderDocMCP/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        # RenderDoc's console is shared with the Python scripting UI. Keep HTTP
        # requests quiet unless the extension itself reports an error.
        return

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (
            "null",
            "http://localhost",
            "http://127.0.0.1",
            "http://[::1]",
        ) or origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")

    def _send(self, status, content_type, body=b"", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "http://localhost")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, "text/plain; charset=utf-8", extra_headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, MCP-Protocol-Version, MCP-Session-Id",
        })

    def _check_protocol_header(self):
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            self._send(400, "application/json", _json_bytes({
                "error": "unsupported MCP protocol version",
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
            }))
            return False
        return True

    def do_GET(self):
        if self.path != "/mcp":
            self._send(404, "application/json", _json_bytes({"error": "not found"}))
            return
        if not self._origin_allowed():
            self._send(403, "application/json", _json_bytes({"error": "origin rejected"}))
            return
        if not self._check_protocol_header():
            return
        # The server is stateless and does not send unsolicited notifications.
        # A short-lived SSE response still satisfies clients that probe GET before
        # issuing their first POST.
        self._send(200, "text/event-stream", b": renderdoc-mcp\n\n")

    def do_POST(self):
        if self.path != "/mcp":
            self._send(404, "application/json", _json_bytes({"error": "not found"}))
            return
        if not self._origin_allowed():
            self._send(403, "application/json", _json_bytes({"error": "origin rejected"}))
            return
        if not self._check_protocol_header():
            return

        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._send(413, "application/json", _json_bytes({"error": "invalid request size"}))
            return

        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send(400, "application/json", _json_bytes({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }))
            return

        try:
            response = self.server.extension_service.handle(request)
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(error)},
            }
            print("RenderDoc MCP request failed:\n{}".format(traceback.format_exc()))

        if response is None:
            self._send(202, "text/plain; charset=utf-8")
        else:
            headers = {}
            if isinstance(request, dict) and request.get("method") == "initialize":
                protocol_version = response.get("result", {}).get("protocolVersion")
                if protocol_version:
                    headers["MCP-Protocol-Version"] = protocol_version
            self._send(200, "application/json", _json_bytes(response), extra_headers=headers)

    def do_DELETE(self):
        if self.path == "/mcp":
            self._send(405, "application/json", _json_bytes({"error": "sessions are not used"}),
                       extra_headers={"Allow": "GET, POST, OPTIONS"})
        else:
            self._send(404, "application/json", _json_bytes({"error": "not found"}))


class ExtensionService(object):
    def __init__(self, context, status_callback=None):
        self.context = context
        self.tools = RenderDocTools(context)
        self.server = None
        self.thread = None
        self.port = None
        self.lock = threading.RLock()
        self.call_lock = threading.RLock()
        self.status_callback = status_callback

    def _notify_status(self, state):
        if self.status_callback is not None:
            try:
                self.status_callback(state, self.endpoint())
            except Exception:
                print("RenderDoc MCP status update failed:\n{}".format(traceback.format_exc()))

    def start(self):
        with self.lock:
            if self.server is not None:
                print("RenderDoc MCP is already running at {}".format(self.endpoint()))
                return

            try:
                server = _ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), _MCPHandler)
            except OSError:
                # Keep the convenient default when it is free, while allowing a
                # second RenderDoc instance to start on an ephemeral local port.
                try:
                    server = _ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
                except OSError as error:
                    self._notify_status("error")
                    print("RenderDoc MCP failed to listen: {}".format(error))
                    return
            server.extension_service = self
            self.server = server
            self.port = server.server_address[1]
            self.thread = threading.Thread(target=server.serve_forever, name="RenderDocMCP")
            self.thread.daemon = True
            self.thread.start()
            self._notify_status("running")
            print("RenderDoc MCP listening at {}".format(self.endpoint()))

    def stop(self):
        with self.lock:
            server = self.server
            thread = self.thread
            self.server = None
            self.thread = None
            self.port = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        self._notify_status("stopped")
        if server is not None:
            print("RenderDoc MCP stopped.")

    def endpoint(self):
        if self.port is None:
            return "stopped"
        return "http://127.0.0.1:{}/mcp".format(self.port)

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}

        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            requested_version = params.get("protocolVersion")
            protocol_version = (requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS
                                 else SUPPORTED_PROTOCOL_VERSIONS[0])
            return self._result(request_id, {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "renderdoc-mcp", "version": "0.1.0"},
                "instructions": "Use get_capture_info before capture-dependent tools.",
            })
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "method not found: {}".format(method)}}

    @staticmethod
    def _result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call_tool(self, request_id, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            with self.call_lock:
                result = self.tools.call(name, arguments)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return self._result(request_id, {"content": [{"type": "text", "text": text}],
                                             "structuredContent": result,
                                             "isError": False})
        except Exception as error:
            print("RenderDoc MCP tool '{}' failed:\n{}".format(name, traceback.format_exc()))
            return self._result(request_id, {"content": [{"type": "text", "text": str(error)}],
                                             "isError": True})
