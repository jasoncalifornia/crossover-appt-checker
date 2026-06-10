#!/usr/bin/env python3
"""
Crossover Health appointment checker — GitHub Actions / cloud version.

Headless, logs in fresh every run, uses shared scraper logic, loads
config.json, supports check_until per-target, and dedupes notifications
across runs by persisting state/last-findings.txt (the workflow commits
this file back to the repo so the next run sees it).
"""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import resend
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

import scraper

ROOT = Path(__file__).parent
CONFIG_FILE   = ROOT / "config.json"
LAST_FINDINGS = ROOT / "state" / "last-findings.txt"
LAST_FINDINGS.parent.mkdir(exist_ok=True)
SCREENSHOT_DIR = Path("/tmp/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

CROSSOVER_USERNAME = os.environ["CROSSOVER_USERNAME"]
CROSSOVER_PASSWORD = os.environ["CROSSOVER_PASSWORD"]
NOTIFY_EMAIL       = os.environ["NOTIFY_EMAIL"]
NOTIFY_PHONE_SMS   = os.environ["NOTIFY_PHONE"] + "@" + os.environ.get("NOTIFY_PHONE_GATEWAY", "vtext.com")
FROM_EMAIL         = os.environ["NOTIFY_FROM_EMAIL"]
resend.api_key     = os.environ["RESEND_API_KEY"]

PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_CONFIG = {
    "weeks_ahead": 4,
    "dedupe_notifications": True,
    "check_window": {"weekday_start": 7, "weekend_start": 9, "end_hour": 22},
    "targets": [{
        "service": "Acupuncture",
        "visit_types": ["Acupuncture Follow-Up", "Acupuncture Initial"],
        "centers": ["Shoreline", "Mountain View", "Santa Clara", "San Tomas"],
        "check_until": None,
    }],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("crossover.cloud")


def load_config():
    if not CONFIG_FILE.exists():
        log.warning("No config.json — using defaults")
        return DEFAULT_CONFIG
    cfg = json.loads(CONFIG_FILE.read_text())
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["check_window"] = {**DEFAULT_CONFIG["check_window"], **cfg.get("check_window", {})}
    return cfg


def filter_active_targets(targets):
    today = date.today()
    active = []
    for t in targets:
        cu = t.get("check_until")
        if cu:
            try:
                if today > date.fromisoformat(cu):
                    log.info(f"Skipping target {t.get('service')}@{t.get('centers')}: check_until={cu} passed")
                    continue
            except ValueError:
                log.warning(f"Invalid check_until={cu}")
        active.append(t)
    return active


def within_check_hours(window):
    now = datetime.now(tz=PACIFIC)
    is_weekend = now.weekday() >= 5
    start = window.get("weekend_start", 9) if is_weekend else window.get("weekday_start", 7)
    return start <= now.hour <= window.get("end_hour", 22)


async def snap(page, name):
    try:
        path = SCREENSHOT_DIR / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info(f"Screenshot: {path}")
    except Exception as e:
        log.debug(f"snap '{name}' failed: {e}")


def build_messages(findings):
    total = sum(len(f["slots"]) for f in findings)
    subject = f"Crossover: {total} slot(s) found"
    short_lines, long_lines = [], [subject, ""]

    def slot_date_key(item):
        _, s = item
        try:
            return datetime.strptime(s.get("date", ""), "%b %d").replace(year=datetime.now().year)
        except ValueError:
            return datetime.max

    all_slots = sorted([(f, s) for f in findings for s in f["slots"]], key=slot_date_key)

    for f, s in all_slots:
        base = f"{f['service']} @ {f['center']} — {s.get('label', s.get('date', '?'))}"
        # Prefer the number of actual time slots we captured; the day-card
        # "N visit" count is approximate.
        slot_count = len(s.get("times") or []) or s.get("visits", 0)
        if slot_count:
            base += f" ({slot_count} slot{'s' if slot_count != 1 else ''})"
        if s.get("provider"):
            base += f" with {s['provider']}"
        if s.get("times"):
            base += " — " + ", ".join(s["times"])
        short_lines.append(base)
        long_lines.append("• " + base)
    booking_url = findings[0].get("booking_url", scraper.PORTAL_URL)
    long_lines += ["", f"Book now: {booking_url}"]
    return subject, "\n".join(short_lines), "\n".join(long_lines), booking_url


def send_notifications(findings):
    subject, short, body, booking_url = build_messages(findings)
    log.info("Sending email...")
    try:
        resend.Emails.send({"from": FROM_EMAIL, "to": NOTIFY_EMAIL, "subject": subject, "text": body})
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")
    log.info("Sending SMS...")
    try:
        sms = f"{short}\n\n{booking_url}"[:480]
        resend.Emails.send({"from": FROM_EMAIL, "to": NOTIFY_PHONE_SMS, "subject": subject, "text": sms})
        log.info("SMS sent")
    except Exception as e:
        log.error(f"SMS failed: {e}")


async def login(page):
    login_domains = ("secure.crossoverhealth.com", "auth0.com", "auth.crossoverhealth.com")
    for attempt in range(3):
        log.info(f"Login attempt {attempt + 1}/3...")
        try:
            await page.goto(scraper.PORTAL_URL, timeout=45000, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            log.warning(f"Portal load timed out (attempt {attempt + 1}/3); retrying...")
            continue
        await snap(page, f"0{attempt}-initial")
        if not any(d in page.url for d in login_domains):
            log.info(f"Already authenticated at: {page.url}")
            return True
        try:
            await page.locator("#username").wait_for(state="visible", timeout=15000)
            await page.locator("#username").fill(CROSSOVER_USERNAME)
            await page.locator("#password").fill(CROSSOVER_PASSWORD)
            await snap(page, f"0{attempt}-credentials")
            await page.locator("#password").press("Enter")
        except PlaywrightTimeout:
            log.warning(f"Login form not found (attempt {attempt + 1}/3); retrying...")
            continue
        try:
            await page.wait_for_url(
                lambda u: "care.crossoverhealth.com" in u and "secure." not in u,
                timeout=30000,
            )
            log.info(f"Logged in: {page.url}")
            return True
        except PlaywrightTimeout:
            log.warning(f"Login redirect timed out at {page.url} (attempt {attempt + 1}/3); retrying...")
            await snap(page, f"0{attempt}-login-timeout")
    log.error("Login failed after 3 attempts")
    await snap(page, "login-failed")
    return False


async def main():
    cfg = load_config()
    now_pt = datetime.now(tz=PACIFIC)
    log.info(f"=== Crossover Cloud Check — {now_pt.strftime('%Y-%m-%d %H:%M %Z')} ===")

    if not within_check_hours(cfg["check_window"]):
        log.info("Outside check hours — exiting")
        return

    targets = filter_active_targets(cfg["targets"])
    if not targets:
        log.info("No active targets — exiting")
        return
    log.info(f"Config: {len(targets)} target(s), weeks_ahead={cfg['weeks_ahead']}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        )
        page = await ctx.new_page()

        if not await login(page):
            log.error("Login failed")
            await browser.close()
            sys.exit(1)

        all_findings = []
        for t in targets:
            try:
                all_findings.extend(await scraper.check_target(page, t, cfg["weeks_ahead"]))
            except Exception as e:
                log.exception(f"Error checking {t}: {e}")
                await snap(page, f"error-{t.get('service','x')}")
        await browser.close()

    if not all_findings:
        log.info("No appointments found across configured targets.")
        return

    log.info(f"FOUND availability in {len(all_findings)} (service, center) combos:")
    for f in all_findings:
        for s in f["slots"]:
            log.info(f"  {f['service']} @ {f['center']}: {s.get('label', s.get('date'))} "
                     f"({s.get('visits')} visit) with {s.get('provider')}")

    sig = scraper.signature(all_findings)
    prev = LAST_FINDINGS.read_text() if LAST_FINDINGS.exists() else ""
    if cfg.get("dedupe_notifications", True) and sig == prev:
        log.info("Findings identical to previous run — skipping notification (dedupe)")
        return
    LAST_FINDINGS.write_text(sig)
    log.info(f"State updated → {LAST_FINDINGS.relative_to(ROOT)} ({len(sig)} chars)")
    send_notifications(all_findings)


if __name__ == "__main__":
    asyncio.run(main())
