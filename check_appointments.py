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


def notify_email(subject, body, to=None):
    to = to or NOTIFY_EMAIL
    if not to:
        return
    safe = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    subj = subject.replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'''
tell application "Mail"
    set msg to make new outgoing message with properties {{subject:"{subj}", content:"{safe}", visible:false}}
    tell msg
        make new to recipient at end of to recipients with properties {{address:"{to}"}}
    end tell
    send msg
end tell'''], check=False)


def fire_notifications(findings, cfg):
    if DRY_RUN:
        log.info("--dry-run set — skipping notifications")
        return
    n = cfg["notify"]
    total = sum(len(f["slots"]) for f in findings)
    summary = ", ".join(f"{f['service']} @ {f['center']} ({len(f['slots'])})" for f in findings[:5])
    title = f"Crossover: {total} slot(s) found"

    lines = []
    for f in findings:
        for s in f["slots"]:
            line = f"- {f['service']} | {f['center']} | {s.get('label', s.get('date'))}"
            if s.get("visits"):
                line += f" ({s['visits']} visit{'s' if s['visits'] != 1 else ''})"
            if s.get("provider"):
                line += f" with {s['provider']}"
            lines.append(line)
    booking_url = findings[0].get("booking_url", scraper.PORTAL_URL)
    long_body = f"{title}\n\n" + "\n".join(lines) + f"\n\nBook now: {booking_url}"

    log.info(f"NOTIFY: {title}")
    if n.get("mac_banner"):     notify_mac(title, summary)
    if n.get("imessage"):       notify_imessage(NOTIFY_PHONE, f"{title}\n{summary}\n{booking_url}")
    if n.get("email"):          notify_email(title, long_body)
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, slow_mo=300 if DEBUG else 0)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            storage_state=str(STATE_FILE) if STATE_FILE.exists() and not FORCE_LOGIN else None,
        )
        page = await ctx.new_page()

        if FORCE_LOGIN:
            ok = await do_automated_login(page)
            await browser.close()
            sys.exit(0 if ok else 1)

        if not STATE_FILE.exists():
            log.error("No saved session — run --login first")
            await browser.close(); sys.exit(1)

        if not await ensure_session(page):
            log.error("Could not establish session"); await browser.close(); sys.exit(1)

        all_findings = []
        for t in targets:
            try:
                all_findings.extend(await scraper.check_target(page, t, cfg["weeks_ahead"]))
            except Exception as e:
                log.exception(f"Error checking {t}: {e}")
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
