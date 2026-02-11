# Using Make.com with Monday.com Label Export

You can use **Make.com** as the webhook receiver instead of pointing Monday directly at your app. Make receives the trigger from Monday, then forwards it to your label-export app.

---

## Option A: Import the blueprint (if it works)

1. In Make.com, create a **new scenario**.
2. Click the **three dots** (top right) → **Import blueprint**.
3. Choose the file: `make-com-monday-label-webhook-blueprint.json`.
4. After import:
   - Open the **HTTP** module (second module).
   - Replace `https://YOUR_APP_URL/webhook/monday` with your real app URL, e.g.:
     - `https://your-app.up.railway.app/webhook/monday`
     - or your ngrok URL: `https://xxxx.ngrok.io/webhook/monday`
5. **Save** the scenario.
6. Turn the scenario **ON**. Make will show you the **Webhook URL** (in the first module). Copy it.
7. In **Monday.com** → your board → **Automations**: set the “Send webhook” action URL to this **Make.com webhook URL** (not the app URL).

When someone sets Job Status to “Preparing for Shipping”, Monday sends the webhook to Make, and Make forwards it to your app. Your app generates the label and uploads it to Monday.

---

## Option B: Build the scenario manually (if import fails or you prefer)

1. In Make.com, create a **new scenario**.

2. **Add trigger:** Click the **+** → **Webhooks** → **Custom webhook**.
   - Click **Add** / **Create webhook**. Make gives you a URL. Copy it for step 6.

3. **Add action:** Click the **+** on the right of the webhook → **HTTP** → **Make a request**.
   - **URL:** Your app’s webhook URL, e.g. `https://your-app.up.railway.app/webhook/monday`
   - **Method:** POST
   - **Body type:** Raw
   - **Content type:** application/json
   - **Request content:** Click in the field and map **Body** from the webhook module (the first module). It’s usually something like `{{1.body}}` or “Body” from the webhook.

4. **Save** and turn the scenario **ON**.

5. In **Monday.com**, in your automation’s “Send webhook” step, set the URL to the **Make.com webhook URL** from step 2.

Done. Monday → Make (webhook) → Your app (generates label and uploads to Monday).

---

## Summary

| What | Where |
|------|--------|
| **Monday automation** | Sends webhook to **Make.com webhook URL** |
| **Make.com** | Receives webhook, sends same body to **your app URL** |
| **Your app** | Runs at your hosted URL; generates PDF and uploads to Monday |

Replace `YOUR_APP_URL` (or the URL in the HTTP module) with wherever your label-export app is running (e.g. Railway, Render, or ngrok when testing).
