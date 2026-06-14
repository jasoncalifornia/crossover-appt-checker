#!/usr/bin/env python3
"""
Crossover appointment checker — Mac/local entry point.

Uses the shared `scraper` module for booking-flow navigation and
date-picker parsing. Handles local-only concerns: saved-session login,
macOS notifications, --debug/--dry-run flags.

Usage:
  python check_appointments.py --login     # first run, save session
  python check_appointments.py --debug     # visible browser
  python check_appointments.py             # headless (scheduler)
  python check_appointments.py --dry-run   # check but don't notify
"""

import asyncio
import os
import sys
import json
import subprocess
import logging
from pathlib import Path
from datetime import datetime, date

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

import scraper

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_FILE     = ROOT / ".playwright-state.json"
SCREENSHOT_DIR = ROOT / "screenshots"
LOG_FILE       = ROOT / "check.log"
CONFIG_FILE    = ROOT / "config.json"
LAST_FINDINGS  = ROOT / "state" / "last-findings.txt"

DEBUG       = "--debug" in sys.argv or "--login" in sys.argv
FORCE_LOGIN = "--login" in sys.argv
DRY_RUN     = "--dry-run" in sys.argv
HEADLESS    = not DEBUG

NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
NOTIFY_PHONE = os.getenv("NOTIFY_PHONE", "")

DEFAULT_CONFIG = {
    "weeks_ahead": 4,
    "notify": {"mac_banner": True, "imessage": True, "email": True, "open_browser": True},
    "dedupe_notifications": True,
    "targets": [{
        "service": "Acupuncture",
        "visit_types": ["Acupuncture Follow-Up"],
        "centers": ["Shoreline"],
        "check_until": None,
    }],
}

SCREENSHOT_DIR.mkdir(exist_ok=True)
LAST_FINDINGS.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("crossover")


def load_config():
    if not CONFIG_FILE.exists():
        log.warning("No config.json — using defaults")
        return DEFAULT_CONFIG
    cfg = json.loads(CONFIG_FILE.read_text())
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["notify"] = {**DEFAULT_CONFIG["notify"], **cfg.get("notify", {})}
    return cfg


def filter_active_targets(targets):
    """Drop targets whose check_until date has passed."""
    today = date.today()
    active = []
    for t in targets:
        cu = t.get("check_until")
        if cu:
            try:
                until = date.fromisoformat(cu)
                if today > until:
                    log.info(f"Skipping target {t.get('service')}: check_until={cu} has passed")
                    continue
            except ValueError:
                log.warning(f"Invalid check_until={cu} on target — ignoring date filter")
        active.append(t)
    return active


# ─── macOS notifications ─────────────────────────────────────────────────────

def notify_mac(title, message):
    subprocess.run(["osascript", "-e",
        f'display notification "{message}" with title "{title}" sound name "Glass"'], check=False)


def notify_imessage(phone, message):
    if not phone:
        return
    for svc in ["iMessage", "SMS"]:
        s = f'''
tell application "Messages"
    set svcRef to 1st service whose service type = {svc}
    set buddy to buddy "{phone}" of svcRef
    send "{message}" to buddy
end tell'''
        if subprocess.run(["osascript", "-e", s], capture_output=True).returncode == 0:
            return


def notify_email(subject, html_body, to=None):
    to = to or NOTIFY_EMAIL
    if not to:
        return
    tmp = "/tmp/crossover_email.html"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_body)
    subj = subject.replace('"', '\\"')
    # Pass html content in make properties — more reliable than setting after creation
    subprocess.run(["osascript", "-e", f'''
tell application "Mail"
    set htmlContent to do shell script "cat /tmp/crossover_email.html"
    set msg to make new outgoing message with properties {{subject:"{subj}", html content:htmlContent, visible:false}}
    tell msg
        make new to recipient at end of to recipients with properties {{address:"{to}"}}
    end tell
    send msg
end tell'''], check=False)


_CENTER_SHORT = {
    "San Tomas/Santa Clara": "Santa Clara",
    "Shoreline/Mountain View": "Mountain View",
    "Sunnyvale/Mathilda": "Sunnyvale",
}

def _short_center(name):
    return _CENTER_SHORT.get(name, name)


def _slot_date_key(s):
    try:
        return datetime.strptime(s.get("date", ""), "%b %d").replace(year=datetime.now().year)
    except ValueError:
        return datetime.max


def _sort_times(times):
    def time_key(t):
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                return datetime.strptime(t.strip(), fmt)
            except ValueError:
                continue
        return datetime.max
    return sorted(times, key=time_key)


def _build_grouped_body(findings):
    """Return email body grouped by (service, center, provider) with dates indented."""
    from collections import defaultdict

    groups = defaultdict(list)
    for f in findings:
        center = _short_center(f["center"])
        for s in f["slots"]:
            key = (f["service"], center, s.get("provider") or "")
            groups[key].append(s)

    # Sort each group's slots by date
    for key in groups:
        groups[key].sort(key=_slot_date_key)

    lines = []
    for key in sorted(groups):
        service, center, provider = key
        header = f"{service} @ {center}"
        if provider:
            header += f" — {provider}"
        lines.append(header)
        for s in groups[key]:
            slot_count = len(s.get("times") or []) or s.get("visits", 0)
            label = s.get("label", s.get("date", "?"))
            if not s.get("times") and label != "Next available":
                continue
            date_line = f"  {label}"
            if s.get("times"):
                date_line += ": " + ", ".join(_sort_times(s["times"]))
            lines.append(date_line)
        lines.append("")

    return "\n".join(lines).rstrip()


_ICON_CALENDAR     = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>'
_ICON_ARROW        = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>'
# Needle: ring handle + long shaft — classic acupuncture icon
_ICON_ACUPUNCTURE  = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="4" r="2"/><line x1="12" y1="6" x2="12" y2="22"/></svg>'
# Eye: open eye + pupil
_ICON_EYE          = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
# Spine vertebrae: stacked rects with connecting lines
_ICON_CHIRO        = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="4" rx="1"/><rect x="9" y="10" width="6" height="4" rx="1"/><rect x="9" y="18" width="6" height="4" rx="1"/><line x1="12" y1="6" x2="12" y2="10"/><line x1="12" y1="14" x2="12" y2="18"/></svg>'
# Stick figure with arms out: physical / PT
_ICON_PT           = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="16"/><path d="M7 10l5 2 5-2"/><line x1="10" y1="16" x2="8" y2="21"/><line x1="14" y1="16" x2="16" y2="21"/></svg>'
# Medical cross in circle: default
_ICON_MEDICAL      = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>'


def _service_icon(service):
    s = service.lower()
    if any(k in s for k in ("acupuncture", "needle", "acu")):
        return _ICON_ACUPUNCTURE
    if any(k in s for k in ("eye", "vision", "optom", "ophthalm")):
        return _ICON_EYE
    if any(k in s for k in ("chiro", "spine", "back")):
        return _ICON_CHIRO
    if any(k in s for k in ("physical", " pt", "therapy", "rehab")):
        return _ICON_PT
    return _ICON_MEDICAL


def _build_html_email(findings):
    from collections import defaultdict

    now = datetime.now()

    groups = defaultdict(list)
    group_url = {}
    for f in findings:
        center = _short_center(f["center"])
        for s in f["slots"]:
            if not s.get("times") and s.get("label") != "Next available":
                continue
            key = (f["service"], center, s.get("provider") or "")
            groups[key].append(s)
            group_url[key] = f.get("booking_url", scraper.PORTAL_URL)

    for key in groups:
        groups[key].sort(key=_slot_date_key)

    total_slots = sum(len(v) for v in groups.values())
    date_str  = now.strftime("%B %-d, %Y")
    time_str  = now.strftime("%-I:%M %p")

    def time_chips(times):
        return "".join(
            f'<span style="display:inline-block;background:#FEE8B0;color:#1c1c1e;'
            f'border-radius:20px;padding:3px 11px;font-size:13px;margin:2px 3px 2px 0;'
            f'font-weight:500;white-space:nowrap">{t}</span>'
            for t in _sort_times(times)
        )

    cards = []
    for key in sorted(groups):
        service, center, provider = key
        slots = groups[key]
        booking_url = group_url.get(key, scraper.PORTAL_URL)
        subtitle = service
        if provider:
            subtitle += f" · {provider}"

        rows = []
        for i, s in enumerate(slots):
            label = s.get("label", s.get("date", "?"))
            times_html = time_chips(s["times"]) if s.get("times") else ""
            border = "" if i == len(slots) - 1 else "border-bottom:1px solid #f2f2f7;"
            rows.append(
                f'<div style="display:flex;align-items:flex-start;padding:9px 0;{border}">'
                f'<div style="width:120px;flex-shrink:0;font-size:14px;font-weight:600;color:#1c1c1e;padding-top:3px">{label}</div>'
                f'<div style="flex:1;line-height:1.8">{times_html}</div>'
                f'</div>'
            )

        cards.append(f'''<div style="background:#fff;border-radius:16px;padding:18px;margin-bottom:12px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
    <div style="width:36px;height:36px;border-radius:10px;background:#FEE8B0;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#1c1c1e">
      {_service_icon(service)}
    </div>
    <div style="flex:1;min-width:0">
      <div style="font-size:17px;font-weight:700;color:#1c1c1e">{center}</div>
      <div style="font-size:14px;color:#636366;margin-top:1px">{subtitle}</div>
    </div>
    <span style="background:#FEE8B0;color:#1c1c1e;font-size:12px;font-weight:700;border-radius:20px;padding:5px 12px;white-space:nowrap;flex-shrink:0">{len(slots)} date{"s" if len(slots) != 1 else ""}</span>
  </div>
  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#636366;margin-bottom:4px">Available Times</div>
  {"".join(rows)}
  <a href="{booking_url}" style="display:flex;align-items:center;justify-content:center;gap:8px;background:#FCB533;color:#1c1c1e;text-decoration:none;text-align:center;padding:13px;border-radius:12px;font-weight:700;margin-top:16px;font-size:15px">
    Book Now {_ICON_ARROW}
  </a>
</div>''')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crossover Health — Appointment Availability</title>
</head>
<body style="font-family:-apple-system,'SF Pro Display',system-ui,sans-serif;background:#f2f2f7;margin:0;padding:16px;color:#1c1c1e;font-size:17px;line-height:1.55;-webkit-text-size-adjust:100%">
<div style="max-width:600px;margin:0 auto">

<div style="background:#FCB533;color:#1c1c1e;border-radius:20px;padding:24px 20px;margin-bottom:12px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
    <div style="opacity:.6">{_ICON_CALENDAR}</div>
    <div style="font-size:26px;font-weight:700;letter-spacing:-.5px">Appointment Availability</div>
  </div>
  <div style="font-size:15px;color:rgba(0,0,0,.55);margin-bottom:14px">Crossover Health · {date_str}</div>
  <div style="display:flex;gap:8px">
    <span style="font-size:13px;background:rgba(0,0,0,.1);border-radius:20px;padding:5px 12px;color:rgba(0,0,0,.65)">{total_slots} slot{"s" if total_slots != 1 else ""} available</span>
  </div>
</div>

{"".join(cards)}

<div style="font-size:12px;color:#8e8e93;text-align:center;padding:8px 0 16px">
  Checked at {time_str} · Availability changes quickly
</div>

</div>
</body>
</html>'''


def fire_notifications(findings, cfg):
    if DRY_RUN:
        log.info("--dry-run set — skipping notifications")
        return
    n = cfg["notify"]
    total = sum(len(f["slots"]) for f in findings)
    summary = ", ".join(
        f"{f['service']} @ {_short_center(f['center'])} ({len(f['slots'])})"
        for f in findings[:5]
    )
    title = f"Crossover: {total} slot(s) found"

    booking_url = findings[0].get("booking_url", scraper.PORTAL_URL)

    log.info(f"NOTIFY: {title}")
    if n.get("mac_banner"):     notify_mac(title, summary)
    if n.get("imessage"):       notify_imessage(NOTIFY_PHONE, f"{title}\n{summary}\n{booking_url}")
    if n.get("email"):          notify_email(title, _build_html_email(findings))
    if n.get("open_browser"):   subprocess.run(["open", booking_url], check=False)


# ─── Local login ─────────────────────────────────────────────────────────────

async def do_automated_login(page):
    u, p = os.getenv("CROSSOVER_USERNAME"), os.getenv("CROSSOVER_PASSWORD")
    if not u or not p:
        log.error("CROSSOVER_USERNAME / CROSSOVER_PASSWORD not in .env")
        return False
    log.info("Logging in...")
    await page.goto(scraper.PORTAL_URL + "/", timeout=30000, wait_until="networkidle")
    try:
        await page.locator('#username').wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout:
        log.error("Username field not visible"); return False
    await page.locator('#username').fill(u)
    await page.locator('#password').fill(p)
    await page.locator('#password').press("Enter")
    try:
        await page.wait_for_url(
            lambda url: "care.crossoverhealth.com" in url and "secure.crossoverhealth.com" not in url,
            timeout=30000,
        )
    except PlaywrightTimeout:
        log.error("Login redirect timed out"); return False
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.context.storage_state(path=str(STATE_FILE))
    log.info("Login OK; session saved")
    return True


async def ensure_session(page):
    await page.goto(scraper.PORTAL_URL + "/", timeout=30000, wait_until="networkidle")
    if "secure.crossoverhealth.com" in page.url or "auth0" in page.url:
        log.warning("Session expired — re-logging in")
        return await do_automated_login(page)
    return True


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 70)
    log.info(f"=== Crossover Check — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    cfg = load_config()
    targets = filter_active_targets(cfg["targets"])
    log.info(f"Config: {len(targets)} active target(s), weeks_ahead={cfg['weeks_ahead']}, dry_run={DRY_RUN}")
    if not targets:
        log.info("No active targets — nothing to do")
        return

    _UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, slow_mo=300 if DEBUG else 0)

        # Step 1: verify/refresh session with a single context
        auth_ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=_UA,
            storage_state=str(STATE_FILE) if STATE_FILE.exists() and not FORCE_LOGIN else None,
        )
        auth_page = await auth_ctx.new_page()

        if FORCE_LOGIN:
            ok = await do_automated_login(auth_page)
            await browser.close()
            sys.exit(0 if ok else 1)

        if not STATE_FILE.exists():
            log.error("No saved session — run --login first")
            await browser.close(); sys.exit(1)

        if not await ensure_session(auth_page):
            log.error("Could not establish session"); await browser.close(); sys.exit(1)

        await auth_ctx.close()

        # Step 2: run all targets in parallel — each gets its own browser context
        async def run_target(t):
            ctx = await browser.new_context(
                viewport={"width": 1400, "height": 1000},
                user_agent=_UA,
                storage_state=str(STATE_FILE),
            )
            try:
                return await scraper.check_target(ctx, t, cfg["weeks_ahead"])
            except Exception as e:
                log.exception(f"Error checking {t}: {e}")
                return []
            finally:
                await ctx.close()

        results = await asyncio.gather(*[run_target(t) for t in targets])
        all_findings = [f for result in results for f in result]
        await browser.close()

    log.info("=" * 70)
    if not all_findings:
        log.info("No appointments found.")
        return

    log.info(f"FOUND availability in {len(all_findings)} (service, center) combos:")
    for f in all_findings:
        for s in f["slots"]:
            log.info(f"  {f['service']} @ {f['center']}: {s.get('label', s.get('date'))} ({s.get('visits')} visit) with {s.get('provider')}")

    # Dedupe: skip notifications if findings identical to last successful run
    if cfg.get("dedupe_notifications", True):
        sig = scraper.signature(all_findings)
        prev = LAST_FINDINGS.read_text() if LAST_FINDINGS.exists() else ""
        if sig == prev:
            log.info("Findings identical to previous run — skipping notifications (dedupe)")
            return
        if not DRY_RUN:
            LAST_FINDINGS.write_text(sig)
        log.info(f"Findings changed — notifying. (previous had {len(prev.splitlines())} lines, now {len(sig.splitlines())})")

    fire_notifications(all_findings, cfg)


if __name__ == "__main__":
    asyncio.run(main())
