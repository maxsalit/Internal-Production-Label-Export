"""
Vercel serverless handler for the Monday.com Job Ticket → Prelim Label webhook.
URL: /api/webhook_job_ticket

Triggered when a Job Ticket PDF is uploaded to the Job Ticket column.
Parses the PDF, generates one prelim label per 400 units per SKU, and
uploads a merged 3×7-grid PDF to the Prelim Label column.
"""
import json
import sys
from pathlib import Path

# Project root (api/webhook_job_ticket.py -> root = 2 levels up)
_root = Path(__file__).resolve().parent.parent
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
        _send_json(self, 200, {
            "status": "ok",
            "message": "Prelim label webhook ready. POST with boardId and pulseId.",
        })

    def do_POST(self):
        try:
            body_bytes = _read_body(self)
            body = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
        except json.JSONDecodeError:
            _send_json(self, 400, {"error": "Invalid JSON body"})
            return

        # Monday.com URL verification challenge
        if body.get("challenge") is not None:
            _send_json(self, 200, {"challenge": body["challenge"]})
            return

        from app import (
            JOB_TICKET_COLUMN_ID,
            _process_job_ticket,
        )

        try:
            event = body.get("event", body)
            item_id = event.get("pulseId") or event.get("itemId") or event.get("item_id")
            column_id = event.get("columnId") or event.get("column_id")

            if not item_id:
                _send_json(self, 200, {"status": "ignored", "reason": "no item_id"})
                return

            if column_id and column_id != JOB_TICKET_COLUMN_ID:
                _send_json(self, 200, {"status": "ignored", "reason": "wrong column"})
                return

            _process_job_ticket(int(item_id))
            _send_json(self, 200, {"status": "ok"})

        except Exception as exc:
            import logging, traceback
            logging.getLogger(__name__).error(
                f"[webhook_job_ticket] item={item_id} error: {exc}\n{traceback.format_exc()}"
            )
            _send_json(self, 200, {"status": "error", "message": str(exc)})
