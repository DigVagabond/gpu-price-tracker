"""
gpu_price_fetcher.py
--------------------
Weekly GPU price tracker for neocloud research (NBIS, CRWV, IREN).
Tracks H100, H200, B200, B300 on-demand prices across Nebius, CoreWeave, Lambda, IREN.

Runs automatically every Monday via GitHub Actions. Also runnable locally:
    pip install anthropic matplotlib
    export ANTHROPIC_API_KEY=...
    export GMAIL_APP_PASSWORD=...
    export EMAIL_TO=you@example.com
    export EMAIL_FROM=youraddress@gmail.com
    python gpu_price_fetcher.py

Outputs:
    gpu_prices.json           — full history export, paste into dashboard
    gpu_prices_history.sqlite — append-only time-series DB (cached in GitHub Actions)
"""

import os, json, sqlite3, datetime, time, base64, io
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
            model="claude-haiku-4-5-20251001",
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

    current_month = datetime.datetime.utcnow().strftime("%B %Y")
    prompt = (
        f"Weekly GPU pricing (neocloud on-demand, {current_month}):\n"
        + "\n".join(lines)
        + "\n\nWrite 4 short paragraphs: (1) overall pricing environment and "
        "what the neocloud-to-floor spread signals about pricing power, "
        "(2) CRWV (CoreWeave) implications, "
        "(3) NBIS (Nebius) and IREN commentary, "
        "(4) one key risk or catalyst. "
        "Plain text, no markdown."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system="You are a GPU cloud pricing analyst covering neocloud equity research.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "\n\n".join(b.text for b in resp.content if hasattr(b, "text"))
    except Exception as ex:
        return f"[Summary generation failed: {ex}]"


# ── Chart generation ──────────────────────────────────────────────────────────

GPU_COLORS = {
    "h100": "#185FA5",
    "h200": "#0F6E56",
    "b200": "#3B6D11",
    "b300": "#7C3D8C",
    "a100": "#854F0B",
}

PROVIDER_COLORS = {
    "Nebius":    "#185FA5",
    "CoreWeave": "#0F6E56",
    "Lambda":    "#854F0B",
    "IREN":      "#3B6D11",
}

# Historical shape params for back-filling estimated weeks
HIST_PARAMS = {
    "h100": {"growth12m":  0.25, "vol": 0.03, "shape": "dip_rise",    "start_frac": 0.0 },
    "h200": {"growth12m":  0.25, "vol": 0.04, "shape": "dip_rise",    "start_frac": 0.0 },
    "b200": {"growth12m":  0.06, "vol": 0.12, "shape": "spike_mar26", "start_frac": 0.0 },
    "b300": {"growth12m":  0.70, "vol": 0.18, "shape": "new_entry",   "start_frac": 0.65},
    "a100": {"growth12m": -0.20, "vol": 0.03, "shape": "decline",     "start_frac": 0.0 },
}
MKT_RATIO = {"h100": 1.15, "h200": 1.20, "b200": 0.90, "b300": 1.50, "a100": 0.75}


def _backfill(gpu_key, current_price, n_weeks):
    """Back-calculate estimated history from current real price."""
    import math
    p = HIST_PARAMS[gpu_key]
    start_week = int(p["start_frac"] * n_weeks)
    usable = n_weeks - start_week
    start_val = current_price / (1 + p["growth12m"] * (usable / 52))
    series = [None] * n_weeks
    for i in range(start_week, n_weeks):
        t = (i - start_week) / max(usable - 1, 1)
        if p["shape"] == "dip_rise":
            v = start_val + (current_price - start_val) * t + math.sin(t * math.pi) * -0.08 * start_val
        elif p["shape"] == "spike_mar26":
            base = start_val + (current_price - start_val) * t
            if 0.82 < t < 0.94:
                base += base * 0.24 * math.sin((t - 0.82) / 0.12 * math.pi)
            v = base
        elif p["shape"] == "new_entry":
            v = start_val + (current_price - start_val) * (t * t)
        else:
            v = start_val + (current_price - start_val) * t
        noise = math.sin(hash(gpu_key) * 7 + i * 13) * 0.5 * p["vol"] * start_val
        series[i] = round(max(v + noise, 0.2), 2)
    series[-1] = current_price  # pin last point to real value
    return series


def generate_charts(history_gpus, n_weeks=52):
    """
    Generate one chart per GPU family.
    Returns dict: {gpu_key: base64_png_string}
    Each chart shows:
      - Shaded band: neocloud min/max range (estimated weeks faded)
      - Solid line:  neocloud avg (estimated dashed, real solid)
      - Dotted line: marketplace floor
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend, required for server
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np
    except ImportError:
        print("  ⚠ matplotlib not installed — skipping charts")
        return {}

    # build week labels (last n_weeks Mondays ending today)
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    week_dates = [monday - datetime.timedelta(weeks=n_weeks - 1 - i) for i in range(n_weeks)]

    charts = {}

    for gpu_key, meta_label in GPU_FAMILIES.items():
        gpu_data = history_gpus.get(gpu_key)
        if not gpu_data:
            continue

        # real history weeks from DB
        real_history = gpu_data.get("history", [])  # [{week, prices, mkt_floor}]
        n_real = len(real_history)
        n_est  = n_weeks - n_real

        # collect providers with any real data
        providers = [p for p, v in (gpu_data.get("neocloud") or {}).items() if v is not None]
        if not providers:
            continue

        # ── build per-provider full series ────────────────────────────────────
        all_series = {}
        for prov in providers:
            current = gpu_data["neocloud"][prov]
            est_part  = _backfill(gpu_key, current, n_est + 1)[:n_est]
            real_part = []
            for wk in real_history:
                real_part.append(wk["prices"].get(prov))
            all_series[prov] = est_part + real_part

        # avg / lo / hi across providers per week
        avg_s, lo_s, hi_s = [], [], []
        for i in range(n_weeks):
            vals = [all_series[p][i] for p in providers if all_series[p][i] is not None]
            if vals:
                avg_s.append(sum(vals) / len(vals))
                lo_s.append(min(vals))
                hi_s.append(max(vals))
            else:
                avg_s.append(None)
                lo_s.append(None)
                hi_s.append(None)

        # marketplace floor series
        current_floor = gpu_data.get("mkt_floor")
        if current_floor:
            ratio     = MKT_RATIO.get(gpu_key, 1.0)
            mkt_start = current_floor / ratio
            est_mkt   = [mkt_start + (current_floor - mkt_start) * (i / max(n_est - 1, 1))
                         for i in range(n_est)]
            real_mkt  = [wk.get("mkt_floor") or current_floor for wk in real_history]
            mkt_s     = est_mkt + real_mkt
        else:
            mkt_s = [None] * n_weeks

        # ── plot ──────────────────────────────────────────────────────────────
        color = GPU_COLORS.get(gpu_key, "#555")
        fig, ax = plt.subplots(figsize=(6.5, 2.8))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#fafafa")

        x = list(range(n_weeks))

        # shaded band (estimated = light, real = slightly stronger)
        def fill_segment(start, end, alpha):
            xs = x[start:end]
            los = lo_s[start:end]
            his = hi_s[start:end]
            if any(v is not None for v in los):
                los_c = [v if v is not None else float("nan") for v in los]
                his_c = [v if v is not None else float("nan") for v in his]
                ax.fill_between(xs, los_c, his_c, color=color, alpha=alpha, linewidth=0)

        fill_segment(0, n_est, 0.07)
        fill_segment(n_est - 1, n_weeks, 0.18)

        # avg line — estimated (dashed, faded) then real (solid)
        def plot_segment(start, end, ls, lw, alpha, label=None):
            xs = x[start:end]
            ys = [avg_s[i] if avg_s[i] is not None else float("nan") for i in range(start, end)]
            ax.plot(xs, ys, color=color, linestyle=ls, linewidth=lw,
                    alpha=alpha, label=label, solid_capstyle="round")

        plot_segment(0, n_est + 1, "--", 1.2, 0.45, label="Neocloud avg (est.)")
        plot_segment(n_est - 1, n_weeks, "-", 2.0, 1.0, label="Neocloud avg (real)")

        # real data points
        if n_real > 0:
            rx = x[n_est:]
            ry = [avg_s[i] for i in range(n_est, n_weeks) if avg_s[i] is not None]
            rx = [x[n_est + j] for j, v in enumerate(avg_s[n_est:]) if v is not None]
            ax.scatter(rx, ry, color=color, s=18, zorder=5)

        # marketplace floor
        mkt_valid = [v for v in mkt_s if v is not None]
        if mkt_valid:
            mkt_plot = [v if v is not None else float("nan") for v in mkt_s]
            ax.plot(x, mkt_plot, color="#aaa", linestyle=":", linewidth=1.3,
                    alpha=0.8, label="Mkt floor (spot)")

        # vertical divider between estimated and real
        if n_real > 0 and n_est > 0:
            ax.axvline(x=n_est - 0.5, color="#ccc", linewidth=0.8, linestyle="--")
            ax.text(n_est - 1, ax.get_ylim()[1] * 0.97, "est.",
                    fontsize=7, color="#bbb", ha="right", va="top")
            ax.text(n_est + 0.3, ax.get_ylim()[1] * 0.97, "real",
                    fontsize=7, color="#888", ha="left", va="top")

        # x-axis ticks — show ~6 evenly spaced labels
        tick_step = max(1, n_weeks // 6)
        tick_idx  = list(range(0, n_weeks, tick_step)) + [n_weeks - 1]
        tick_lbls = [week_dates[i].strftime("%b %d") for i in tick_idx]
        ax.set_xticks(tick_idx)
        ax.set_xticklabels(tick_lbls, fontsize=7, color="#888")

        ax.yaxis.set_tick_params(labelsize=7, labelcolor="#888")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:.2f}"))
        ax.set_ylabel("$/GPU/hr", fontsize=7, color="#888")
        ax.set_title(meta_label, fontsize=10, fontweight="bold", color="#333", pad=6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#ddd")
        ax.grid(axis="y", color="#eee", linewidth=0.6)

        # legend
        handles = [
            mpatches.Patch(color=color, alpha=0.9, label="Neocloud avg"),
            mpatches.Patch(color=color, alpha=0.2, label="Min/max range"),
        ]
        if mkt_valid:
            handles.append(mpatches.Patch(color="#aaa", label="Mkt floor"))
        ax.legend(handles=handles, fontsize=7, framealpha=0, loc="upper left",
                  ncol=len(handles))

        plt.tight_layout(pad=0.8)

        # encode to base64
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="white")
        plt.close(fig)
        buf.seek(0)
        charts[gpu_key] = base64.b64encode(buf.read()).decode("utf-8")

    return charts


# ── Email via Resend ──────────────────────────────────────────────────────────

def build_html_email(snapshot, summary, fetched_at, charts=None):
    date_str = datetime.datetime.fromisoformat(fetched_at).strftime("%B %d, %Y")
    charts = charts or {}

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

    # trend chart images — one per GPU family
    chart_html = ""
    if charts:
        chart_html = """
<h3 style="font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#555;margin:24px 0 12px;">Price trends</h3>
<p style="font-size:11px;color:#888;margin:0 0 16px;">
  Solid line = neocloud avg &nbsp;·&nbsp; shaded band = min/max range &nbsp;·&nbsp;
  dotted = marketplace floor &nbsp;·&nbsp; dashed left = estimated history
</p>
"""
        for gk in ["h100", "h200", "b200", "b300", "a100"]:
            if gk in charts:
                chart_html += (
                    f"<img src='data:image/png;base64,{charts[gk]}' "
                    f"style='width:100%;max-width:620px;display:block;margin:0 0 12px;' "
                    f"alt='{GPU_FAMILIES.get(gk, gk)} price trend'/>\n"
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

{chart_html}

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
    print("\n→ Waiting 60s before summary to avoid rate limit…")
    time.sleep(60)
    print("→ Generating AI pricing power summary…")
    summary = generate_summary(client, snapshot)
    print("✓ Summary generated")

    # ── 5. send email ─────────────────────────────────────────────────────────
    print("\n→ Generating trend charts…")
    charts = generate_charts(history)
    print(f"✓ Generated {len(charts)} charts")

    date_str  = datetime.datetime.fromisoformat(fetched_at).strftime("%b %d, %Y")
    subject   = f"GPU Price Tracker — Week of {date_str}"
    html_body = build_html_email(snapshot, summary, fetched_at, charts)
    print(f"\n→ Sending email to {', '.join(EMAIL_TO_LIST) or '(not configured)'}…")
    send_email(html_body, subject)

    # ── 6. print paste-ready JSON ─────────────────────────────────────────────
    print("\n" + "=" * 52)
    print("PASTE THIS INTO THE DASHBOARD:")
    print("=" * 52)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
