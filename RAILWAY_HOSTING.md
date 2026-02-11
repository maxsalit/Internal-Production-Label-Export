# Step-by-step: Host your Label Export app on Railway

Railway will give your app a permanent URL (like `https://your-app.railway.app`). You’ll point Monday’s automation at that URL so it works 24/7 without your computer.

---

## What you’ll need

- Your **Monday.com Label Export** folder (this project) on your computer  
- A **GitHub** account (free)  
- A **Railway** account (free tier is enough)  
- About 10 minutes  

---

## Step 1: Put your code on GitHub

Railway deploys from GitHub, so we need to upload this project there once.

1. Go to [github.com](https://github.com) and sign in (or create an account).
2. Click the **+** (top right) → **New repository**.
3. **Repository name:** e.g. `monday-label-export`.
4. Leave it **Public**. Don’t add a README, .gitignore, or license (we already have files).
5. Click **Create repository**.

You’ll see a page with “push an existing repository from the command line.” We’ll do that from your computer.

6. **On your Mac:** Open **Terminal** (search “Terminal” in Spotlight).
7. Go to your project folder and create a git repo, then add GitHub and push:

   ```bash
   cd "/Users/maxwellsalit/Cursor Projects/Monday.com Label Export"
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/monday-label-export.git
   git push -u origin main
   ```

   Replace `YOUR_USERNAME` with your actual GitHub username. If GitHub asks you to sign in, use your GitHub email and a **Personal Access Token** as the password (GitHub → Settings → Developer settings → Personal access tokens → Generate new token).

After this, your code is on GitHub. You won’t put your `.env` file in the repo (it’s in `.gitignore`), so your token stays off GitHub.

---

## Step 2: Create a Railway project and deploy from GitHub

1. Go to [railway.app](https://railway.app) and sign in (e.g. “Login with GitHub”).
2. Click **New Project**.
3. Choose **Deploy from GitHub repo**.
4. If asked, **Connect your GitHub account** and allow Railway to see your repos.
5. Select the repo you just created (e.g. `monday-label-export`).
6. Railway will detect the app and start a deploy. Wait until it finishes (you’ll see a green check or “Success”).

You now have a running app, but we still need to add your Monday API token and get the public URL.

---

## Step 3: Add your Monday API token on Railway

1. In Railway, click your **service** (the box that represents your app).
2. Open the **Variables** tab (or **Settings** → **Variables**).
3. Click **+ New Variable** or **Add variable**.
4. **Variable name:** `MONDAY_API_TOKEN`  
   **Value:** paste your Monday.com API token (the same one you have in your local `.env`).
5. Save. Railway will redeploy once; wait for it to finish.

Your token is now set only on Railway, not in the code.

---

## Step 4: Get your app’s public URL

1. In the same service in Railway, open the **Settings** tab (or the **Deployments** / **Networking** area).
2. Find **Public Networking** or **Generate domain** (wording can vary).
3. Click **Generate domain** (or **Add domain**). Railway will assign a URL like:
   - `https://monday-label-export-production-xxxx.up.railway.app`
4. **Copy that full URL.** You’ll use it in the next step.

Your webhook URL will be: **that URL + `/webhook/monday`**  
Example: `https://monday-label-export-production-xxxx.up.railway.app/webhook/monday`

Optional check: open in a browser **your-url/health** — you should see `{"status":"ok"}`.

---

## Step 5: Point Monday’s automation at Railway

1. In **Monday.com**, open your board and go to **Automations** (or **Integrations** → **Automations**).
2. Open the automation that runs when **Job Status** changes to **“Preparing for Shipping”**.
3. Find the **Send webhook** (or **HTTP request**) action.
4. Set the **URL** to your Railway webhook URL:  
   `https://YOUR-RAILWAY-URL/webhook/monday`  
   (the exact URL you copied in Step 4, with `/webhook/monday` at the end).
5. **Method:** POST.  
   **Body:** same as before (e.g. include `boardId` and `pulseId`, or “Include item details” if your automation has that).
6. **Save** the automation.

Done. When someone sets an item to “Preparing for Shipping,” Monday will call Railway, and your app will generate the label and upload it to the item’s file column.

---

## Quick reference

| Step              | Where        | What to do |
|-------------------|-------------|------------|
| Code on GitHub     | github.com  | New repo → push this folder (no `.env`). |
| Deploy            | railway.app | New Project → Deploy from GitHub → pick your repo. |
| API token         | Railway     | Variables → `MONDAY_API_TOKEN` = your token. |
| Webhook URL       | Railway     | Generate domain → copy URL → use `URL/webhook/monday`. |
| Monday automation | Monday.com  | Webhook action URL = `https://your-railway-url/webhook/monday`. |

---

## If something goes wrong

- **“Application failed to respond” / 502:** Wait 1–2 minutes after deploy, then try again. Check Railway’s **Deployments** tab for errors.
- **Labels not uploading / 401:** In Railway → Variables, confirm `MONDAY_API_TOKEN` is set correctly (no extra spaces, full token).
- **Monday says webhook failed:** Confirm the URL is exactly `https://YOUR-RAILWAY-DOMAIN/webhook/monday` and that the automation sends a POST with `boardId` and `pulseId` (or item details).

You can always run the app locally with `python3 app.py` and test with `python3 test_label.py --upload`; the same code runs on Railway.
