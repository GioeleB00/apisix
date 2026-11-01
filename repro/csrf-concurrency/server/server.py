#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Small upstream for the local CSRF experiment; APISIX owns CSRF validation."""

import argparse
import json
from _thread import LockType
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

ASSET_DIR: Path = Path(__file__).resolve().parent
ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_BODY_BYTES: int = 4096
WRITE_COUNTS: dict[str, int] = {}
WRITE_LOCK: LockType = Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version: str = "HTTP/1.1"
    timeout: float = 5.0

    def send_body(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        self.send_body(status, json.dumps(payload).encode(), "application/json")

    def reject(self, status: HTTPStatus, message: str) -> None:
        # Close on errors so unread or malformed bodies cannot become requests.
        self.close_connection = True
        self.send_json(status, {"error": message})

    def do_GET(self) -> None:
        path: str = urlsplit(self.path).path
        if path == "/api/read":
            self.send_json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/stats":
            with WRITE_LOCK:
                counts: dict[str, int] = WRITE_COUNTS.copy()
            self.send_json(
                HTTPStatus.OK, {"writes": counts, "total_writes": sum(counts.values())}
            )
        elif path in ASSETS:
            # Serve only these fixed assets, never arbitrary filesystem paths.
            filename, content_type = ASSETS[path]
            try:
                body: bytes = (ASSET_DIR / filename).read_bytes()
            except OSError:
                self.reject(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "Static asset unavailable"
                )
                return
            self.send_body(HTTPStatus.OK, body, content_type)
        else:
            self.reject(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_OPTIONS(self) -> None:
        if urlsplit(self.path).path not in ("/api/read", "/api/write", "/api/stats"):
            self.reject(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, OPTIONS, POST")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/write":
            self.reject(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return
        if self.headers.get("Transfer-Encoding"):
            self.reject(HTTPStatus.BAD_REQUEST, "Chunked bodies are not supported")
            return
        lengths: list[str] = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.reject(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return
        if len(lengths) != 1:
            self.reject(HTTPStatus.BAD_REQUEST, "One Content-Length header is required")
            return
        try:
            length: int = int(lengths[0])
        except ValueError:
            self.reject(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length < 0:
            self.reject(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self.reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Body exceeds 4096 bytes")
            return
        if self.headers.get_content_type() != "application/json":
            self.reject(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use application/json")
            return
        try:
            raw_body: bytes = self.rfile.read(length)
        except TimeoutError:
            self.reject(HTTPStatus.REQUEST_TIMEOUT, "Request body timed out")
            return
        if len(raw_body) != length:
            self.reject(HTTPStatus.BAD_REQUEST, "Incomplete request body")
            return
        try:
            payload: object = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            self.reject(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
        if not isinstance(payload, dict):
            self.reject(HTTPStatus.BAD_REQUEST, "Use a JSON object")
            return
        operation_id: object = payload.get("operation_id")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            self.reject(
                HTTPStatus.BAD_REQUEST, "operation_id must be 1 to 128 characters"
            )
            return

        # Count every accepted POST. Reusing an ID is not deduplicated.
        with WRITE_LOCK:
            count: int = WRITE_COUNTS.get(operation_id, 0) + 1
            WRITE_COUNTS[operation_id] = count
        self.send_json(
            HTTPStatus.CREATED,
            {"operation_id": operation_id, "accepted_count": count},
        )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args: argparse.Namespace = parser.parse_args()
    host: str = args.host
    port: int = args.port
    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f"HTTP fixture listening on {host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
