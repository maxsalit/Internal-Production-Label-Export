# Monday.com Label Export

When **Job Status** on a Monday.com board changes to **"Preparing for Shipping"**, this tool receives a webhook, fetches that item’s **Client Name**, **Item Description** (pulse name), and **PO#**, and generates a PDF label. The PDF is saved in the `labels/` folder and **automatically uploaded to the item’s label file column** on Monday (column ID: `file_mm0fzm60`).

---

## Where does the code live? Is it in Monday.com?

**No.** The code lives **only on your computer** (in the folder “Monday.com Label Export”). Monday.com does **not** run or store this code.

- **Monday.com** has: your board, the automation (“when Job Status = Preparing for Shipping, send a webhook”), and the **URL** you put in that automation (e.g. `https://something.ngrok.io/webhook/monday`).
- **Your app** runs somewhere that URL points to: either your Mac (with a tunnel like ngrok) or a server on the internet. When Monday sends the webhook to that URL, your app runs, builds the PDF, and uploads it to Monday.

So: code = on your machine (or a server you use). Monday = just triggers your app via a URL and receives the uploaded file.

---

## Hosting: keep it local or put it online?

**Option 1 – Run it on your Mac (local)**  
- You run `python3 app.py` and leave it running, and use **ngrok** so Monday.com can reach your Mac.  
- **Pros:** Simple, no extra cost, everything stays on your machine.  
- **Cons:** Your Mac must stay on and the app + ngrok must keep running. If the Mac sleeps, restarts, or loses internet, the webhook will fail when someone sets “Preparing for Shipping.” On ngrok’s free plan the URL changes each time you restart ngrok, so you’d need to update the URL in the Monday automation.

**Option 2 – Run it on a small server online (recommended for “set and forget”)**  
- You put the same code on a hosting service (e.g. **Railway**, **Render**, or **Fly.io**). They give you a permanent URL. You set that URL in Monday’s automation once and leave it.  
- **Pros:** Works 24/7 even when your computer is off. URL stays the same.  
- **Cons:** You follow the host’s steps to deploy (usually: create account, connect GitHub or upload the folder, add `MONDAY_API_TOKEN` in their dashboard, deploy). Some have a free tier.

**Summary:**  
- **Local is fine** for testing or if you’re okay leaving your Mac on and updating the webhook URL when ngrok changes.  
- **Hosting online is smarter** if you want it to work whenever anyone changes the status, without depending on your computer.

---

## Security: API token

**Do not put your Monday.com API token in the code.** Use a `.env` file (and never commit it).

1. Copy the example file:  
   `cp .env.example .env`
2. Open `.env` and set your token:  
   `MONDAY_API_TOKEN=paste_your_token_here`
3. Keep `.env` only on your machine. Add `.env` to `.gitignore` if you use git.

If you ever shared your token, regenerate it in Monday.com (Profile → Developers → API) and update `.env`.

## Setup

1. **Python 3.8+** required.

2. **Install dependencies:**
   ```bash
   cd "Monday.com Label Export"
   pip install -r requirements.txt
   ```

3. **Create `.env`** with `MONDAY_API_TOKEN` (see above).

4. **Run the server** (for local testing you’ll expose it with a tunnel; see below):
   ```bash
   python app.py
   ```
   Server runs at `http://0.0.0.0:5000`.  
   - Webhook URL: `http://YOUR_PUBLIC_URL/webhook/monday`  
   - Health check: `http://YOUR_PUBLIC_URL/health`

5. **Expose the server to the internet** so Monday.com can send the webhook:
   - **Option A – ngrok (quick test):**  
     Install [ngrok](https://ngrok.com), then run:  
     `ngrok http 5000`  
     Use the HTTPS URL it gives you (e.g. `https://abc123.ngrok.io/webhook/monday`).
   - **Option B – Deploy** to a server or cloud (e.g. a small VPS or PaaS) and use that app’s HTTPS URL.

## Monday.com automation

1. In your board, open **Automations** (or **Integrations** → **Automations**).
2. Add a trigger: **When a column value changes** → choose the **Job Status** column → set value to **"Preparing for Shipping"** (or the exact status text you use).
3. Add an action: **Send webhook** (or **HTTP request**).
4. Configure the webhook:
   - **URL:** `https://YOUR_PUBLIC_URL/webhook/monday` (must be HTTPS if your Monday.com plan requires it).
   - **Method:** POST.
   - **Body:** JSON. The app expects the item that triggered the automation to be identified by **board id** and **item (pulse) id**.  
     If your automation lets you pick “Include item details” or “Send item and board,” enable that.  
     If you must build the body yourself, send at least:
     - `boardId` – number (your board id).
     - `pulseId` – number (the item/pulse id that changed).

   Example minimal body (if your automation doesn’t auto-fill it), using your board:
   ```json
   { "boardId": 9347371455, "pulseId": 11244242150 }
   ```
   Use the **item (pulse) id of the row that triggered the automation** (e.g. the one whose Job Status changed). Board id is always `9347371455` for this board.

5. Save the automation. When you change an item’s Job Status to “Preparing for Shipping,” Monday.com will POST to your URL and a PDF will be generated.

## Board setup

Your Monday.com board should have:

- **Client Name** – column with that exact title (or “client name”).
- **PO#** – column titled “PO#”, “PO Number”, or “PO”.
- **Item name** – used as “Item Description” on the label (the pulse/item title).

The label PDF uses: **Client Name** (bold), **Item Description** (multi-line from the item name), and **PO# PO Number** with the value (and “PO#” prefix if needed).

## Test without webhook

To confirm your token and column mapping work, generate a label for a single item from the command line:

```bash
python test_label.py
```

This uses board `9347371455` and item `11244242150` by default. To use a different item:

```bash
python3 test_label.py --board 9347371455 --item ITEM_ID
```

To also upload the generated PDF to that item’s label file column on Monday (same as the webhook does):

```bash
python3 test_label.py --upload
```

The PDF is written to `labels/` and the script prints the path.

## Output

- PDFs are written to the **`labels/`** folder next to `app.py`.
- Filename format: `ClientName_PO#_itemId.pdf` (unsafe characters replaced).

## Troubleshooting

- **401 / “No access”:** Check that `MONDAY_API_TOKEN` in `.env` is correct and has access to the board.
- **404 “Item not found”:** The webhook must send the correct `boardId` and `pulseId` for the item whose status changed.
- **Webhook not firing:** Confirm the automation trigger (column + value) matches exactly, and that the webhook URL is reachable from the internet (test with `/health` in the browser).
- **Wrong columns:** Ensure column titles on the board match “Client Name” and “PO#” (or “PO Number” / “PO”) as above.
