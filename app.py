import os
import re
import json
import logging
import tempfile
from pathlib import Path

import pdfplumber
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_FILE_API_URL = "https://api.monday.com/v2/file"
PACKING_SLIP_COLUMN_ID = "file_mkv0jhmj"
SHIPPING_LABELS_COLUMN_ID = "file_mm0fzm60"
JOB_STATUS_COLUMN_ID = "status__1"   # "Preparing for Shipping" trigger


# ---------------------------------------------------------------------------
# Monday.com API helpers
# ---------------------------------------------------------------------------

def get_token():
    token = os.environ.get("MONDAY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MONDAY_API_TOKEN environment variable is not set")
    return token


def monday_request(query, variables=None):
    token = get_token()
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2024-01",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(MONDAY_API_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data


def get_packing_slip_url(item_id):
    """Return the (url, name) of the most recent file in the Packing Slip column."""
    query = """
    query GetPackingSlip($itemId: ID!) {
      items(ids: [$itemId]) {
        column_values(ids: ["file_mkv0jhmj"]) {
          ... on FileValue {
            files {
              ... on FileAssetValue {
                asset {
                  public_url
                  name
                }
              }
            }
          }
        }
      }
    }
    """
    data = monday_request(query, {"itemId": str(item_id)})
    items = data.get("data", {}).get("items", [])
    if not items:
        raise RuntimeError(f"Item {item_id} not found")
    col_values = items[0].get("column_values", [])
    if not col_values:
        raise RuntimeError("Packing Slip column not found on item")
    files = col_values[0].get("files", [])
    if not files:
        raise RuntimeError("No files in Packing Slip column")
    # Most recent file is last in the list
    latest = files[-1].get("asset", {})
    url = latest.get("public_url")
    name = latest.get("name", "packing_slip.pdf")
    if not url:
        raise RuntimeError("Could not retrieve file URL from Packing Slip column")
    return url, name


def download_file(url, dest_path):
    """Download a file using a pre-signed public URL (no auth header needed)."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    log.info(f"Downloaded file to {dest_path} ({len(resp.content)} bytes)")


def upload_labels_to_monday(item_id, pdf_path):
    """Upload the labels PDF to the Shipping Labels column."""
    token = get_token()
    mutation = """
    mutation AddFile($itemId: ID!, $columnId: String!, $file: File!) {
      add_file_to_column(item_id: $itemId, column_id: $columnId, file: $file) {
        id
      }
    }
    """
    variables = {
        "itemId": str(item_id),
        "columnId": SHIPPING_LABELS_COLUMN_ID,
    }
    with open(pdf_path, "rb") as f:
        file_bytes = f.read()

    filename = Path(pdf_path).name
    resp = requests.post(
        MONDAY_FILE_API_URL,
        headers={"Authorization": token, "API-Version": "2024-01"},
        files={
            "query": (None, mutation),
            "variables": (None, json.dumps(variables)),
            "map": (None, json.dumps({"file": ["variables.file"]})),
            "file": (filename, file_bytes, "application/pdf"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise RuntimeError(f"Upload error: {result['errors']}")
    log.info(f"Uploaded {filename} to Shipping Labels column for item {item_id}")
    return result


# ---------------------------------------------------------------------------
# PDF Parsing
# ---------------------------------------------------------------------------

def parse_packing_slip(pdf_path):
    """
    Parse a Sunshine Enclosures packing slip PDF.

    Returns:
        {
            "customer_name": str,
            "po_number": str,
            "line_items": [{"description": str, "carton_qty": int, "qty_per_carton": int}]
        }
    """
    customer_name = "(Unknown Customer)"
    po_number = "(Unknown PO)"
    line_items = []

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""

        # --- Extract customer name ---
        # The packing slip text reads: "CUSTOMER Popped Candy\nNAME:"
        # So the customer name follows "CUSTOMER" on the same line.
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for line in lines:
            if line.upper().startswith("CUSTOMER"):
                # Remove "CUSTOMER" prefix and optional "NAME:" to get the name
                rest = line[len("CUSTOMER"):].strip()
                rest = re.sub(r"^NAME\s*:\s*", "", rest, flags=re.IGNORECASE).strip()
                if rest and not any(
                    kw in rest.upper()
                    for kw in ("NAME:", "ORDER DATE", "INVOICE", "PURCHASE")
                ):
                    customer_name = rest
                    break

        # --- Extract tables (used for both PO# and line items) ---
        tables = page.extract_tables()

        # The main table structure (Table 0):
        #   Row 0: merged header "ORDER DATE Invoice # PURCHASE ORDER # CUSTOMER CONTACT"
        #   Row 1: [date, invoice#, PO#, contact, None, None]
        #   Row 2: merged column headers
        #   Row 3+: line item data  [carton_qty, description, None, qty_per_carton, total_qty, pallet]
        if tables:
            main_table = max(tables, key=lambda t: len(t))

            # PO# is in the first data row, column index 2
            for row in main_table:
                cells = [str(c).strip() if c else "" for c in row]
                if len(cells) >= 3 and cells[2] and not any(
                    kw in cells[0].upper() for kw in ("ORDER DATE", "CARTON", "PURCHASE")
                ):
                    # Check this row has a date-like first cell and PO-like third cell
                    if re.match(r"\d+/\d+/\d+", cells[0]) and cells[2]:
                        po_number = cells[2]
                        break

            line_items = _parse_line_items_table(main_table)

    log.info(
        f"Parsed packing slip: customer='{customer_name}', PO='{po_number}', "
        f"{len(line_items)} line item rows"
    )
    return {
        "customer_name": customer_name,
        "po_number": po_number,
        "line_items": line_items,
    }


def _parse_line_items_table(table):
    """
    Extract line items from the Sunshine Enclosures packing slip table.

    The table structure as extracted by pdfplumber:
      Row 0: merged header ("ORDER DATE Invoice # PURCHASE ORDER # ...")
      Row 1: [date, invoice#, PO#, contact, None, None]
      Row 2: merged column header (contains "Carton Qty", "DESCRIPTION", "Item QTY per Carton")
      Row 3+: [carton_qty, description, None, qty_per_carton, total_qty, pallet#]

    Column indices in data rows:
      0 = Carton Qty
      1 = Description
      3 = Item QTY per Carton  (index 2 is always None due to PDF table layout)
    """
    COL_CARTON = 0
    COL_DESC = 1
    COL_QTY_PER = 3

    items = []
    data_started = False

    for row in table:
        cells = [str(c).strip() if c else "" for c in row]
        if not any(cells):
            continue

        # The column header row has "Carton Qty" and "DESCRIPTION" in the first cell
        # (it's a merged/spanned cell in the PDF). Once found, next rows are data.
        if not data_started:
            cell0 = cells[0].upper()
            if "CARTON" in cell0 and "DESCRIPTION" in cell0:
                data_started = True
            continue

        carton_qty_str = cells[COL_CARTON] if COL_CARTON < len(cells) else ""
        description = cells[COL_DESC] if COL_DESC < len(cells) else ""
        qty_per_str = cells[COL_QTY_PER] if COL_QTY_PER < len(cells) else ""

        # Skip TOTAL row
        if "TOTAL" in description.upper() or "TOTAL" in carton_qty_str.upper():
            continue

        carton_qty_str = carton_qty_str.replace(",", "").strip()
        qty_per_str = qty_per_str.replace(",", "").strip()

        if not carton_qty_str.isdigit() or not description:
            continue

        carton_qty = int(carton_qty_str)
        try:
            qty_per_carton = int(float(qty_per_str)) if qty_per_str else 0
        except ValueError:
            qty_per_carton = 0

        if carton_qty > 0:
            items.append({
                "description": description,
                "carton_qty": carton_qty,
                "qty_per_carton": qty_per_carton,
            })

    return items


# ---------------------------------------------------------------------------
# Label grouping
# ---------------------------------------------------------------------------

def group_line_items(line_items):
    """
    Group line items by description (preserving order of first appearance).

    Returns list of:
        {
            "description": str,
            "total_cartons": int,
            "carton_groups": [{"qty": int, "count": int}, ...]
        }

    Example:
        Input:  [{desc:"Grape Pop", carton_qty:11, qty_per_carton:400},
                 {desc:"Grape Pop", carton_qty: 1, qty_per_carton:260}]
        Output: [{description:"Grape Pop", total_cartons:12,
                  carton_groups:[{qty:400, count:11}, {qty:260, count:1}]}]
    """
    seen = {}
    order = []

    for item in line_items:
        desc = item["description"]
        if desc not in seen:
            seen[desc] = {"description": desc, "total_cartons": 0, "carton_groups": []}
            order.append(desc)
        seen[desc]["total_cartons"] += item["carton_qty"]
        seen[desc]["carton_groups"].append({
            "qty": item["qty_per_carton"],
            "count": item["carton_qty"],
        })

    return [seen[d] for d in order]


# ---------------------------------------------------------------------------
# Label PDF generation — 3 columns x 7 rows = 21 labels per page
# ---------------------------------------------------------------------------
# Label size:  2.83" wide  x  1.5" tall
# Page:        8.5"  x  11"  (US Letter)
# Derived margins:
#   Left/right: (8.5 - 3 × 2.83) / 2 ≈ 0.005"  (essentially flush)
#   Top/bottom: (11  - 7 × 1.5)  / 2  = 0.25"
# No borders — labels are printed on pre-cut adhesive sheets.
# ---------------------------------------------------------------------------

from reportlab.lib.utils import simpleSplit

PAGE_WIDTH, PAGE_HEIGHT = letter          # 612 x 792 pt
LABELS_PER_ROW  = 3
ROWS_PER_PAGE   = 7
LABELS_PER_PAGE = LABELS_PER_ROW * ROWS_PER_PAGE   # 21

H_LEFT_MARGIN = 0.125 * inch             # 9 pt  — left & right page margin
COL_GAP       = 0.0625 * inch           # 4.5 pt — gap between columns
V_TOP_MARGIN  = (PAGE_HEIGHT - ROWS_PER_PAGE * 1.5 * inch) / 2  # = 18 pt

LABEL_W = (PAGE_WIDTH - 2 * H_LEFT_MARGIN - (LABELS_PER_ROW - 1) * COL_GAP) / LABELS_PER_ROW
LABEL_H = 1.5 * inch                    # 108 pt

LABEL_PAD = 5   # pt — internal padding on all sides


def _label_origin(idx_on_page):
    """Return (x, y) bottom-left corner for label at position idx_on_page."""
    col = idx_on_page % LABELS_PER_ROW
    row = idx_on_page // LABELS_PER_ROW
    x = H_LEFT_MARGIN + col * (LABEL_W + COL_GAP)
    y = PAGE_HEIGHT - V_TOP_MARGIN - (row + 1) * LABEL_H
    return x, y


def build_labels_pdf(customer_name, po_number, grouped_items, out_path):
    """
    Generate a PDF with one label per carton, 3 labels per row (OL5350).
    Returns the total number of labels generated.
    """
    c = canvas.Canvas(str(out_path), pagesize=letter)
    label_index = 0

    for group in grouped_items:
        desc = group["description"]
        total = group["total_cartons"]
        box_num = 0

        for cg in group["carton_groups"]:
            for _ in range(cg["count"]):
                box_num += 1

                if label_index > 0 and label_index % LABELS_PER_PAGE == 0:
                    c.showPage()

                lx, ly = _label_origin(label_index % LABELS_PER_PAGE)
                _draw_label(c, lx, ly, customer_name, po_number, desc, box_num, total, cg["qty"])
                label_index += 1

    c.save()
    log.info(f"Generated labels PDF: {label_index} labels → {out_path}")
    return label_index


def _draw_label(c, x, y, customer_name, po_number, description, box_num, total_boxes, qty):
    """Draw a single shipping label clipped to its bounding box. No border."""
    pad = LABEL_PAD
    text_w = LABEL_W - 2 * pad      # max width available for text

    # --- Clip content to label area so nothing bleeds into adjacent labels ---
    c.saveState()
    clip = c.beginPath()
    clip.rect(x, y, LABEL_W, LABEL_H)
    c.clipPath(clip, stroke=0, fill=0)
    c.setFillColorRGB(0, 0, 0)

    # --- Layout top-down ---
    NAME_SIZE = 10
    DESC_SIZE = 8
    PO_SIZE   = 8
    BOX_SIZE  = 9

    cursor = y + LABEL_H - pad  # start just below top edge

    # Customer name
    c.setFont("Helvetica-Bold", NAME_SIZE)
    c.drawString(x + pad, cursor - NAME_SIZE, customer_name)
    cursor -= NAME_SIZE + 3

    # Description — wrap to 2 lines max if too long
    c.setFont("Helvetica", DESC_SIZE)
    desc_lines = simpleSplit(description, "Helvetica", DESC_SIZE, text_w)[:2]
    for line in desc_lines:
        c.drawString(x + pad, cursor - DESC_SIZE, line)
        cursor -= DESC_SIZE + 2

    # PO#
    c.setFont("Helvetica", PO_SIZE)
    c.drawString(x + pad, cursor - PO_SIZE, f"PO# {po_number}")

    # --- BOX / QTY — fixed position from bottom, raised to avoid cut-off ---
    bottom_y = y + pad + 4 + 0.25 * inch
    c.setFont("Helvetica-Bold", BOX_SIZE)
    c.drawString(x + pad, bottom_y, f"BOX: {box_num}/{total_boxes}")
    c.drawRightString(x + LABEL_W - pad, bottom_y, f"QTY: {qty:,}")

    c.restoreState()


# ---------------------------------------------------------------------------
# End-to-end processing
# ---------------------------------------------------------------------------

def _process_packing_slip(item_id):
    """Download packing slip, parse it, generate labels, upload to Monday."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf_in = Path(tmp) / "packing_slip.pdf"
        pdf_out = Path(tmp) / f"shipping_labels_{item_id}.pdf"

        log.info(f"Fetching packing slip file URL for item {item_id}")
        url, filename = get_packing_slip_url(item_id)
        log.info(f"Downloading: {filename}")
        download_file(url, pdf_in)

        log.info("Parsing packing slip")
        parsed = parse_packing_slip(pdf_in)

        if not parsed["line_items"]:
            raise RuntimeError("No line items found in packing slip — check PDF format")

        grouped = group_line_items(parsed["line_items"])
        total_labels = sum(g["total_cartons"] for g in grouped)
        log.info(
            f"Generating {total_labels} labels for "
            f"'{parsed['customer_name']}' PO# {parsed['po_number']}"
        )
        build_labels_pdf(parsed["customer_name"], parsed["po_number"], grouped, pdf_out)

        log.info("Uploading labels to Monday.com")
        upload_labels_to_monday(item_id, pdf_out)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook/monday", methods=["GET"])
def webhook_verify():
    """Monday.com webhook URL verification."""
    challenge = request.args.get("challenge")
    if challenge:
        return jsonify({"challenge": challenge})
    return "OK", 200


@app.route("/webhook/monday", methods=["POST"])
def webhook_handler():
    try:
        body = request.get_json(force=True, silent=True) or {}

        # Monday.com sends a JSON challenge on first registration
        if "challenge" in body:
            return jsonify({"challenge": body["challenge"]})

        event = body.get("event", body)
        item_id = event.get("pulseId") or event.get("itemId") or event.get("item_id")
        column_id = event.get("columnId") or event.get("column_id")

        log.info(f"Webhook — item={item_id}, column={column_id}")

        if not item_id:
            log.warning("No item_id in webhook payload, ignoring")
            return jsonify({"status": "ignored", "reason": "no item_id"}), 200

        allowed = {PACKING_SLIP_COLUMN_ID, JOB_STATUS_COLUMN_ID}
        if column_id and column_id not in allowed:
            log.info(f"Column {column_id} not a shipping-label trigger, ignoring")
            return jsonify({"status": "ignored", "reason": "wrong column"}), 200

        try:
            _process_packing_slip(item_id)
        except Exception as exc:
            log.exception(f"Processing error for item {item_id}: {exc}")
            return jsonify({"status": "error", "message": str(exc)}), 200

        return jsonify({"status": "ok"}), 200

    except Exception as exc:
        log.exception(f"Unexpected webhook error: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Local test endpoints (do not use in production)
# ---------------------------------------------------------------------------

@app.route("/test-parse", methods=["POST"])
def test_parse():
    """
    Test PDF parsing without Monday.com.
    Usage: curl -X POST -F "file=@packing_slip.pdf" http://localhost:5000/test-parse
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided — use form field 'file'"}), 400
    f = request.files["file"]
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        f.save(tmp.name)
        parsed = parse_packing_slip(tmp.name)
    grouped = group_line_items(parsed["line_items"])
    return jsonify({
        "customer_name": parsed["customer_name"],
        "po_number": parsed["po_number"],
        "grouped_items": grouped,
        "total_labels": sum(g["total_cartons"] for g in grouped),
    })


@app.route("/test-labels", methods=["POST"])
def test_labels():
    """
    Generate and return the labels PDF for a packing slip.
    Usage: curl -X POST -F "file=@packing_slip.pdf" http://localhost:5000/test-labels -o labels.pdf
    """
    from flask import send_file
    if "file" not in request.files:
        return jsonify({"error": "No file provided — use form field 'file'"}), 400
    f = request.files["file"]
    with tempfile.TemporaryDirectory() as tmp:
        pdf_in = Path(tmp) / "input.pdf"
        pdf_out = Path(tmp) / "labels.pdf"
        f.save(pdf_in)
        parsed = parse_packing_slip(pdf_in)
        grouped = group_line_items(parsed["line_items"])
        build_labels_pdf(parsed["customer_name"], parsed["po_number"], grouped, pdf_out)
        return send_file(
            pdf_out,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="shipping_labels.pdf",
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    log.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)


# ---------------------------------------------------------------------------
# Prelim Label Feature — Job Ticket Webhook
# Triggered when a Job Ticket PDF is uploaded to the Job Ticket column.
# Reads the PDF, generates one prelim label per 400 units per SKU, and
# uploads a merged PDF (same 3×7 grid as shipping labels) to the Prelim
# Label column. The packing slip / shipping label code above is unchanged.
# ---------------------------------------------------------------------------

JOB_TICKET_COLUMN_ID = "file_mksn8rw8"
PRELIM_LABEL_COLUMN_ID = "file_mm2cy8fm"


def get_job_ticket_url(item_id):
    """Return the (url, name) of the most recent file in the Job Ticket column."""
    query = """
    query GetJobTicket($itemId: ID!) {
      items(ids: [$itemId]) {
        column_values(ids: ["file_mksn8rw8"]) {
          ... on FileValue {
            files {
              ... on FileAssetValue {
                asset {
                  public_url
                  name
                }
              }
            }
          }
        }
      }
    }
    """
    data = monday_request(query, {"itemId": str(item_id)})
    items = data.get("data", {}).get("items", [])
    if not items:
        raise RuntimeError(f"Item {item_id} not found")
    col_values = items[0].get("column_values", [])
    if not col_values:
        raise RuntimeError("Job Ticket column not found on item")
    files = col_values[0].get("files", [])
    if not files:
        raise RuntimeError("No files in Job Ticket column")
    latest = files[-1].get("asset", {})
    url = latest.get("public_url")
    name = latest.get("name", "job_ticket.pdf")
    if not url:
        raise RuntimeError("Could not retrieve file URL from Job Ticket column")
    return url, name


def parse_job_ticket(pdf_path):
    """
    Parse a Full Scale job ticket PDF (fillable AcroForm).

    Handles two known field naming conventions:
      - Smokiez format: CUSTOMER, CUSTOMER PO, QTY TO PRINTRow1..15, NOTESRow1..15
      - DANK format:    7 - CUSTOMER, 18 - INVOICE#, QTY TO PRINTRow1..20 (+ Row1_2..Row10_2),
                        ITEM Row1..20 (+ Row1_2..Row10_2)

    Returns:
        {
            "client_name": str,
            "po_number": str,
            "skus": [{"description": str, "num_labels": int}, ...]
        }

    One label is generated per 400 units (ceiling division).
    """
    import math
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    fields = reader.get_fields() or {}

    def field_val(name):
        f = fields.get(name)
        if f is None:
            return ""
        v = f.get("/V", "")
        return str(v).strip() if v and v != "/Off" else ""

    def first_nonempty(*names):
        for name in names:
            v = field_val(name)
            if v:
                return v
        return ""

    # --- Client name: try both conventions ---
    client_name = first_nonempty("CUSTOMER", "7 - CUSTOMER") or "(No Client Name)"

    # --- PO number: try named field, then scan for any PO/INVOICE field ---
    po_number = first_nonempty("CUSTOMER PO", "CUSTOMER PO#")
    if not po_number:
        for k in sorted(fields.keys()):
            ku = k.upper()
            if ("PO" in ku or "INVOICE" in ku) and "PRINT" not in ku:
                v = field_val(k)
                if v:
                    po_number = v
                    break
    po_number = po_number or "(No PO#)"

    # --- SKU rows: scan both naming conventions and extended row sets ---
    # Row IDs: Row1–Row20 (primary) + Row1_2–Row10_2 (secondary, DANK format)
    row_ids = [str(n) for n in range(1, 21)] + [f"{n}_2" for n in range(1, 11)]

    skus = []
    for row_id in row_ids:
        qty_str = re.sub(r'[,\s]', '', field_val(f"QTY TO PRINTRow{row_id}"))
        # DANK uses "ITEM Row*", Smokiez uses "NOTESRow*"
        description = first_nonempty(f"ITEM Row{row_id}", f"NOTESRow{row_id}")
        if qty_str.isdigit() and int(qty_str) > 0 and description:
            num_labels = math.ceil(int(qty_str) / 400)
            skus.append({"description": description, "num_labels": num_labels})

    log.info(
        f"Parsed job ticket: client='{client_name}', PO='{po_number}', "
        f"{len(skus)} SKUs"
    )
    return {
        "client_name": client_name,
        "po_number": po_number,
        "skus": skus,
    }


def _draw_prelim_label(c, x, y, client_name, po_display, description):
    """Draw a single prelim label. Same dimensions as shipping labels; no BOX/QTY."""
    pad = LABEL_PAD
    text_w = LABEL_W - 2 * pad

    c.saveState()
    clip = c.beginPath()
    clip.rect(x, y, LABEL_W, LABEL_H)
    c.clipPath(clip, stroke=0, fill=0)
    c.setFillColorRGB(0, 0, 0)

    NAME_SIZE = 10
    DESC_SIZE = 8
    PO_SIZE = 8

    cursor = y + LABEL_H - pad

    # Client name (bold)
    c.setFont("Helvetica-Bold", NAME_SIZE)
    c.drawString(x + pad, cursor - NAME_SIZE, client_name)
    cursor -= NAME_SIZE + 3

    # SKU description — wrap up to 3 lines
    c.setFont("Helvetica", DESC_SIZE)
    desc_lines = simpleSplit(description, "Helvetica", DESC_SIZE, text_w)[:3]
    for line in desc_lines:
        c.drawString(x + pad, cursor - DESC_SIZE, line)
        cursor -= DESC_SIZE + 2

    # PO#
    c.setFont("Helvetica", PO_SIZE)
    c.drawString(x + pad, cursor - PO_SIZE, po_display)

    c.restoreState()


def build_prelim_labels_pdf(client_name, po_number, skus, out_path):
    """
    Generate a merged prelim labels PDF (3×7 grid, 21 per page).
    Labels from all SKUs fill the grid continuously — no wasted space between SKUs.
    Returns total label count.
    """
    po_display = f"PO# {po_number}" if not po_number.upper().startswith("PO#") else po_number

    all_descriptions = []
    for sku in skus:
        for _ in range(sku["num_labels"]):
            all_descriptions.append(sku["description"])

    c = canvas.Canvas(str(out_path), pagesize=letter)

    if not all_descriptions:
        c.setFont("Helvetica", 10)
        c.drawString(H_LEFT_MARGIN, PAGE_HEIGHT / 2, "No SKUs found in Job Ticket")
        c.save()
        return 0

    for i, description in enumerate(all_descriptions):
        if i > 0 and i % LABELS_PER_PAGE == 0:
            c.showPage()
        lx, ly = _label_origin(i % LABELS_PER_PAGE)
        _draw_prelim_label(c, lx, ly, client_name, po_display, description)

    c.save()
    total = len(all_descriptions)
    log.info(f"Generated prelim labels PDF: {total} labels → {out_path}")
    return total


def upload_prelim_labels_to_monday(item_id, pdf_path):
    """Upload the prelim labels PDF to the Prelim Label column on Monday.com."""
    token = get_token()
    mutation = """
    mutation AddFile($itemId: ID!, $columnId: String!, $file: File!) {
      add_file_to_column(item_id: $itemId, column_id: $columnId, file: $file) {
        id
      }
    }
    """
    variables = {
        "itemId": str(item_id),
        "columnId": PRELIM_LABEL_COLUMN_ID,
    }
    with open(pdf_path, "rb") as f:
        file_bytes = f.read()

    filename = Path(pdf_path).name
    resp = requests.post(
        MONDAY_FILE_API_URL,
        headers={"Authorization": token, "API-Version": "2024-01"},
        files={
            "query": (None, mutation),
            "variables": (None, json.dumps(variables)),
            "map": (None, json.dumps({"file": ["variables.file"]})),
            "file": (filename, file_bytes, "application/pdf"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise RuntimeError(f"Upload error: {result['errors']}")
    log.info(f"Uploaded {filename} to Prelim Label column for item {item_id}")
    return result


def _process_job_ticket(item_id):
    """Download job ticket, parse it, generate prelim labels, upload to Monday."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf_in = Path(tmp) / "job_ticket.pdf"
        pdf_out = Path(tmp) / f"prelim_labels_{item_id}.pdf"

        log.info(f"Fetching job ticket file URL for item {item_id}")
        url, filename = get_job_ticket_url(item_id)
        log.info(f"Downloading: {filename}")
        download_file(url, pdf_in)

        log.info("Parsing job ticket")
        parsed = parse_job_ticket(pdf_in)

        if not parsed["skus"]:
            raise RuntimeError("No SKUs found in job ticket — check PDF format")

        total_labels = sum(sku["num_labels"] for sku in parsed["skus"])
        log.info(
            f"Generating {total_labels} prelim labels for "
            f"'{parsed['client_name']}' PO# {parsed['po_number']}"
        )
        build_prelim_labels_pdf(
            parsed["client_name"], parsed["po_number"], parsed["skus"], pdf_out
        )

        log.info("Uploading prelim labels to Monday.com")
        upload_prelim_labels_to_monday(item_id, pdf_out)


@app.route("/webhook/job-ticket", methods=["GET"])
def job_ticket_webhook_verify():
    """Monday.com webhook URL verification for job ticket endpoint."""
    challenge = request.args.get("challenge")
    if challenge:
        return jsonify({"challenge": challenge})
    return "OK", 200


@app.route("/webhook/job-ticket", methods=["POST"])
def job_ticket_webhook_handler():
    try:
        body = request.get_json(force=True, silent=True) or {}

        if "challenge" in body:
            return jsonify({"challenge": body["challenge"]})

        event = body.get("event", body)
        item_id = event.get("pulseId") or event.get("itemId") or event.get("item_id")
        column_id = event.get("columnId") or event.get("column_id")

        log.info(f"Job ticket webhook — item={item_id}, column={column_id}")

        if not item_id:
            log.warning("No item_id in webhook payload, ignoring")
            return jsonify({"status": "ignored", "reason": "no item_id"}), 200

        if column_id and column_id != JOB_TICKET_COLUMN_ID:
            log.info(f"Column {column_id} is not Job Ticket, ignoring")
            return jsonify({"status": "ignored", "reason": "wrong column"}), 200

        try:
            _process_job_ticket(item_id)
        except Exception as exc:
            log.exception(f"Processing error for item {item_id}: {exc}")
            return jsonify({"status": "error", "message": str(exc)}), 200

        return jsonify({"status": "ok"}), 200

    except Exception as exc:
        log.exception(f"Unexpected webhook error: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 200
