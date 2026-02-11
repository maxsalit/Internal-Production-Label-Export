"""
Generate a single label from Monday.com (no webhook).
Use this to test your API token and see the PDF before setting up the automation.

Usage:
  python test_label.py
  python test_label.py --board 9347371455 --item 11244242150
"""

import argparse
import sys
from pathlib import Path

# Load .env before importing app (so MONDAY_API_TOKEN is set)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import (
    fetch_item,
    extract_label_data,
    build_label_pdf,
    ensure_output_dir,
    OUTPUT_DIR,
    safe_filename,
    get_env_token,
    upload_label_to_monday,
)

DEFAULT_BOARD_ID = 9347371455
DEFAULT_ITEM_ID = 11244242150


def main():
    parser = argparse.ArgumentParser(description="Generate one label from a Monday.com item")
    parser.add_argument("--board", type=int, default=DEFAULT_BOARD_ID, help="Monday.com board ID")
    parser.add_argument("--item", type=int, default=DEFAULT_ITEM_ID, help="Monday.com item (pulse) ID")
    parser.add_argument("--upload", action="store_true", help="Upload the PDF to the item's label file column on Monday")
    args = parser.parse_args()

    print(f"Fetching item {args.item} from board {args.board}...")
    try:
        item = fetch_item(args.board, args.item)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    label_data = extract_label_data(item)
    print(f"  Client Name: {label_data['client_name']}")
    desc = label_data["item_description"]
    print(f"  Item Description: {desc[:60] + '...' if len(desc) > 60 else desc}")
    print(f"  PO#: {label_data['po_number']}")

    ensure_output_dir()
    safe_name = safe_filename(f"{label_data['client_name']}_{label_data['po_number']}_{args.item}")
    out_path = OUTPUT_DIR / f"{safe_name}.pdf"
    build_label_pdf(
        label_data["client_name"],
        label_data["item_description"],
        label_data["po_number"],
        out_path,
    )
    print(f"Label saved: {out_path}")

    if args.upload:
        print("Uploading to Monday...")
        try:
            token = get_env_token()
            upload_label_to_monday(args.item, out_path, token)
            print("Uploaded to item's label column on Monday.")
        except Exception as e:
            print(f"Upload failed: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
