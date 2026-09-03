#!/usr/bin/env python3
"""
Daily Meta (Facebook) Ads report.

Pulls yesterday's performance from one or more ad accounts via the Graph API
Marketing Insights endpoint at three levels of detail:
  - account  -> combined + per-account summary (top of the email)
  - campaign -> per-campaign table (in the email body)
  - ad       -> per-ad table (top performers in the email body, full list
                attached as a CSV so nothing is cut off)

All secrets are read from environment variables (populated from GitHub
Actions repository secrets) — nothing sensitive is hardcoded here.
"""

import csv
import io
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

import requests

GRAPH_API_VERSION = "v21.0"

# Action types that count as a "conversion" for CPA/ROAS purposes.
# Adjust this list if your pixel/CAPI events use different names.
CONVERSION_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
}

# How many top rows (by spend) to show inline in the email body.
TOP_CAMPAIGNS_INLINE = 25
TOP_ADS_INLINE = 15

ACCOUNTS = [
    {
        "label": "SAASO",
        "account_id": os.environ.get("META_ACCOUNT_ID_SAASO", "act_1509303630645987"),
        "token_env": "META_TOKEN_SAASO",
    },
    {
        "label": "Maps Agency",
        "account_id": os.environ.get("META_ACCOUNT_ID_MAPS", "act_1324805233150000"),
        "token_env": "META_TOKEN_MAPS",
    },
]

BASE_FIELDS = "spend,impressions,clicks,cpc,ctr,actions,action_values,purchase_roas,date_start,date_stop"


# Meta error codes that mean "you're being throttled, back off and retry"
# rather than "this request is fundamentally broken."
THROTTLE_CODES = {4, 17, 32, 613}


def _get_with_diagnostics(url: str, params: dict | None) -> dict:
    resp = requests.get(url, params=params, timeout=30)
    if resp.ok:
        return resp.json()

    try:
        err = resp.json().get("error", {})
        code = err.get("code")
        detail = (
            f"Meta API error {resp.status_code}: "
            f"[{err.get('type', '?')} / code {code}"
            f"{'/' + str(err['error_subcode']) if 'error_subcode' in err else ''}] "
            f"{err.get('message', resp.text[:300])}"
        )
    except Exception:
        code = None
        detail = f"Meta API error {resp.status_code}: {resp.text[:300]}"

    exc = RuntimeError(detail)
    exc.throttled = code in THROTTLE_CODES  # type: ignore[attr-defined]
    raise exc


def fetch_insights(account_id: str, token: str, level: str | None = None, name_fields: str = "") -> list:
    """Fetch insight rows for an account. level=None -> single aggregated
    account-level row. level='campaign'/'ad' -> one row per entity, with
    pagination handled so nothing is silently truncated. Retries once with
    backoff on throttling-type errors before giving up."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{account_id}/insights"
    fields = BASE_FIELDS + (f",{name_fields}" if name_fields else "")
    base_params = {
        "fields": fields,
        "date_preset": "yesterday",
        "access_token": token,
        "limit": 500,
    }
    if level:
        base_params["level"] = level

    rows = []
    next_url = url
    params = base_params
    while next_url:
        for attempt in range(3):
            try:
                data = _get_with_diagnostics(next_url, params)
                break
            except RuntimeError as e:
                if getattr(e, "throttled", False) and attempt < 2:
                    time.sleep(15 * (attempt + 1))
                    continue
                raise
        rows.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        params = None  # paging "next" already contains the full query string
    return rows


def extract_conversions(row: dict) -> float:
    total = 0.0
    for a in row.get("actions", []) or []:
        if a.get("action_type") in CONVERSION_ACTION_TYPES:
            try:
                total += float(a.get("value", 0))
            except (TypeError, ValueError):
                pass
    return total


def extract_revenue(row: dict) -> float:
    total = 0.0
    for a in row.get("action_values", []) or []:
        if a.get("action_type") in CONVERSION_ACTION_TYPES:
            try:
                total += float(a.get("value", 0))
            except (TypeError, ValueError):
                pass
    return total


def extract_roas(row: dict, spend: float, revenue: float) -> float:
    for r in row.get("purchase_roas", []) or []:
        try:
            return float(r.get("value", 0))
        except (TypeError, ValueError):
            pass
    return (revenue / spend) if spend else 0.0


def build_metrics(row: dict, name: str = "") -> dict:
    spend = float(row.get("spend", 0) or 0)
    impressions = int(float(row.get("impressions", 0) or 0))
    clicks = int(float(row.get("clicks", 0) or 0))
    cpc = float(row.get("cpc", 0) or 0)
    ctr = float(row.get("ctr", 0) or 0)
    conversions = extract_conversions(row)
    revenue = extract_revenue(row)
    roas = extract_roas(row, spend, revenue)
    cpa = (spend / conversions) if conversions else 0.0

    return {
        "name": name,
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "cpc": cpc,
        "ctr": ctr,
        "conversions": conversions,
        "revenue": revenue,
        "roas": roas,
        "cpa": cpa,
    }


def fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def fmt_num(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def render_summary_block(label: str, m: dict) -> str:
    return (
        f"<h3 style='margin:24px 0 8px'>{label}</h3>"
        f"<table style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Spend</td><td><b>{fmt_money(m['spend'])}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>ROAS</td><td><b>{m['roas']:.2f}x</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Conversions</td><td><b>{fmt_num(m['conversions'])}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>CPA</td><td><b>{fmt_money(m['cpa'])}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>CPC</td><td><b>{fmt_money(m['cpc'])}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Impressions</td><td><b>{fmt_num(m['impressions'])}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>CTR</td><td><b>{m['ctr']:.2f}%</b></td></tr>"
        f"</table>"
    )


def render_detail_table(title: str, rows: list, name_col: str, extra_cols: list = None) -> str:
    """rows: list of build_metrics() dicts, each with a 'name' plus optional
    extra columns already stuffed into the dict under keys named in extra_cols."""
    if not rows:
        return f"<h3 style='margin:24px 0 8px'>{title}</h3><p style='font-family:Arial,sans-serif;color:#777'>No data.</p>"

    extra_cols = extra_cols or []
    headers = [name_col] + [c[1] for c in extra_cols] + ["Spend", "ROAS", "Conversions", "CPA", "CPC", "Impr.", "CTR"]
    head_html = "".join(f"<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #ddd;font-size:12px;color:#555'>{h}</th>" for h in headers)

    body_rows = []
    for r in rows:
        cells = [r["name"]]
        for key, _label in extra_cols:
            cells.append(str(r.get(key, "")))
        cells += [
            fmt_money(r["spend"]),
            f"{r['roas']:.2f}x",
            fmt_num(r["conversions"]),
            fmt_money(r["cpa"]),
            fmt_money(r["cpc"]),
            fmt_num(r["impressions"]),
            f"{r['ctr']:.2f}%",
        ]
        tds = "".join(f"<td style='padding:5px 10px;border-bottom:1px solid #eee;font-size:13px'>{c}</td>" for c in cells)
        body_rows.append(f"<tr>{tds}</tr>")

    return (
        f"<h3 style='margin:24px 0 8px'>{title}</h3>"
        f"<table style='border-collapse:collapse;font-family:Arial,sans-serif;width:100%'>"
        f"<thead><tr>{head_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def rows_to_csv(all_ad_rows: list, fieldnames: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in all_ad_rows:
        writer.writerow(row)
    return buf.getvalue()


def send_email(subject: str, html_body: str, csv_attachment: str = None, csv_filename: str = "ad_level_detail.csv"):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    to_address = os.environ.get("REPORT_TO_ADDRESS", gmail_address)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    if csv_attachment:
        part = MIMEApplication(csv_attachment.encode("utf-8"), Name=csv_filename)
        part["Content-Disposition"] = f'attachment; filename="{csv_filename}"'
        msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


def main():
    account_summaries = {}
    campaign_sections = []
    ad_sections = []
    all_ad_csv_rows = []
    errors = []
    report_date = ""

    for acct in ACCOUNTS:
        label = acct["label"]
        token = os.environ.get(acct["token_env"])
        if not token:
            errors.append(f"Missing secret {acct['token_env']} for {label}")
            continue

        try:
            # --- account level (summary) ---
            acct_rows = fetch_insights(acct["account_id"], token, level=None)
            if acct_rows:
                report_date = acct_rows[0].get("date_start", report_date)
                account_summaries[label] = build_metrics(acct_rows[0], name=label)

            # --- campaign level ---
            time.sleep(2)  # small gap between calls to stay well under rate limits
            camp_rows = fetch_insights(acct["account_id"], token, level="campaign", name_fields="campaign_name")
            campaigns = [build_metrics(r, name=r.get("campaign_name", "(unnamed)")) for r in camp_rows]
            campaigns.sort(key=lambda m: m["spend"], reverse=True)
            campaign_sections.append((label, campaigns))

            # --- ad level ---
            time.sleep(2)
            ad_rows = fetch_insights(
                acct["account_id"], token, level="ad",
                name_fields="ad_name,adset_name,campaign_name",
            )
            ads = []
            for r in ad_rows:
                m = build_metrics(r, name=r.get("ad_name", "(unnamed)"))
                m["campaign"] = r.get("campaign_name", "")
                m["adset"] = r.get("adset_name", "")
                ads.append(m)
            ads.sort(key=lambda m: m["spend"], reverse=True)
            ad_sections.append((label, ads))

            for m in ads:
                all_ad_csv_rows.append({
                    "account": label,
                    "campaign": m["campaign"],
                    "adset": m["adset"],
                    "ad_name": m["name"],
                    "spend": f"{m['spend']:.2f}",
                    "roas": f"{m['roas']:.2f}",
                    "conversions": m["conversions"],
                    "cpa": f"{m['cpa']:.2f}",
                    "cpc": f"{m['cpc']:.2f}",
                    "impressions": m["impressions"],
                    "clicks": m["clicks"],
                    "ctr": f"{m['ctr']:.2f}",
                })

        except Exception as e:
            errors.append(f"{label}: {e}")

    if not account_summaries:
        html = "<p>No data could be fetched today.</p><ul>" + "".join(f"<li>{e}</li>" for e in errors) + "</ul>"
        send_email("Meta Ads Daily Report - ERROR", html)
        sys.exit(1)

    combined = {
        "spend": sum(m["spend"] for m in account_summaries.values()),
        "impressions": sum(m["impressions"] for m in account_summaries.values()),
        "clicks": sum(m["clicks"] for m in account_summaries.values()),
        "conversions": sum(m["conversions"] for m in account_summaries.values()),
        "revenue": sum(m["revenue"] for m in account_summaries.values()),
    }
    combined["cpc"] = (combined["clicks"] and combined["spend"] / combined["clicks"]) or 0.0
    combined["ctr"] = (combined["impressions"] and combined["clicks"] / combined["impressions"] * 100) or 0.0
    combined["cpa"] = (combined["conversions"] and combined["spend"] / combined["conversions"]) or 0.0
    combined["roas"] = (combined["spend"] and combined["revenue"] / combined["spend"]) or 0.0

    html = ["<div style='font-family:Arial,sans-serif'>"]
    if report_date:
        html.append(f"<p style='color:#777;font-size:13px'>Data for {report_date}</p>")

    html.append(render_summary_block("Combined (All Accounts)", combined))
    for label, m in account_summaries.items():
        html.append(render_summary_block(label, m))

    for label, campaigns in campaign_sections:
        html.append(render_detail_table(f"{label} — Campaigns ({len(campaigns)})", campaigns, "Campaign"))

    for label, ads in ad_sections:
        top_ads = ads[:TOP_ADS_INLINE]
        html.append(render_detail_table(
            f"{label} — Top {len(top_ads)} Ads by Spend (of {len(ads)} total — full list in attached CSV)",
            top_ads, "Ad",
            extra_cols=[("campaign", "Campaign"), ("adset", "Ad Set")],
        ))

    if errors:
        html.append("<h3 style='color:#b00'>Notes</h3><ul>" + "".join(f"<li>{e}</li>" for e in errors) + "</ul>")
    html.append("</div>")

    csv_text = rows_to_csv(
        all_ad_csv_rows,
        fieldnames=["account", "campaign", "adset", "ad_name", "spend", "roas", "conversions", "cpa", "cpc", "impressions", "clicks", "ctr"],
    ) if all_ad_csv_rows else None

    subject = f"Meta Ads Daily Report — {report_date}" if report_date else "Meta Ads Daily Report"
    send_email(subject, "".join(html), csv_attachment=csv_text, csv_filename=f"ad_level_detail_{report_date or 'latest'}.csv")


if __name__ == "__main__":
    main()
