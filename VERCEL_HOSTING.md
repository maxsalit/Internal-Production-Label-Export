# Step-by-step: Host your Label Export app on Vercel

Vercel will give your app a URL like `https://your-project.vercel.app`. You’ll point Monday’s automation at the webhook URL so it works 24/7.

---

## What you’ll need

- This **Monday.com Label Export** project on your computer  
- A **GitHub** account (free)  
- A **Vercel** account (free at [vercel.com](https://vercel.com))  
- About 10 minutes  

---

## Step 1: Put your code on GitHub

Vercel deploys from GitHub.

1. Go to [github.com](https://github.com) and sign in (or create an account).
2. Click **+** (top right) → **New repository**.
3. **Repository name:** e.g. `monday-label-export`.
4. Leave it **Public**. Do **not** add README, .gitignore, or license.
5. Click **Create repository**.

Then on your Mac:

6. Open **Terminal** and run (replace `YOUR_USERNAME` with your GitHub username):

   ```bash
   cd "/Users/maxwellsalit/Cursor Projects/Monday.com Label Export"
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/maxsalit/monday-label-export.git
   git push -u origin main
   ```

   Your `.env` is in `.gitignore`, so your token will **not** be pushed to GitHub.

---

## Step 2: Create a Vercel project from GitHub

1. Go to [vercel.com](https://vercel.com) and sign in (e.g. **Continue with GitHub**).
2. Click **Add New…** → **Project**.
3. **Import** the repository you created (e.g. `monday-label-export`). Click **Import**.
4. Leave the default settings (Vercel will detect the project). Do **not** change the root directory.
5. Click **Deploy**. Wait until the deployment finishes (usually 1–2 minutes).

You’ll get a URL like `https://monday-label-export-xxxx.vercel.app`. We still need to add your API token.

---

## Step 3: Add your Monday API token on Vercel

1. In Vercel, open your **project** (the one you just deployed).
2. Go to **Settings** → **Environment Variables**.
3. Click **Add** (or **Add New**).
4. **Name:** `MONDAY_API_TOKEN`  
   **Value:** your Monday.com API token (same as in your local `.env`).
5. Select **Production** (and optionally **Preview** if you use branch deploys).
6. Click **Save**.
7. **Redeploy** so the new variable is used: go to **Deployments** → click the **⋯** on the latest deployment → **Redeploy**.

---

## Step 4: Get your webhook URL

Your webhook URL is:

**`https://YOUR-VERCEL-URL/api/webhook/monday`**

Example: if your project URL is `https://monday-label-export-xxxx.vercel.app`, then:

**`https://monday-label-export-xxxx.vercel.app/api/webhook/monday`**

Optional check: open **`https://YOUR-VERCEL-URL/api/health`** in a browser. You should see `{"status":"ok"}`.

---

## Step 5: Point Monday’s automation at Vercel

1. In **Monday.com**, open your board → **Automations** (or **Integrations** → **Automations**).
2. Open the automation that runs when **Job Status** = **“Preparing for Shipping”**.
3. In the **Send webhook** (or **HTTP request**) action, set:
   - **URL:** `https://YOUR-VERCEL-URL/api/webhook/monday` (from Step 4).
   - **Method:** POST.
   - **Body:** same as before (e.g. `boardId` and `pulseId`, or “Include item details”).
4. **Save** the automation.

When someone sets an item to “Preparing for Shipping,” Monday will call Vercel, and your app will generate the label and upload it to the item’s file column.

---

## Quick reference

| Step            | Where      | What to do |
|-----------------|------------|------------|
| Code on GitHub  | github.com | New repo → push this folder (no `.env`). |
| Deploy          | vercel.com | Add New → Project → Import repo → Deploy. |
| API token       | Vercel     | Settings → Environment Variables → `MONDAY_API_TOKEN`. |
| Webhook URL     | —          | `https://YOUR-PROJECT.vercel.app/api/webhook/monday` |
| Monday          | Monday.com | Automation webhook URL = the URL above. |

---

## Notes

- **Timeout:** The webhook is allowed to run up to 60 seconds (set in `vercel.json`). If you hit timeouts, check Vercel’s plan limits.
- **No local `labels/` on Vercel:** On Vercel the app writes PDFs to `/tmp` (temporary), then uploads them to Monday. Nothing is stored on disk between requests.
- **Same code locally:** You can still run `python3 app.py` and `python3 test_label.py --upload` on your Mac; the same logic runs on Vercel at `/api/webhook/monday`.

---

## If something goes wrong

- **“Application error” / 500:** Check **Vercel** → **Deployments** → latest run → **Functions** or **Logs** for the error. Often it’s a missing or wrong `MONDAY_API_TOKEN`.
- **401 / “No access”:** In Vercel → **Settings** → **Environment Variables**, confirm `MONDAY_API_TOKEN` is set for Production and that you redeployed after adding it.
- **Monday says webhook failed:** Confirm the URL is exactly `https://YOUR-PROJECT.vercel.app/api/webhook/monday` (with `https`, no trailing slash, and `/api/webhook/monday` at the end).
