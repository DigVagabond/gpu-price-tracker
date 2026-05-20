# GPU Price Tracker — Setup Guide

Weekly neocloud GPU price tracker for NBIS (Nebius), CRWV (CoreWeave), IREN.
Runs automatically every Monday via GitHub Actions, sends an email report,
and commits updated price history to this repo.

---

## One-time setup (15 minutes)

### Step 1 — Create a GitHub repo

1. Go to github.com → New repository
2. Name it `gpu-price-tracker` (private)
3. Upload all files from this folder to the root of the repo:
   - `gpu_price_fetcher.py`
   - `requirements.txt`
   - `.github/workflows/gpu_tracker.yml`
   - `gpu_prices.json`  ← paste your current snapshot JSON here as the seed file

### Step 2 — Add secrets to GitHub

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these four secrets:

| Secret name        | Value                                      |
|--------------------|---------------------------------------------|
| `ANTHROPIC_API_KEY`| Your Anthropic API key                      |
| `RESEND_API_KEY`   | Your Resend API key (free at resend.com)    |
| `EMAIL_TO`         | Where to send the weekly report             |
| `EMAIL_FROM`       | Verified sender address in Resend           |

**Getting a free Resend key (2 minutes):**
1. Sign up at resend.com — free tier is 3,000 emails/month, 100/day
2. Go to API Keys → Create API Key → Full Access
3. Go to Domains → Add a domain (or use `onboarding@resend.dev` as sender for testing)

### Step 3 — Seed the history file

Paste your current `gpu_prices.json` snapshot into the repo root.
On the first run, the script will append to it. On subsequent runs it
reads the existing SQLite DB from cache and keeps growing the history.

### Step 4 — Trigger a test run

Go to your repo → **Actions → GPU Price Tracker — Weekly Run → Run workflow**

Watch the logs. It should:
- Fetch prices for all 5 GPU families (~60 seconds)
- Append to `gpu_prices_history.sqlite`
- Export updated `gpu_prices.json`
- Send an email report
- Commit the updated JSON back to the repo

---

## Weekly workflow (after setup)

Every Monday at 8am ET the action runs automatically.

To update the dashboard:
1. Go to your repo on GitHub
2. Click `gpu_prices.json`
3. Click the **Raw** button
4. Select all → Copy
5. Paste into the dashboard widget → click **Load prices**

Or automate it further: the dashboard can fetch the raw file directly
from your repo's public URL (if the repo is public) or via GitHub API.

---

## Local testing

```bash
pip install anthropic sendgrid
export ANTHROPIC_API_KEY=your_key
export SENDGRID_API_KEY=your_key
export EMAIL_TO=you@example.com
export EMAIL_FROM=tracker@yourdomain.com
python gpu_price_fetcher.py
```

---

## File structure

```
gpu-price-tracker/
├── .github/
│   └── workflows/
│       └── gpu_tracker.yml     ← GitHub Actions schedule
├── gpu_price_fetcher.py        ← main script
├── requirements.txt            ← anthropic, sendgrid
├── gpu_prices.json             ← exported weekly (committed to repo)
└── README.md
```

`gpu_prices_history.sqlite` is stored in the GitHub Actions cache
(not committed to the repo) and persists between weekly runs.

---

## Cost

| Service         | Cost                                      |
|-----------------|-------------------------------------------|
| GitHub Actions  | Free (2,000 min/month on free tier)       |
| Anthropic API   | ~$0.05–0.10 per weekly run (5 API calls)  |
| SendGrid        | Free (100 emails/day free tier)           |
| **Total**       | **~$0.20–0.40/month**                     |
