"""
gpu_price_fetcher.py
--------------------
Weekly GPU price tracker for neocloud research (NBIS, CRWV, IREN).

Runs automatically every Monday via GitHub Actions. Also runnable locally:
    pip install anthropic resend
    export ANTHROPIC_API_KEY=...
    export SENDGRID_API_KEY=...
    export EMAIL_TO=you@example.com
    export EMAIL_FROM=tracker@yourdomain.com
    python gpu_price_fetcher.py

Outputs:
    gpu_prices.json           — full history export, paste into dashboard
    gpu_prices_history.sqlite — append-only time-series DB
"""

import os, json, sqlite3, datetime, time, textwrap
import anthropic

# ── config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
# Comma-separated list of recipients, e.g. "a@x.com,b@x.com"
EMAIL_TO_RAW      = os.environ.get("EMAIL_TO", "maxim.zhu@mlp.com")
EMAIL_TO_LIST     = [e.strip() for e in EMAIL_TO_RAW.split(",") if e.strip()]
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "gpu-tracker@yourdomain.com")

OUTPUT_JSON = "gpu_prices.json"
HISTORY_DB  = "gpu_prices_history.sqlite"

GPU_FAMILIES = {
    "h100": "H100 SXM5",
    "h200": "H200",
    "b200": "B200",
    "b300": "B300",
    "a100": "A100 80GB",
}

# ── database ──────────────────────────────────────────────────────────────────

def init_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_prices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at    TEXT NOT NULL,
            week_start    TEXT NOT NULL,
            gpu           TEXT NOT NULL,
            provider      TEXT NOT NULL,
            price_od      REAL,
            mkt_floor     REAL,
            trend_pct_12m REAL,
            notes         TEXT
        )
    """)
    conn.commit()
    return conn


def week_start(dt_str):
    dt = datetime.datetime.fromisoformat(dt_str)
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def insert_rows(conn, snapshot, fetched_at):
    ws = week_start(fetched_at)
    for gpu_key, gpu_data in snapshot.items():
        if not gpu_data:
            continue
        for provider, price_od in (gpu_data.get("neocloud") or {}).items():
            conn.execute("""
                INSERT OR IGNORE INTO gpu_prices
                    (fetched_at, week_start, gpu, provider, price_od,
                     mkt_floor, trend_pct_12m, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fetched_at, ws, gpu_key, provider, price_od,
                gpu_data.get("mkt_floor"),
                gpu_data.get("trend_pct_12m"),
                gpu_data.get("notes", ""),
            ))
    conn.commit()


def export_history(conn):
    """
    Export full time-series as dict ready for the dashboard.
    Each gpu entry has:
      - history:       [{week, prices: {provider: price}, mkt_floor}]
      - neocloud:      {provider: price}  ← most recent week
      - mkt_floor, trend_pct_12m, notes  ← from most recent row
    """
    from collections import defaultdict

    rows = conn.execute("""
        SELECT week_start, gpu, provider, price_od, mkt_floor, trend_pct_12m, notes
        FROM gpu_prices
        ORDER BY week_start ASC
    """).fetchall()

    by_gpu  = defaultdict(lambda: defaultdict(dict))
    meta    = defaultdict(dict)

    for week, gpu, provider, price_od, mkt_floor, trend, notes in rows:
        by_gpu[gpu][week][provider] = price_od
        meta[gpu] = {"mkt_floor": mkt_floor, "trend_pct_12m": trend, "notes": notes or ""}

    result = {}
    for gpu, weeks_dict in by_gpu.items():
        sorted_weeks = sorted(weeks_dict.keys())
        history = []
        for w in sorted_weeks:
            row = conn.execute(
                "SELECT mkt_floor FROM gpu_prices WHERE gpu=? AND week_start=? LIMIT 1",
                (gpu, w)
            ).fetchone()
            history.append({
                "week":      w,
                "prices":    weeks_dict[w],
                "mkt_floor": row[0] if row else None,
            })
        result[gpu] = {
            "gpu":            gpu,
            "history":        history,
            "neocloud":       weeks_dict[sorted_weeks[-1]],
            "mkt_floor":      meta[gpu].get("mkt_floor"),
            "trend_pct_12m":  meta[gpu].get("trend_pct_12m"),
            "notes":          meta[gpu].get("notes", ""),
        }
    return result


# ── Claude price fetch ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a GPU cloud pricing data extraction agent.
Search and return ONLY a valid JSON object — no prose, no markdown, no preamble.
Prices are on-demand USD per GPU per hour. Use null for unavailable data.
Primary sources: getdeploying.com, nebius.com/prices, coreweave.com/pricing, lambdalabs.com."""


def fetch_gpu_prices(client, gpu_key, gpu_label):
    prompt = f"""Find CURRENT {gpu_label} on-demand prices (USD/GPU/hr) for:
Nebius, CoreWeave, Lambda Labs, IREN (Iris Energy).
Also: current marketplace spot floor (Vast.ai or RunPod), and 12-month % price change.

Return ONLY this JSON:
{{
  "gpu": "{gpu_key}",
  "gpu_label": "{gpu_label}",
  "fetched_at": "{datetime.datetime.utcnow().isoformat()}",
  "neocloud": {{"Nebius":<n|null>,"CoreWeave":<n|null>,"Lambda":<n|null>,"IREN":<n|null>}},
  "mkt_floor": <n|null>,
  "trend_pct_12m": <n|null>,
  "notes": "<sources used>"
}}"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "\n".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        s, e = text.find("{"), text.rfind("}") + 1
        if s == -1 or e == 0:
            print(f"  ✗ {gpu_label}: no JSON in response")
            return None
        return json.loads(text[s:e])
    except Exception as ex:
        print(f"  ✗ {gpu_label}: {ex}")
        return None


# ── Claude AI summary ─────────────────────────────────────────────────────────

def generate_summary(client, snapshot):
    lines = []
    for gk, d in snapshot.items():
        if not d:
            continue
        neo  = d.get("neocloud", {})
        vals = [v for v in neo.values() if v is not None]
        avg  = sum(vals) / len(vals) if vals else None
        lo   = min(vals) if vals else None
        hi   = max(vals) if vals else None
        floor = d.get("mkt_floor")
        trend = d.get("trend_pct_12m")
        spread = f"{((avg/floor - 1)*100):.0f}%" if avg and floor else "n/a"
        lines.append(
            f"{d.get('gpu_label', gk)}: avg ${avg:.2f}/hr (range ${lo:.2f}–${hi:.2f}), "
            f"mkt floor ${floor:.2f}/hr, spread {spread}, "
            f"12m {'%+.0f' % trend + '%' if trend is not None else 'n/a'}"
            if avg else f"{d.get('gpu_label', gk)}: no data"
        )

    prompt = (
        "Weekly GPU pricing data (neocloud on-demand, May 2026):\n"
        + "\n".join(lines)
        + "\n\nWrite a concise 4-paragraph investment-grade analysis covering: "
        "(1) overall GPU pricing environment across all families and what the "
        "neocloud-to-floor spread signals about pricing power, "
        "(2) CRWV (CoreWeave) margin and pricing power implications, "
        "(3) NBIS (Nebius) and IREN commentary, "
        "(4) one forward-looking risk or catalyst. "
        "No markdown, plain paragraphs only."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system="You are a GPU cloud pricing analyst covering neocloud equity research.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "\n\n".join(b.text for b in resp.content if hasattr(b, "text"))
    except Exception as ex:
        return f"[Summary generation failed: {ex}]"


# ── Email via Resend ──────────────────────────────────────────────────────────

def build_html_email(snapshot, summary, fetched_at):
    date_str = datetime.datetime.fromisoformat(fetched_at).strftime("%B %d, %Y")

    # snapshot table rows
    rows = ""
    for gk, d in snapshot.items():
        if not d:
            continue
        neo   = d.get("neocloud", {})
        vals  = [v for v in neo.values() if v is not None]
        avg   = sum(vals) / len(vals) if vals else None
        floor = d.get("mkt_floor")
        trend = d.get("trend_pct_12m")
        label = d.get("gpu_label", gk)
        caveat = " ⚠" if gk == "b300" else ""

        cells = "".join(
            f"<td style='padding:6px 12px;'>"
            f"{'$'+str(round(neo.get(p, None) or 0, 2)) if neo.get(p) else '—'}"
            f"</td>"
            for p in ["Nebius", "CoreWeave", "Lambda", "IREN"]
        )
        trend_col = (
            f"<span style='color:{'#c0392b' if trend and trend >= 0 else '#27ae60'}'>"
            f"{'%+.0f' % trend}%</span>" if trend is not None else "—"
        )
        rows += f"""
        <tr style='border-bottom:1px solid #eee;'>
          <td style='padding:6px 12px;font-weight:500;'>{label}{caveat}</td>
          {cells}
          <td style='padding:6px 12px;'>{'$'+str(round(avg,2)) if avg else '—'}</td>
          <td style='padding:6px 12px;color:#888;'>{'$'+str(round(floor,2)) if floor else '—'}</td>
          <td style='padding:6px 12px;'>{trend_col}</td>
        </tr>"""

    # summary paragraphs
    paras = "".join(
        f"<p style='margin:0 0 12px;line-height:1.7;'>{p.strip()}</p>"
        for p in summary.split("\n\n") if p.strip()
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:680px;margin:0 auto;padding:24px;">

<h2 style="margin:0 0 4px;font-size:18px;">GPU Price Tracker</h2>
<p style="margin:0 0 24px;color:#888;font-size:12px;">Week of {date_str} &nbsp;·&nbsp; neocloud on-demand (USD/GPU/hr)</p>

<h3 style="font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#555;margin:0 0 8px;">Current snapshot</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">
  <thead>
    <tr style="background:#f5f5f5;text-align:left;">
      <th style="padding:7px 12px;font-weight:500;">GPU</th>
      <th style="padding:7px 12px;font-weight:500;">Nebius</th>
      <th style="padding:7px 12px;font-weight:500;">CoreWeave</th>
      <th style="padding:7px 12px;font-weight:500;">Lambda</th>
      <th style="padding:7px 12px;font-weight:500;">IREN</th>
      <th style="padding:7px 12px;font-weight:500;">Neo avg</th>
      <th style="padding:7px 12px;font-weight:500;">Mkt floor</th>
      <th style="padding:7px 12px;font-weight:500;">12m Δ</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>

<h3 style="font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#555;margin:0 0 12px;">AI pricing power analysis — NBIS · CRWV · IREN</h3>
<div style="background:#fafafa;border-left:3px solid #ddd;padding:14px 16px;margin-bottom:24px;font-size:13px;">
  {paras}
</div>

<p style="font-size:11px;color:#aaa;margin:0;">
  Sources: nebius.com/prices · getdeploying.com · coreweave.com/pricing · lambdalabs.com · SemiAnalysis H100 index ·
  B300 note: 6% confirmed stock — treat as indicative. Azure NDv5 and GB200 excluded from comp group.<br>
  Generated by gpu_price_fetcher.py · {fetched_at}
</p>

</body>
</html>"""


def send_email(html_body, subject):
    if not GMAIL_APP_PASSWORD or not EMAIL_TO_LIST or not EMAIL_FROM:
        print("  ⚠ GMAIL_APP_PASSWORD / EMAIL_TO / EMAIL_FROM not set — skipping email.")
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = ", ".join(EMAIL_TO_LIST)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO_LIST, msg.as_string())

        print(f"  ✓ Email sent → {', '.join(EMAIL_TO_LIST)}")
    except Exception as ex:
        print(f"  ✗ Email failed: {ex}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not ANTHROPIC_API_KEY:
        raise SystemExit(
            "ERROR: ANTHROPIC_API_KEY not set.\n"
            "Run:  export ANTHROPIC_API_KEY=your_key_here"
        )

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    conn       = init_db(HISTORY_DB)
    fetched_at = datetime.datetime.utcnow().isoformat()
    snapshot   = {}

    print(f"\nGPU Price Tracker — {fetched_at}")
    print("=" * 52)

    # ── 1. fetch prices ───────────────────────────────────────────────────────
    for gpu_key, gpu_label in GPU_FAMILIES.items():
        print(f"\n→ Fetching {gpu_label}…")
        data = fetch_gpu_prices(client, gpu_key, gpu_label)
        if data:
            snapshot[gpu_key] = data
            neo  = data.get("neocloud", {})
            vals = [v for v in neo.values() if v is not None]
            avg  = sum(vals) / len(vals) if vals else None
            print(f"  ✓ avg ${avg:.2f}/hr" if avg else "  ✓ partial data")
            for p, v in neo.items():
                print(f"    {p:<14} {'$'+str(v)+'/hr' if v else '—'}")
            if data.get("mkt_floor"):
                print(f"    Mkt floor      ${data['mkt_floor']:.2f}/hr spot")
        else:
            snapshot[gpu_key] = None
        time.sleep(2)

    # ── 2. append to DB ───────────────────────────────────────────────────────
    insert_rows(conn, snapshot, fetched_at)
    ws = week_start(fetched_at)
    print(f"\n✓ Appended to {HISTORY_DB}  (week {ws})")

    # ── 3. export history JSON ────────────────────────────────────────────────
    history = export_history(conn)
    conn.close()

    output = {
        "fetched_at": fetched_at,
        "mode":       "history",
        "gpus":       history,
    }
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)
    print(f"✓ Saved {OUTPUT_JSON}")

    # ── 4. generate AI summary ────────────────────────────────────────────────
    print("\n→ Generating AI pricing power summary…")
    summary = generate_summary(client, snapshot)
    print("✓ Summary generated")

    # ── 5. send email ─────────────────────────────────────────────────────────
    date_str  = datetime.datetime.fromisoformat(fetched_at).strftime("%b %d, %Y")
    subject   = f"GPU Price Tracker — Week of {date_str}"
    html_body = build_html_email(snapshot, summary, fetched_at)
    print(f"\n→ Sending email to {', '.join(EMAIL_TO_LIST) or '(not configured)'}…")
    send_email(html_body, subject)

    # ── 6. print paste-ready JSON ─────────────────────────────────────────────
    print("\n" + "=" * 52)
    print("PASTE THIS INTO THE DASHBOARD:")
    print("=" * 52)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
