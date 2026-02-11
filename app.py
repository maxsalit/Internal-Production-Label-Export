"""
Monday.com Label Export

Webhook server: when Job Status changes to "Preparing for Shipping", Monday.com
calls this app. We fetch the item's Client Name, Item Description (pulse name),
and PO#, then generate a PDF label and save it to the labels/ folder.
"""

import json
import os
import re
from pathlib import Path

import requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph

app = Flask(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_FILE_API_URL = "https://api.monday.com/v2/file"
# On Vercel (serverless) we must write to /tmp; locally use labels/ folder
OUTPUT_DIR = Path("/tmp") if os.environ.get("VERCEL") else (Path(__file__).resolve().parent / "labels")

# Monday column to upload the generated label PDF into (when webhook runs)
LABEL_FILE_COLUMN_ID = "file_mm0fzm60"

# Column matching: by title or by column ID (for label/lookup columns)
CLIENT_NAME_TITLES = ("Client Name", "Client", "client name", "client")
CLIENT_NAME_COLUMN_IDS = ("lookup_mkv6padj",)  # Client (lookup) column
PO_TITLES = ("PO#", "PO Number", "PO", "po#", "po number")


def get_env_token():
    token = os.environ.get("MONDAY_API_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "MONDAY_API_TOKEN is not set. Create a .env file or export the variable. See .env.example."
        )
    return token


def _monday_request(query: str, variables: dict, token: str) -> dict:
    """Send GraphQL request to Monday.com; returns data payload or raises."""
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": "2024-01",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError("Monday.com API error: " + str(data["errors"]))
    return data.get("data", {})


def fetch_item(board_id: int, item_id: int) -> dict:
    """Load a single item from Monday.com with column values. Returns item dict or raises."""
    token = get_env_token()
    item_id_str = str(item_id)
    # Board no longer has "items" – use items_page and paginate until we find the item
    query_page = """
    query ($boardId: ID!) {
      boards(ids: [$boardId]) {
        items_page(limit: 100) {
          cursor
          items {
            id
            name
            column_values {
              id
              type
              text
              value
              column { title }
              ... on MirrorValue {
                display_value
                mirrored_items {
                  linked_item { id name }
                }
              }
            }
          }
        }
      }
    }
    """
    query_next = """
    query ($cursor: String!) {
      next_items_page(cursor: $cursor, limit: 100) {
        cursor
        items {
          id
          name
          column_values {
            id
            type
            text
            value
            column { title }
            ... on MirrorValue {
              display_value
              mirrored_items { linked_item { id name } }
            }
          }
        }
      }
    }
    """
    variables = {"boardId": board_id}
    data = _monday_request(query_page, variables, token)
    boards = data.get("boards") or []
    if not boards:
        raise ValueError(f"Board {board_id} not found or no access")

    page = boards[0].get("items_page") or {}
    cursor = page.get("cursor")
    items = page.get("items") or []

    while True:
        for it in items:
            if str(it.get("id")) == item_id_str:
                return it
        if not cursor:
            break
        data = _monday_request(query_next, {"cursor": cursor}, token)
        page = data.get("next_items_page") or {}
        cursor = page.get("cursor")
        items = page.get("items") or []

    raise ValueError(f"Item {item_id} not found on board {board_id}")


def extract_label_data(item: dict) -> dict:
    """From Monday item, get client_name, item_description, po_number."""
    client_name = ""
    po_number = ""
    for col in item.get("column_values") or []:
        col_id = (col.get("id") or "").strip()
        title = (col.get("column") or {}).get("title") or col.get("title") or ""
        title = str(title).strip()
        text = (col.get("text") or "").strip()
        if col_id in CLIENT_NAME_COLUMN_IDS or title in CLIENT_NAME_TITLES:
            if text:
                client_name = text
            else:
                # Mirror column (Client): use display_value (shows "Glass House"), not linked item name (C&D)
                client_name = (col.get("display_value") or "").strip()
                if not client_name:
                    # Fallback: linked item name (for non-mirror or if display_value missing)
                    mirrored = col.get("mirrored_items") or []
                    if mirrored and isinstance(mirrored[0], dict):
                        linked = (mirrored[0] or {}).get("linked_item") or {}
                        client_name = (linked.get("name") or "").strip()
                if not client_name:
                    raw = col.get("value")
                    if isinstance(raw, str) and raw.strip().startswith("{"):
                        try:
                            val = json.loads(raw)
                            client_name = val.get("label") or val.get("label_name") or (val.get("labels") or [None])[0] or ""
                            if isinstance(client_name, dict):
                                client_name = client_name.get("name") or client_name.get("label") or ""
                            client_name = str(client_name).strip()
                        except (json.JSONDecodeError, TypeError):
                            pass
        if title in PO_TITLES:
            po_number = text
    item_description = (item.get("name") or "").strip()
    return {
        "client_name": client_name or "(No Client Name)",
        "item_description": item_description or "(No Item Description)",
        "po_number": po_number or "(No PO#)",
    }


def safe_filename(s: str, max_len: int = 80) -> str:
    """Make a string safe for use in a filename."""
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = s.strip() or "unnamed"
    return s[:max_len]


def build_label_pdf(client_name: str, item_description: str, po_number: str, out_path: Path) -> None:
    """Create one PDF label with only the values (right-hand part): client name, description, PO#."""
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "LabelTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        spaceAfter=8,
    )
    normal_style = ParagraphStyle(
        "LabelNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        spaceAfter=8,
    )

    po_display = f"PO# {po_number}" if po_number and not str(po_number).upper().startswith("PO#") else po_number
    desc_para = item_description.replace("\n", "<br/>")

    # Right-hand part only: values in order (client name bold, then description, then PO#)
    story = [
        Paragraph(client_name, title_style),
        Paragraph(desc_para, normal_style),
        Paragraph(po_display, normal_style),
    ]
    doc.build(story)


def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def upload_label_to_monday(item_id: int, file_path: Path, token: str) -> None:
    """Upload the PDF file to the item's label file column on Monday.com. Raises on failure."""
    mutation = (
        "mutation ($file: File!, $item_id: ID!, $column_id: String!) {"
        " add_file_to_column(item_id: $item_id, column_id: $column_id, file: $file) { id }"
        "}"
    )
    variables = {"item_id": str(item_id), "column_id": LABEL_FILE_COLUMN_ID}
    # Multipart: query, variables, map (maps file part to variables.file), and the file
    data = {
        "query": mutation,
        "variables": json.dumps(variables),
        "map": json.dumps({"file": "variables.file"}),
    }
    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "application/pdf")}
        resp = requests.post(
            MONDAY_FILE_API_URL,
            data=data,
            files=files,
            headers={"Authorization": token},
            timeout=60,
        )
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise RuntimeError("Monday file upload error: " + str(result["errors"]))


def parse_webhook_payload(body: dict) -> tuple[int, int]:
    """Extract board_id and item_id from webhook JSON. Returns (board_id, item_id)."""
    # Monday automation webhooks may send pulseId/boardId at top level or under payload/event
    for candidate in (body, body.get("payload") or {}, body.get("event") or {}):
        board_id = candidate.get("boardId") or candidate.get("board_id")
        item_id = candidate.get("pulseId") or candidate.get("pulse_id") or candidate.get("itemId") or candidate.get("item_id")
        if board_id and item_id:
            return int(board_id), int(item_id)
    raise ValueError(
        "Webhook payload must include boardId and pulseId (or itemId). "
        f"Received keys: {list(body.keys())}"
    )


@app.route("/webhook/monday", methods=["POST", "GET"])
def monday_webhook():
    """
    Monday.com calls this URL when your automation runs (e.g. Job Status -> Preparing for Shipping).
    Expects JSON body with boardId and pulseId (or board_id, pulse_id / itemId, item_id).
    """
    # URL verification: some integrations send GET with challenge
    if request.method == "GET":
        challenge = request.args.get("challenge")
        if challenge:
            return challenge
        return jsonify({"error": "Use POST with JSON body"}), 400

    # Challenge in POST body (e.g. Monday webhook verification)
    body = request.get_json(silent=True) or {}
    if "challenge" in body:
        return jsonify({"challenge": body["challenge"]})

    try:
        board_id, item_id = parse_webhook_payload(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        item = fetch_item(board_id, item_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Request failed: {e}"}), 502

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
        return jsonify({
            "ok": False,
            "message": "Label created but upload to Monday failed",
            "error": str(e),
            "file": out_path.name,
        }), 502
    return jsonify({
        "ok": True,
        "message": "Label created and uploaded to Monday",
        "file": out_path.name,
        "path": str(out_path),
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def main():
    import sys
    if "MONDAY_API_TOKEN" not in os.environ:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
    port = int(os.environ.get("PORT", 5000))
    print(f"Label webhook server running at http://0.0.0.0:{port}")
    print("  POST /webhook/monday  <- point Monday.com automation here")
    print("  GET  /health          <- health check")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
