#!/usr/bin/env python3
"""Engram · LOCAL DEV SHIM ONLY -- stands in for API Gateway so the real Lambda handler code
(`workers/approvals/handler.py`) can be exercised end-to-end from the actual browser dashboard
without deploying to AWS. This is a verification harness, not part of the real deployment --
`infra/` is the real API Gateway + Lambda, this is just a way to prove the code between them
(the handler, workers/common/db.py, the Next.js proxy route) is wired correctly before deploying.

Checks a fixed local dev API key (matches what you put in dashboard/.env.local) so the same
"X-Api-Key" contract the real API Gateway enforces is exercised here too, not skipped.

    python scripts/local_approvals_api_shim.py
    # then set in dashboard/.env.local:
    #   ENGRAM_APPROVALS_API_URL=http://localhost:8787
    #   ENGRAM_APPROVALS_API_KEY=local-dev-key
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "workers"))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from approvals.handler import handler as lambda_handler

LOCAL_DEV_API_KEY = "local-dev-key"
PATH_RE = re.compile(r"^/approvals/([^/]+)$")


class Shim(BaseHTTPRequestHandler):
    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming convention)
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        match = PATH_RE.match(self.path)
        if not match:
            self.send_response(404)
            self._cors_headers()
            self.end_headers()
            return

        if self.headers.get("X-Api-Key") != LOCAL_DEV_API_KEY:
            self.send_response(401)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "invalid api key"}).encode())
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode() if length else "{}"

        event = {
            "httpMethod": "POST",
            "pathParameters": {"approval_id": match.group(1)},
            "body": raw_body,
        }
        result = lambda_handler(event, None)

        self.send_response(result["statusCode"])
        for k, v in result["headers"].items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(result["body"].encode())

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        print(f"  {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    port = 8787
    print(f"LOCAL DEV SHIM (not real infra) listening on http://localhost:{port}")
    print(f"API key: {LOCAL_DEV_API_KEY}")
    print("Set in dashboard/.env.local:")
    print(f"  ENGRAM_APPROVALS_API_URL=http://localhost:{port}")
    print(f"  ENGRAM_APPROVALS_API_KEY={LOCAL_DEV_API_KEY}")
    HTTPServer(("localhost", port), Shim).serve_forever()
