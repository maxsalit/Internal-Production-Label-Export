"""
Vercel serverless handler for Monday.com label webhook.
URL: /api/webhook/monday
"""
import json
import os
import sys
from pathlib import Path

# Ensure project root is on path so we can import app (api/webhook/monday.py -> root = 3 levels up)
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from http.server import BaseHTTPRequestHandler


def _read_body(handler):
    content_length = int(handler.headers.get("Content-Length") or 0)
    if content_length:
        return handler.rfile.read(content_length)
    return b""


def _send_json(handler, status: int, data: dict):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # URL verification (e.g. ?challenge=xxx)
        if "?" in self.path:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            challenge = (qs.get("challenge") or [None])[0]
            if challenge:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(challenge.encode("utf-8"))
                return
        _send_json(self, 400, {"error": "Use POST with JSON body"})

    def do_POST(self):
        try:
            body_bytes = _read_body(self)
            body = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
        except json.JSONDecodeError:
            _send_json(self, 400, {"error": "Invalid JSON body"})
            return

        # Challenge in body (webhook verification)
        if body.get("challenge") is not None:
            _send_json(self, 200, {"challenge": body["challenge"]})
            return

        # Run webhook logic (import here so VERCEL env is set before app loads OUTPUT_DIR)
        from app import (
            parse_webhook_payload,
            fetch_item,
            extract_label_data,
            build_label_pdf,
            ensure_output_dir,
            safe_filename,
            get_env_token,
            upload_label_to_monday,
            OUTPUT_DIR,
        )

        try:
            board_id, item_id = parse_webhook_payload(body)
        except ValueError as e:
            _send_json(self, 400, {"error": str(e)})
            return

        try:
            item = fetch_item(board_id, item_id)
        except ValueError as e:
            _send_json(self, 404, {"error": str(e)})
            return
        except Exception as e:
            _send_json(self, 502, {"error": str(e)})
            return

        label_data = extract_label_data(item)
        ensure_output_dir()
        safe_name = safe_filename(f"{label_data['client_name']}_{label_data['po_number']}_{item_id}")
        out_path = OUTPUT_DIR / f"{safe_name}.pdf"
        build_label_pdf(
            label_data["client_name"],
            label_data["item_description"],
            label_data["po_number"],
            out_path,
        )
        try:
            token = get_env_token()
            upload_label_to_monday(item_id, out_path, token)
        except Exception as e:
            _send_json(self, 502, {
                "ok": False,
                "message": "Label created but upload to Monday failed",
                "error": str(e),
                "file": out_path.name,
            })
            return

        _send_json(self, 200, {
            "ok": True,
            "message": "Label created and uploaded to Monday",
            "file": out_path.name,
        })
