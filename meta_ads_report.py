#!/usr/bin/env python3
"""
Daily Meta (Facebook) Ads report.

Pulls yesterday's performance from one or more ad accounts via the Graph API
Marketing Insights endpoint, builds a combined summary + per-account
breakdown, and emails it via Gmail SMTP.

All secrets are read from environment variables (populated from GitHub
Actions repository secrets) — nothing sensitive is hardcoded here.
"""

import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

GRAPH_API_VERSION = "v21.0"

# Action types that count as a "conversion" for CPA/ROAS purposes.
# Adjust this list if your pixel/CAPI events use different names.
CONVERSION_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
}

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


def fetch_insights(account_id: str, token: str) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{account_id}/insights"
    params = {
        "fields": "spend,impressions,clicks,cpc,ctr,actions,action_values,purchase_roas",
        "date_preset": "yesterday",
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0] if data else {}


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


def build_account_metrics(row: dict) -> dict:
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


def render_account_block(label: str, m: dict) -> str:
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


def send_email(subject: str, html_body: str):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    to_address = os.environ.get("REPORT_TO_ADDRESS", gmail_address)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


def main():
    results = {}
    errors = []

    for acct in ACCOUNTS:
        token = os.environ.get(acct["token_env"])
        if not token:
            errors.append(f"Missing secret {acct['token_env']} for {acct['label']}")
            continue
        try:
            row = fetch_insights(acct["account_id"], token)
            results[acct["label"]] = build_account_metrics(row)
        except Exception as e:
            errors.append(f"{acct['label']}: {e}")

    if not results:
        # Still email so the failure doesn't go unnoticed.
        html = "<p>No data could be fetched today.</p><ul>" + "".join(f"<li>{e}</li>" for e in errors) + "</ul>"
        send_email("Meta Ads Daily Report - ERROR", html)
        sys.exit(1)

    combined = {
        "spend": sum(m["spend"] for m in results.values()),
        "impressions": sum(m["impressions"] for m in results.values()),
        "clicks": sum(m["clicks"] for m in results.values()),
        "conversions": sum(m["conversions"] for m in results.values()),
        "revenue": sum(m["revenue"] for m in results.values()),
    }
    combined["cpc"] = (combined["clicks"] and combined["spend"] / combined["clicks"]) or 0.0
    combined["ctr"] = (combined["impressions"] and combined["clicks"] / combined["impressions"] * 100) or 0.0
    combined["cpa"] = (combined["conversions"] and combined["spend"] / combined["conversions"]) or 0.0
    combined["roas"] = (combined["spend"] and combined["revenue"] / combined["spend"]) or 0.0

    html = ["<div style='font-family:Arial,sans-serif'>"]
    html.append(render_account_block("Combined (All Accounts)", combined))
    for label, m in results.items():
        html.append(render_account_block(label, m))
    if errors:
        html.append("<h3 style='color:#b00'>Notes</h3><ul>" + "".join(f"<li>{e}</li>" for e in errors) + "</ul>")
    html.append("</div>")

    send_email("Meta Ads Daily Report", "".join(html))


if __name__ == "__main__":
    main()
