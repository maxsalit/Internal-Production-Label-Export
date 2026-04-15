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


def download_file(url, dest_path, auth_token: str | None = None):
    """Download a file, optionally with a Monday.com API token for protected_static URLs.

    Monday's CDN (protected_static) requires 'Authorization: Bearer {token}'.
    The GraphQL endpoint uses the raw token without Bearer — these are different.
    """
    headers = {}
    if auth_token:
        headers["Authorization"] = auth_token
    elif "protected_static" in url or "monday.com" in url:
        # Monday protected_static URLs require the API token
        try:
            headers["Authorization"] = get_token()
        except Exception:
            pass
    resp = requests.get(url, headers=headers, timeout=60)
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

        if column_id and column_id != PACKING_SLIP_COLUMN_ID:
            log.info(f"Column {column_id} is not Packing Slip, ignoring")
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

    Handles four known field naming conventions:
      - Pouch JT:          has "POUCH TYPE" field; QTY TO PRINT{A-RR}, DETAIL  SKU{A-O} / DETAIL SKU{P+}
      - Non-Pouch JT:      has "QTY TO PRINTA" but no "POUCH TYPE"; Item # in single-letter field (A-N)
      - Old Smokiez/DANK:  has "QTY TO PRINTRow1"; description in "NOTESRow1" or "ITEM Row1"
      - WCC-style numbered: has "07 Text Field 4"; rows at Text Field 14/15, 19/20, … (+5 per row)

    Format is auto-detected from which fields are present.
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

    def parse_qty(raw):
        """Parse qty strings like '6000', '6,000', '10K', '10.5K' → int."""
        s = raw.strip().upper().replace(',', '').replace(' ', '')
        if not s:
            return 0
        if s.endswith('K'):
            try:
                return int(float(s[:-1]) * 1000)
            except ValueError:
                return 0
        try:
            return int(float(s))
        except ValueError:
            return 0

    # --- Detect format by template-unique fields ---
    is_pouch_format    = "POUCH TYPE" in fields          # Pouch JT template
    has_numbered_fields = "07 Text Field 4" in fields    # WCC-style generic-numbered template
    has_row_numbers    = "QTY TO PRINTRow1" in fields    # Old Smokiez / DANK
    has_letter_rows    = "QTY TO PRINTA" in fields       # New Non-Pouch JT (letter rows, no POUCH TYPE)

    # --- Client name ---
    if has_numbered_fields:
        client_name = field_val("07 Text Field 4") or "(No Client Name)"
    else:
        client_name = first_nonempty("CUSTOMER", "7 - CUSTOMER") or "(No Client Name)"

    # --- PO / Invoice number ---
    if has_numbered_fields:
        po_number = field_val("20 Text Field 10") or "(No PO#)"
    else:
        po_number = first_nonempty("CUSTOMER PO", "CUSTOMER PO#")
        if not po_number:
            for k in sorted(fields.keys()):
                ku = k.upper()
                if ("PO" in ku or "INVOICE" in ku) and "PRINT" not in ku and "DETAIL" not in ku:
                    v = field_val(k)
                    if v:
                        po_number = v
                        break
        po_number = po_number or "(No PO#)"

    skus = []

    if is_pouch_format:
        # Pouch JT: rows A–O use "DETAIL  SKU{row}" (double space);
        #           rows P–RR use "DETAIL SKU{row}" (single space)
        rows_ao = list("ABCDEFGHIJKLMNO")
        rows_p_plus = (
            list("PQRSTUVWXYZ")
            + ["AA", "BB", "CC", "DD", "EE", "FF", "GG", "HH", "II", "JJ",
               "KK", "LL", "MM", "NN", "OO", "PP", "QQ", "RR"]
        )
        for row in rows_ao + rows_p_plus:
            qty = parse_qty(field_val(f"QTY TO PRINT{row}"))
            detail_key = f"DETAIL  SKU{row}" if row in rows_ao else f"DETAIL SKU{row}"
            description = field_val(detail_key)
            if qty > 0 and description:
                skus.append({"description": description, "num_labels": math.ceil(qty / 400)})
    elif has_numbered_fields:
        # WCC-style template: rows at Text Field 14, 15 / 19, 20 / 24, 25 … (+5 per row, 10 rows)
        # [ITEM #, QTY TO PRINT, SIZE, M&C, NAME] per row
        for row in range(10):
            base = 14 + row * 5
            description = field_val(f"Text Field {base}")
            qty = parse_qty(field_val(f"Text Field {base + 1}"))
            if qty > 0 and description:
                skus.append({"description": description, "num_labels": math.ceil(qty / 400)})
    elif has_letter_rows:
        # New Non-Pouch JT: rows A–N; item name in single-letter field; row N has a space prefix
        for L in list("ABCDEFGHIJKLM") + ["N"]:
            sp = " " if L == "N" else ""
            qty = parse_qty(field_val(f"QTY TO PRINT{sp}{L}"))
            description = field_val(L)  # "Item #" column
            if qty > 0 and description:
                skus.append({"description": description, "num_labels": math.ceil(qty / 400)})
    elif has_row_numbers:
        # Old Smokiez / DANK format: numbered rows Row1–Row20 + Row1_2–Row10_2
        row_ids = [str(n) for n in range(1, 21)] + [f"{n}_2" for n in range(1, 11)]
        for row_id in row_ids:
            qty = parse_qty(field_val(f"QTY TO PRINTRow{row_id}"))
            description = first_nonempty(f"ITEM Row{row_id}", f"NOTESRow{row_id}")
            if qty > 0 and description:
                skus.append({"description": description, "num_labels": math.ceil(qty / 400)})

    if is_pouch_format:
        fmt = "pouch"
    elif has_numbered_fields:
        fmt = "non-pouch (numbered fields)"
    elif has_letter_rows:
        fmt = "non-pouch (letter rows)"
    elif has_row_numbers:
        fmt = "non-pouch (numbered rows)"
    else:
        fmt = "unknown"
    if not skus:
        all_field_names = sorted(fields.keys())
        log.warning(
            f"Parsed job ticket ({fmt} format): 0 SKUs found. "
            f"All PDF fields ({len(all_field_names)}): {all_field_names}"
        )
    else:
        log.info(
            f"Parsed job ticket ({fmt} format): client='{client_name}', "
            f"PO='{po_number}', {len(skus)} SKUs"
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


# ---------------------------------------------------------------------------
# Pouch Job Ticket Generation — Proof Approved Webhook
# Triggered when Proof Status changes to "Proof Approved" (status3__1).
# Finds the ProForma invoice on the Pricing board via PI#, extracts pouch
# specs using Claude, fills the Pouch JT PDF template, and uploads it to
# the Job Ticket column. Non-pouch invoices are silently skipped.
# ---------------------------------------------------------------------------

PRICING_BOARD_ID = "7035178904"
PRICING_PI_COLUMN_ID = "text_mksn7xdc"
PRICING_INVOICE_COLUMN_ID = "file_mknhcwtm"
PROOF_STATUS_COLUMN_ID = "status3__1"

POUCH_JT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "Job Ticket Templates"
    / "Pouches Job Ticket_Form_MULTI LOT V12.pdf"
)
NONPOUCH_JT_NOAPP_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "Job Ticket Templates"
    / "Non-Pouch JT_NoAPP_Final_April2026.pdf"
)
NONPOUCH_JT_WITHAPP_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "Job Ticket Templates"
    / "Non-Pouch_JT_WITH_Application April2026.pdf"
)

# Row letters used in the Pouch JT form (order = fill order)
# Rows A–O: DETAIL field has DOUBLE SPACE ("DETAIL  SKU{row}")
# Rows P+:  DETAIL field has SINGLE SPACE ("DETAIL SKU{row}")
_JT_ROWS_AO = list("ABCDEFGHIJKLMNO")
_JT_ROWS_P_PLUS = (
    list("PQRSTUVWXYZ")
    + ["AA", "BB", "CC", "DD", "EE", "FF", "GG", "HH", "II", "JJ",
       "KK", "LL", "MM", "NN", "OO", "PP", "QQ", "RR"]
)
_JT_ALL_ROWS = _JT_ROWS_AO + _JT_ROWS_P_PLUS


def _format_initials_from_text(people_text: str) -> str:
    """
    Convert a people column's text value (e.g. 'John Doe, Jane Smith') to
    slash-separated initials ('JD/JS').
    """
    if not people_text:
        return ""
    parts = []
    for name in re.split(r"[,;&]", people_text):
        name = name.strip()
        if name:
            parts.append("".join(w[0].upper() for w in name.split() if w))
    return "/".join(parts)


def _get_item_data_for_jt(item_id: int) -> dict:
    """
    Fetch header data and subitems from the Monday item for Job Ticket filling.

    Returns:
        {
            "customer": str,
            "sram_initials": str,
            "order_date": str,
            "pi_number": str,
            "customer_po": str,
            "subitems": [{"name": str, "qty": str}, ...]
        }
    """
    # Fetch board columns (for title lookup) alongside item column_values.
    # Monday API v2024-01 does not expose `title` on ColumnValue — we join via board.columns.
    # People column `text` = comma-separated names (e.g. "John Doe, Jane Smith").
    query = """
    query GetItemForJT($itemId: ID!) {
      items(ids: [$itemId]) {
        id
        name
        board {
          columns {
            id
            title
            type
          }
        }
        column_values {
          id
          type
          text
          value
        }
        subitems {
          id
          name
          board {
            columns {
              id
              title
            }
          }
          column_values {
            id
            type
            text
            value
          }
        }
      }
    }
    """
    data = monday_request(query, {"itemId": str(item_id)})
    items = data.get("data", {}).get("items", [])
    if not items:
        raise RuntimeError(f"Item {item_id} not found")

    item = items[0]

    # title map: column_id → title (from parent board)
    board_col_titles = {
        col["id"]: col["title"].strip()
        for col in item.get("board", {}).get("columns", [])
    }

    col_by_title: dict = {}  # title.lower() → column_value
    col_by_id: dict = {}

    for cv in item.get("column_values", []):
        cid = cv.get("id", "")
        col_by_id[cid] = cv
        title = board_col_titles.get(cid, "").lower()
        if title:
            col_by_title[title] = cv

    def get_text(*titles):
        for t in titles:
            cv = col_by_title.get(t.lower(), {})
            v = (cv.get("text") or "").strip()
            if v:
                return v
        return ""

    # "Client" is a mirror column on this board; text = client name
    customer = get_text("client", "customer", "customer name", "company")

    # SR and AM are separate people columns; combine initials (e.g. "JD/JS")
    sr_text = get_text("sr")
    am_text = get_text("am")
    sram_initials = "/".join(
        filter(None, [_format_initials_from_text(sr_text), _format_initials_from_text(am_text)])
    )

    order_date = get_text("order date", "date")

    # PI# — prefer known column ID, fall back to title match
    pi_cv = col_by_id.get("text_mksn14en", {})
    pi_number = (pi_cv.get("text") or "").strip() or get_text("pi #", "pi#", "pi", "pi number")

    # Column title is "PO#" on this board
    customer_po = get_text("po#", "customer po", "customer po#", "po number", "purchase order")

    # Subitems → name + order quantity
    subitems = []
    for si in item.get("subitems", []):
        si_name = (si.get("name") or "").strip()

        # Build subitem column title map from its own board
        si_col_titles = {
            col["id"]: col["title"].strip().lower()
            for col in si.get("board", {}).get("columns", [])
        }

        qty = ""
        for cv in si.get("column_values", []):
            t = si_col_titles.get(cv.get("id", ""), "")
            text_val = (cv.get("text") or "").strip()
            if any(kw in t for kw in ("qty", "quantity", "order", "units")) and text_val:
                qty = text_val
                break

        # Fallback: first column with a pure numeric value
        if not qty:
            for cv in si.get("column_values", []):
                text_val = (cv.get("text") or "").strip()
                if text_val and text_val.replace(",", "").isdigit():
                    qty = text_val
                    break

        subitems.append({"name": si_name, "qty": qty})

    log.info(
        f"[jt-data] item={item_id} customer='{customer}' PI#='{pi_number}' "
        f"subitems={len(subitems)}"
    )
    return {
        "customer": customer,
        "sram_initials": sram_initials,
        "order_date": order_date,
        "pi_number": pi_number,
        "customer_po": customer_po,
        "subitems": subitems,
    }


def _post_monday_error_update(item_id: int, error_msg: str) -> None:
    """Post an error message as a Monday.com update on the item so it's visible without Vercel logs."""
    try:
        mutation = """
        mutation PostUpdate($itemId: ID!, $body: String!) {
          create_update(item_id: $itemId, body: $body) { id }
        }
        """
        monday_request(mutation, {
            "itemId": str(item_id),
            "body": f"⚠️ Proof Approved automation error:\n\n{error_msg}",
        })
    except Exception as e:
        log.warning(f"[proof-approved] could not post error update to Monday: {e}")


def _find_invoice_on_pricing_board(pi_number: str) -> tuple:
    """
    Search the Pricing board for an item whose PI# column matches pi_number.
    Returns (url, filename, customer_name) of the most recent invoice file attached.
    customer_name comes from the "Client" dropdown column on the Pricing board.

    File download strategy: Monday's protected_static CDN URLs cannot be downloaded
    with an API token. Instead we get the assetId from the file column value JSON,
    then call assets(ids: [assetId]) { public_url } which returns a pre-signed S3 URL
    valid for 1 hour — no auth header needed to download.
    """
    query = """
    query FindInvoice($boardId: ID!, $columnId: String!, $value: String!) {
      items_page_by_column_values(
        limit: 5
        board_id: $boardId
        columns: [{ column_id: $columnId, column_values: [$value] }]
      ) {
        items {
          id
          name
          column_values(ids: ["file_mknhcwtm", "dropdown_mks82t5z"]) {
            id
            text
            value
          }
        }
      }
    }
    """
    data = monday_request(query, {
        "boardId": PRICING_BOARD_ID,
        "columnId": PRICING_PI_COLUMN_ID,
        "value": str(pi_number),
    })
    items = (
        data.get("data", {})
        .get("items_page_by_column_values", {})
        .get("items", [])
    )
    if not items:
        raise RuntimeError(f"No item found on Pricing board for PI# '{pi_number}'")

    item = items[0]
    col_values = item.get("column_values", [])

    invoice_cv = next((cv for cv in col_values if cv.get("id") == "file_mknhcwtm"), {})
    client_cv = next((cv for cv in col_values if cv.get("id") == "dropdown_mks82t5z"), {})

    customer_name = (client_cv.get("text") or "").strip()

    # Parse assetId from the file column value JSON
    raw_value = invoice_cv.get("value") or "{}"
    try:
        file_data = json.loads(raw_value)
        latest_file = (file_data.get("files") or [{}])[-1]
        asset_id = latest_file.get("assetId") or latest_file.get("id")
        name = latest_file.get("name", "invoice.pdf")
    except Exception:
        asset_id = None
        name = "invoice.pdf"

    if not asset_id:
        raise RuntimeError(f"No invoice file attached for PI# '{pi_number}'")

    # Query the assets API for a pre-signed public_url (downloadable without auth)
    assets_query = """
    query GetAsset($ids: [ID!]!) {
      assets(ids: $ids) { id name public_url url }
    }
    """
    assets_data = monday_request(assets_query, {"ids": [str(asset_id)]})
    assets = assets_data.get("data", {}).get("assets", [])
    if not assets:
        raise RuntimeError(f"Asset {asset_id} not found for PI# '{pi_number}'")

    asset = assets[0]
    name = asset.get("name") or name
    # Prefer public_url (pre-signed S3, no auth needed); fall back to url
    url = asset.get("public_url") or asset.get("url") or ""
    if not url:
        raise RuntimeError(
            f"No downloadable URL for invoice asset {asset_id} (PI# '{pi_number}'). "
            "public_url and url are both empty."
        )

    log.info(f"[find-invoice] PI#{pi_number} → asset {asset_id} '{name}' url_type={'public' if asset.get('public_url') else 'protected'}")
    return url, name, customer_name


def _extract_invoice_text(pdf_path) -> str:
    """Extract all text from an invoice PDF using pdfplumber."""
    parts = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _extract_pouch_specs(invoice_text: str) -> list:
    """
    Call Claude API to extract all pouch line items from the invoice.

    Returns a list of spec dicts — one per distinct "Pouches:" line item.
    Returns an empty list if the invoice contains no pouch products.
    Each dict has all spec fields as strings ("" if not found on the invoice).
    """
    import anthropic as _ant

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("Anthropic_API_Key")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = _ant.Anthropic(api_key=api_key)

    prompt = (
        "You are a packaging production assistant extracting job specifications "
        "from a ProForma invoice.\n\n"
        "TASK: Find every distinct pouch/bag product line item (lines starting with "
        "\"Pouches:\") and extract its specs. Each sizing variant is a SEPARATE item.\n\n"
        "Pouch products: stand-up pouches, flat pouches, mylar bags, resealable bags, etc.\n"
        "Skip non-pouch lines (fees, shipping, labels, boxes, services).\n\n"
        "For EACH pouch line item, extract:\n\n"
        "TEXT FIELDS (exact text from invoice, or \"\" if not found):\n"
        "- sku: Most descriptive product name for this size variant "
        "(e.g. 'Summit\\'s Peak Domestic Pouches - 1/4 OZ Sizing')\n"
        "- pouch_type: Type of pouch (e.g. \"Custom Pouch\", \"Stand-Up Pouch\")\n"
        "- width: Width in inches, number only (e.g. \"6\")\n"
        "- height: Height in inches, number only (e.g. \"4.5\")\n"
        "- gusset: Gusset depth in inches, number only, or \"\" if none\n"
        "- pms_swatch: Pantone/PMS color (e.g. \"PMS 123 C\"), or \"\" if none\n"
        "- details: Any spec notes not captured in the fields above, or \"\"\n\n"
        "DROPDOWN FIELDS — match EXACTLY to one of the options, or \"\" if unclear:\n"
        "- premium_white: NONE | 1 HIT | 2 HIT\n"
        "- substrate: MET PET | PCR MET PET | WHITE MET PET | CLEAR PET | OTHER *\n"
        "- color: CMYK | CMY | CMYK + WHITE | CMY + WHITE | K ONLY\n"
        "- lamination: GLOSS | MATTE | SOFT TOUCH | HOLOGRAPHIC | OTHER *\n"
        "- zipper: CR ZIPPER (24MM) | NON - CR ZIPPER (10MM) | NO ZIPPER\n"
        "- hang_hole: NONE | CIRCLE (8MM) | SOMBERO\n"
        "- tear_notches: YES | NO\n"
        "- seal_type: K WITH SKIRT | K WITHOUT SKIRT | 3SS\n"
        "- corner: SQUARE | 0.25\" ROUND CORNER\n\n"
        "SUBSTRATE MAPPING: METPET → MET PET\n"
        "ZIPPER MAPPING: Freshlock CR (24mm) → CR ZIPPER (24MM)\n"
        "CORNER MAPPING: Rounded → 0.25\" ROUND CORNER\n"
        "COLOR MAPPING: CMYK + White → CMYK + WHITE\n"
        "LAMINATION MAPPING: Matte Laminate → MATTE\n"
        "SEAL MAPPING: K-Seal With Skirt → K WITH SKIRT\n\n"
        f"INVOICE TEXT:\n{invoice_text[:8000]}\n\n"
        "Respond with ONLY a valid JSON array (no markdown, no extra text). "
        "One object per pouch line item. Return [] if no pouch products found.\n"
        '[{"sku": "", "pouch_type": "", "width": "", "height": "", "gusset": "", '
        '"pms_swatch": "", "details": "", "premium_white": "", "substrate": "", '
        '"color": "", "lamination": "", "zipper": "", "hang_hole": "", '
        '"tear_notches": "", "seal_type": "", "corner": ""}]'
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    # Extract the first complete JSON array by counting bracket depth
    json_str = None
    start = response_text.find("[")
    if start != -1:
        depth = 0
        for i, ch in enumerate(response_text[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    json_str = response_text[start : i + 1]
                    break
    if not json_str:
        raise RuntimeError(f"Claude returned unexpected response: {response_text[:300]}")

    specs_list = json.loads(json_str)
    if not isinstance(specs_list, list):
        raise RuntimeError(f"Claude returned non-list JSON: {response_text[:300]}")

    log.info(f"[claude] extracted {len(specs_list)} pouch line item(s)")
    for i, s in enumerate(specs_list, 1):
        log.info(
            f"  [{i}] sku='{s.get('sku')}' size={s.get('width')}x"
            f"{s.get('height')}x{s.get('gusset')} substrate='{s.get('substrate')}'"
        )
    return specs_list


def _extract_nonpouch_specs(invoice_text: str) -> dict | None:
    """
    Call Claude API to extract non-pouch label job specs from the invoice.

    Returns a dict with keys: product_name, size, material_coating, has_application, details
    Returns None if the invoice does not describe a label job (e.g. it is pouch-only or
    contains no label line items), so _process_proof_approved can skip silently.
    """
    import anthropic as _ant

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("Anthropic_API_Key")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = _ant.Anthropic(api_key=api_key)

    prompt = (
        "You are a packaging production assistant extracting job specifications "
        "from a ProForma invoice for NON-POUCH label products.\n\n"
        "TASK: Determine whether this invoice contains label products (pressure-sensitive "
        "labels, shrink sleeves, wrap-around labels, etc.). If it does, extract the specs "
        "below. If there are no label line items, return null.\n\n"
        "Extract the following as a JSON object:\n"
        "- product_name: The descriptive product/SKU name for the label (e.g. 'Custom Labels')\n"
        "- size: Label dimensions in W x H format with inch marks "
        "(e.g. '4.6\" x 2.15\"'). Use only the numeric dimensions — do NOT include "
        "labels like 'W' or 'H'. Always use the inch mark (\") not the word 'inches'.\n"
        "- material_coating: The material and coating specification found near the bottom "
        "of the invoice in the job spec / material section "
        "(e.g. 'BOPP w/ Matte Laminate', 'White BOPP, Gloss OV'). "
        "Capture the full value including material and any coating/laminate.\n"
        "- has_application: true if the invoice or job notes mention 'Application', "
        "'Application Service', or similar applied-label service; otherwise false.\n"
        "- details: Any additional relevant spec notes not captured above, or \"\"\n\n"
        "Return ONLY a valid JSON object (no markdown). "
        "Return the literal value null (not a JSON object) if no label products are present.\n\n"
        f"INVOICE TEXT:\n{invoice_text[:8000]}"
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    log.info(f"[claude-nonpouch] raw response: {response_text[:300]}")

    if response_text.lower() in ("null", "none", ""):
        log.info("[claude-nonpouch] invoice has no label products — skipping")
        return None

    # Extract the first complete JSON object by counting brace depth.
    # re.search with DOTALL is greedy and matches first-{ to last-}, which
    # breaks when Claude adds trailing text or multiple objects.
    json_str = None
    start = response_text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(response_text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    json_str = response_text[start : i + 1]
                    break

    if not json_str:
        log.warning(f"[claude-nonpouch] unexpected response (no JSON object): {response_text[:300]}")
        return None

    specs = json.loads(json_str)
    if not isinstance(specs, dict):
        log.warning("[claude-nonpouch] Claude returned non-dict JSON — skipping")
        return None

    log.info(
        f"[claude-nonpouch] specs: product='{specs.get('product_name')}' "
        f"size='{specs.get('size')}' mc='{specs.get('material_coating')}' "
        f"has_application={specs.get('has_application')}"
    )
    return specs


def _fill_nonpouch_jt(template_path, item_data: dict, specs: dict, subitems: list, out_path) -> None:
    """
    Fill a Non-Pouch Job Ticket PDF template and save to out_path.

    Page 0 has 14 AcroForm rows (letters A–N) filled via update_page_form_field_values:
      - DETAIL  SKU{L}  (double-space; row N has a space before N: 'DETAIL  SKU N')
      - QTY TO PRINT{L} (row N: 'QTY TO PRINT N')
      - M&C {L}         (always has a space)
      - FILE NAME{L}    (row N: 'FILE NAME N')
      - {L}             (just the letter — the item# column)

    Pages 1–3 have P2/P3/P4 overflow rows (72 rows) set via direct annotation scanning:
      P2 rows 01–14 have _P2 suffix; rows 15–24 don't. P3/P4 rows have no suffix.

    Total capacity: 86 rows.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, create_string_object

    reader = PdfReader(str(template_path))
    writer = PdfWriter()
    writer.append(reader)

    size = (specs.get("size") or "").strip()
    mc = (specs.get("material_coating") or "").strip()

    # -----------------------------------------------------------------------
    # Page 0: AcroForm rows A–N (14 rows)
    # -----------------------------------------------------------------------
    _PAGE0_LETTERS = list("ABCDEFGHIJKLMN")  # 14 letters

    acroform_fields: dict[str, str] = {}
    def _a(k, v):
        if v:
            acroform_fields[k] = v

    # Header fields
    _a("CUSTOMER", item_data.get("customer", ""))
    _a("SRAM", item_data.get("sram_initials", ""))
    _a("ORDER DATE", item_data.get("order_date", ""))
    _a("PI", item_data.get("pi_number", ""))
    _a("CUSTOMER PO", item_data.get("customer_po", ""))

    # Page 0 item rows — fill as many as we have subitems (up to 14)
    page0_subitems = subitems[: len(_PAGE0_LETTERS)]
    overflow_subitems = subitems[len(_PAGE0_LETTERS):]

    for i, subitem in enumerate(page0_subitems):
        L = _PAGE0_LETTERS[i]
        # Row N has a leading space before the letter in most field names
        sp = " " if L == "N" else ""
        item_name = (subitem.get("name") or "").strip()
        qty = (subitem.get("qty") or "").strip()
        # Item # column ({L}) = SKU/subitem name
        _a(L, item_name)
        # DETAIL  SKU{L} = SIZE column on the non-pouch template
        _a(f"DETAIL  SKU{sp}{L}", size)
        _a(f"QTY TO PRINT{sp}{L}", qty)
        _a(f"M&C {L}", mc)
        # FILE NAME left blank for team to fill

    for page in writer.pages:
        writer.update_page_form_field_values(page, acroform_fields)

    # -----------------------------------------------------------------------
    # Pages 1–3: overflow rows via direct annotation scanning (P2/P3/P4)
    # -----------------------------------------------------------------------
    if overflow_subitems:
        # Build ordered slot list for P2/P3/P4 rows
        slots: list[tuple[int, int, bool]] = []
        for r in range(1, 15):    # P2 rows 01–14: have _P2 suffix
            slots.append((2, r, True))
        for r in range(15, 25):   # P2 rows 15–24: no suffix
            slots.append((2, r, False))
        for r in range(1, 25):    # P3 rows 01–24
            slots.append((3, r, False))
        for r in range(1, 25):    # P4 rows 01–24
            slots.append((4, r, False))

        row_field_values: dict[str, str] = {}
        for slot_idx, subitem in enumerate(overflow_subitems[: len(slots)]):
            p, r, has_suffix = slots[slot_idx]
            sfx = f"_P{p}" if has_suffix else ""
            rr = f"{r:02d}"
            row_field_values[f"P{p}_R{rr}_ITEM{sfx}"] = (subitem.get("name") or "").strip()
            row_field_values[f"P{p}_R{rr}_QTY{sfx}"] = (subitem.get("qty") or "").strip()
            row_field_values[f"P{p}_R{rr}_SIZE{sfx}"] = size
            row_field_values[f"P{p}_R{rr}_MC{sfx}"] = mc

        for page in writer.pages:
            annots_ref = page.get("/Annots")
            if not annots_ref:
                continue
            annots = annots_ref.get_object() if hasattr(annots_ref, "get_object") else annots_ref
            for ref in annots:
                try:
                    annot = ref.get_object()
                except Exception:
                    continue
                field_name = str(annot.get("/T", ""))
                if field_name not in row_field_values:
                    continue
                val = row_field_values[field_name]
                annot[NameObject("/V")] = create_string_object(val)
                if NameObject("/AP") in annot:
                    del annot[NameObject("/AP")]

        log.info(f"[fill-nonpouch-jt] overflow: {len(row_field_values)} P2/P3/P4 fields set")

    with open(str(out_path), "wb") as f:
        writer.write(f)

    log.info(
        f"[fill-nonpouch-jt] {len(acroform_fields)} AcroForm fields "
        f"({len(page0_subitems)} page-0 rows, {len(overflow_subitems)} overflow) → {out_path}"
    )


def _fill_pouch_jt(template_path, item_data: dict, pouch_specs: dict, subitems: list, out_path) -> None:
    """Fill the Pouch Job Ticket PDF template and save to out_path."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(template_path))
    writer = PdfWriter()
    writer.append(reader)

    fields: dict = {}

    # Header
    _set = lambda k, v: fields.__setitem__(k, v) if v else None
    _set("CUSTOMER", item_data.get("customer", ""))
    _set("SRAM", item_data.get("sram_initials", ""))
    _set("ORDER DATE", item_data.get("order_date", ""))
    _set("PI", item_data.get("pi_number", ""))
    _set("CUSTOMER PO", item_data.get("customer_po", ""))

    # Job spec text fields (W/H/G get inch marks appended)
    for field_name, spec_key in [
        ("SKU", "sku"),
        ("POUCH TYPE", "pouch_type"),
        ("W", "width"),
        ("H", "height"),
        ("G", "gusset"),
        ("PMS SWATCH", "pms_swatch"),
    ]:
        val = (pouch_specs.get(spec_key) or "").strip()
        if field_name in ("W", "H", "G") and val and not val.endswith('"'):
            val = val + '"'
        _set(field_name, val)

    # Dropdowns — defaults apply when Claude leaves a field blank
    _DROPDOWN_DEFAULTS = {
        "Dropdown6": "NONE",  # hang_hole: if not specified, default to None
    }
    for field_name, spec_key in [
        ("Dropdown1", "premium_white"),
        ("Dropdown2", "substrate"),
        ("Dropdown3", "color"),
        ("Dropdown4", "lamination"),
        ("Dropdown5", "zipper"),
        ("Dropdown6", "hang_hole"),
        ("Dropdown8", "tear_notches"),
        ("Dropdown9", "seal_type"),
        ("Dropdown10", "corner"),
    ]:
        val = (pouch_specs.get(spec_key) or "").strip()
        if not val:
            val = _DROPDOWN_DEFAULTS.get(field_name, "")
        _set(field_name, val)

    # Details text area
    _set("DETAILSRow1", (pouch_specs.get("details") or "").strip())

    # Subitem rows — fill QTY and DETAIL SKU; leave ITEM# blank (team fills it)
    for i, subitem in enumerate(subitems[: len(_JT_ALL_ROWS)]):
        row = _JT_ALL_ROWS[i]
        qty = (subitem.get("qty") or "").strip()
        sku_name = (subitem.get("name") or "").strip()
        _set(f"QTY TO PRINT{row}", qty)
        # Rows A–O have DOUBLE SPACE before SKU; P+ have single space
        detail_key = f"DETAIL  SKU{row}" if row in _JT_ROWS_AO else f"DETAIL SKU{row}"
        _set(detail_key, sku_name)

    for page in writer.pages:
        writer.update_page_form_field_values(page, fields)

    # After filling, increase font size for dimension fields and force re-render.
    # update_page_form_field_values bakes a cached appearance (/AP); deleting it
    # makes PDF viewers fall back to /DA (Default Appearance) which we set to 12pt.
    from pypdf.generic import NameObject, create_string_object
    _DIM_FIELDS = {"W", "H", "G"}
    for page in writer.pages:
        annots = page.get("/Annots") or []
        for ref in annots:
            try:
                annot = ref.get_object()
            except Exception:
                continue
            t = str(annot.get("/T", ""))
            if t not in _DIM_FIELDS:
                continue
            da = str(annot.get("/DA", "/Helv 8 Tf 0 g"))
            new_da = re.sub(r"[\d.]+\s+Tf", "12 Tf", da)
            if "Tf" not in new_da:
                new_da = "/Helv 12 Tf 0 g"
            annot[NameObject("/DA")] = create_string_object(new_da)
            if NameObject("/AP") in annot:
                del annot[NameObject("/AP")]

    with open(str(out_path), "wb") as f:
        writer.write(f)

    log.info(f"[fill-jt] wrote {len(fields)} fields → {out_path}")


def _upload_file_to_monday_column(item_id: int, file_path, column_id: str) -> None:
    """Upload any file to a specified Monday.com file column."""
    token = get_token()
    mutation = """
    mutation AddFile($itemId: ID!, $columnId: String!, $file: File!) {
      add_file_to_column(item_id: $itemId, column_id: $columnId, file: $file) {
        id
      }
    }
    """
    variables = {"itemId": str(item_id), "columnId": column_id}
    with open(str(file_path), "rb") as f:
        file_bytes = f.read()

    filename = Path(file_path).name
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
    log.info(f"[upload] {filename} → column '{column_id}' on item {item_id}")


def _process_proof_approved(item_id: int) -> None:
    """
    End-to-end handler for Proof Approved trigger:
      1. Fetch item data (customer, PI#, subitems, …) from Monday
      2. Find and download ProForma invoice from Pricing board via PI#
      3. Extract invoice text
      4a. If pouch line items found → fill Pouch JT template (one per distinct size)
      4b. Else if label line items found → fill Non-Pouch JT template (NoAPP or WITH_Application)
      4c. Else → skip silently
      5. Upload filled PDF(s) to Job Ticket column
      6. Directly generate prelim labels (Monday may not fire the JT webhook for uploads)
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)

        try:
            log.info(f"[proof-approved] step 1/6 — fetching item data for {item_id}")
            item_data = _get_item_data_for_jt(item_id)
        except Exception as e:
            raise RuntimeError(f"[step 1 fetch-item] {e}") from e

        pi_number = item_data.get("pi_number", "").strip()
        if not pi_number:
            log.warning(f"[proof-approved] item {item_id} has no PI# — skipping")
            return

        try:
            log.info(f"[proof-approved] step 2/6 — looking up invoice for PI# {pi_number}")
            invoice_url, invoice_name, pricing_customer = _find_invoice_on_pricing_board(pi_number)
        except Exception as e:
            raise RuntimeError(f"[step 2 find-invoice PI#{pi_number}] {e}") from e

        # Mirror column may be null if board relation isn't connected; fall back to Pricing board
        if not item_data.get("customer") and pricing_customer:
            item_data["customer"] = pricing_customer
            log.info(f"[proof-approved] customer from Pricing board: '{pricing_customer}'")

        try:
            log.info(f"[proof-approved] step 3/6 — downloading invoice from {invoice_url[:80]}…")
            invoice_path = tmp / "invoice.pdf"
            download_file(invoice_url, invoice_path)
            log.info(f"[proof-approved] invoice downloaded ({invoice_path.stat().st_size} bytes)")
        except Exception as e:
            raise RuntimeError(f"[step 3 download-invoice] {e}") from e

        try:
            log.info("[proof-approved] step 4/6 — extracting invoice text")
            invoice_text = _extract_invoice_text(invoice_path)
            log.info(f"[proof-approved] extracted {len(invoice_text)} chars of invoice text")
        except Exception as e:
            raise RuntimeError(f"[step 4 extract-text] {e}") from e

        try:
            log.info("[proof-approved] step 5a/6 — calling Claude for pouch specs")
            specs_list = _extract_pouch_specs(invoice_text)
        except Exception as e:
            raise RuntimeError(f"[step 5a claude-pouch] {e}") from e

        # Build a filesystem-safe base name: "Client Name_PI#_JT"
        _safe = re.sub(r'[\\/:*?"<>|]', "", item_data.get("customer", "Unknown"))
        _pi = item_data.get("pi_number", "").strip() or "NoPI"
        _base = f"{_safe}_{_pi}_JT"

        uploaded_jt_paths = []

        if specs_list:
            # --- Pouch job: one JT per distinct sizing line item ---
            for i, pouch_specs in enumerate(specs_list, 1):
                suffix = f"_{i}" if len(specs_list) > 1 else ""
                jt_out = tmp / f"{_base}{suffix}.pdf"
                try:
                    log.info(f"[proof-approved] step 5b/6 — filling pouch JT {i}/{len(specs_list)}: {pouch_specs.get('sku', '')}")
                    _fill_pouch_jt(
                        POUCH_JT_TEMPLATE_PATH,
                        item_data,
                        pouch_specs,
                        item_data["subitems"],
                        jt_out,
                    )
                except Exception as e:
                    raise RuntimeError(f"[step 5b fill-pouch-jt {i}] {e}") from e
                try:
                    log.info(f"[proof-approved] step 6/6 — uploading pouch JT {i}/{len(specs_list)}")
                    _upload_file_to_monday_column(item_id, jt_out, JOB_TICKET_COLUMN_ID)
                    uploaded_jt_paths.append(jt_out)
                except Exception as e:
                    raise RuntimeError(f"[step 6 upload-pouch-jt {i}] {e}") from e
            log.info(f"[proof-approved] done — {len(specs_list)} pouch JT(s) uploaded for item {item_id}")
        else:
            # --- Not a pouch job — try non-pouch label template ---
            try:
                log.info("[proof-approved] step 5b/6 — calling Claude for non-pouch specs")
                nonpouch_specs = _extract_nonpouch_specs(invoice_text)
            except Exception as e:
                raise RuntimeError(f"[step 5b claude-nonpouch] {e}") from e

            if nonpouch_specs is None:
                log.info(f"[proof-approved] item {item_id} is not a pouch or label job — skipping")
                return

            has_app = nonpouch_specs.get("has_application", False)
            template = NONPOUCH_JT_WITHAPP_TEMPLATE_PATH if has_app else NONPOUCH_JT_NOAPP_TEMPLATE_PATH
            log.info(
                f"[proof-approved] non-pouch label job — "
                f"{'WITH' if has_app else 'No'} Application template"
            )

            jt_out = tmp / f"{_base}.pdf"
            try:
                _fill_nonpouch_jt(
                    template,
                    item_data,
                    nonpouch_specs,
                    item_data["subitems"],
                    jt_out,
                )
            except Exception as e:
                raise RuntimeError(f"[step 5b fill-nonpouch-jt] {e}") from e
            try:
                log.info("[proof-approved] step 6/6 — uploading non-pouch JT")
                _upload_file_to_monday_column(item_id, jt_out, JOB_TICKET_COLUMN_ID)
                uploaded_jt_paths.append(jt_out)
            except Exception as e:
                raise RuntimeError(f"[step 6 upload-nonpouch-jt] {e}") from e
            log.info(f"[proof-approved] done — non-pouch JT uploaded for item {item_id}")

        # Monday.com does not reliably fire column-change webhooks for programmatic
        # file uploads, so we generate prelim labels directly here rather than
        # relying on the JT webhook to pick them up.
        if uploaded_jt_paths:
            log.info(f"[proof-approved] generating prelim labels directly for item {item_id}")
            try:
                _process_job_ticket(item_id)
                log.info(f"[proof-approved] prelim labels generated for item {item_id}")
            except Exception as e:
                # Non-fatal: JT was already uploaded; log but don't fail the whole request
                log.error(f"[proof-approved] prelim label generation failed (non-fatal): {e}")


@app.route("/webhook/proof-approved", methods=["GET"])
def proof_approved_webhook_verify():
    challenge = request.args.get("challenge")
    if challenge:
        return jsonify({"challenge": challenge})
    return "OK", 200


@app.route("/webhook/proof-approved", methods=["POST"])
def proof_approved_webhook_handler():
    try:
        body = request.get_json(force=True, silent=True) or {}

        if "challenge" in body:
            return jsonify({"challenge": body["challenge"]})

        event = body.get("event", body)
        item_id = event.get("pulseId") or event.get("itemId") or event.get("item_id")
        column_id = event.get("columnId") or event.get("column_id")

        log.info(f"[proof-approved] webhook — item={item_id} column={column_id}")

        if not item_id:
            return jsonify({"status": "ignored", "reason": "no item_id"}), 200

        if column_id and column_id != PROOF_STATUS_COLUMN_ID:
            return jsonify({"status": "ignored", "reason": "wrong column"}), 200

        try:
            _process_proof_approved(int(item_id))
        except Exception as exc:
            log.exception(f"[proof-approved] error for item {item_id}: {exc}")
            return jsonify({"status": "error", "message": str(exc)}), 200

        return jsonify({"status": "ok"}), 200

    except Exception as exc:
        log.exception(f"[proof-approved] unexpected error: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 200
