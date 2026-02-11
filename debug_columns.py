"""
One-off script to print column_values for an item so we can see the client-name column structure.
Run: python3 debug_columns.py
"""
import json
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from app import fetch_item

BOARD_ID = 9347371455
ITEM_ID = 11244242150

item = fetch_item(BOARD_ID, ITEM_ID)
print("Item name:", item.get("name"))
print("\nClient column - full object:")
for col in item.get("column_values") or []:
    if col.get("id") == "lookup_mkv6padj":
        for k, v in col.items():
            print(f"  {k!r}: {v!r}")
        print("  type =", col.get("type"))
        break
else:
    print("  (lookup_mkv6padj not found)")
