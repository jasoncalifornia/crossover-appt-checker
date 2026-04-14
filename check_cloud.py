#!/usr/bin/env python3
"""
Crossover Health Acupuncture Appointment Checker — GitHub Actions / Cloud version

Runs headless, logs in fresh every time, sends notifications via Resend.
Time-window check is handled in-script (7am–10pm PT weekdays, 9am–10pm PT weekends).
"""

import asyncio
import os
import sys
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import resend
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── Config ──────────────────────────────────────────────────────────────────
CROSSOVER_USERNAME = os.environ["CROSSOVER_USERNAME"]
CROSSOVER_PASSWORD = os.environ["CROSSOVER_PASSWORD"]
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "jasondcurry@mac.com")
NOTIFY_PHONE_SMS   = "4083736041@vtext.com"   # Verizon SMS gateway
FROM_EMAIL         = "crossover@jasonmeetsalchemy.com"

resend.api_key = os.environ["RESEND_API_KEY"]

PORTAL_URL = "https://care.crossoverhealth.com"
PACIFIC    = ZoneInfo("America/Los_Angeles")

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─── Time-window check ───────────────────────────────────────────────────────

def within_check_hours() -> bool:
    now = datetime.now(tz=PACIFIC)
    hour = now.hour
    is_weekend = now.weekday() >= 5          # Saturday=5, Sunday=6
    start = 9 if is_weekend else 7
    return start <= hour <= 22               # inclusive of 10pm (hour 22)


# ─── Notifications ───────────────────────────────────────────────────────────

def send_notifications(appointments: list, booking_url: str):
    count = len(appointments)
    details = "\n".join(
        f"  - {a['location']}: {a.get('date','?')} {a.get('time','')}"
        for a in appointments
    )
    subject = f"Crossover Acupuncture Available! ({count} slot(s))"
    body = (
        f"Acupuncture appointment(s) available:\n\n{details}\n\n"
        f"Book now: {booking_url}"
    )
    short = f"{count} slot(s) found. Book: {booking_url}"

    log.info("Sending email notification...")
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": NOTIFY_EMAIL,
            "subject": subject,
            "text": body,
        })
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")

    log.info("Sending SMS notification...")
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": NOTIFY_PHONE_SMS,
            "subject": subject,
            "text": short,
        })
        log.info("SMS sent")
    except Exception as e:
        log.error(f"SMS failed: {e}")


# ─── Login ───────────────────────────────────────────────────────────────────

async def login(page) -> bool:
    log.info("Logging in...")
    await page.goto(PORTAL_URL, timeout=30000, wait_until="networkidle")
    # If already on the portal, skip login
    if "secure.crossoverhealth.com" not in page.url:
        log.info(f"Already authenticated: {page.url}")
        return True

    try:
        await page.locator("#username").wait_for(state="visible", timeout=10000)
        await page.locator("#username").fill(CROSSOVER_USERNAME)
        await page.locator("#password").fill(CROSSOVER_PASSWORD)
        await page.locator("#password").press("Enter")
        log.info("Submitted credentials")
    except PlaywrightTimeout:
        log.error("Login form not found")
        return False

    try:
        await page.wait_for_url(
            lambda url: "care.crossoverhealth.com" in url and "secure." not in url,
            timeout=20000,
        )
        log.info(f"Logged in: {page.url}")
        return True
    except PlaywrightTimeout:
        log.error(f"Login redirect timed out — current URL: {page.url}")
        return False


# ─── Appointment check ───────────────────────────────────────────────────────

async def find_appointments(page) -> tuple[list, str]:
    """Navigate the booking flow and return (appointments, booking_url)."""
    booking_url = PORTAL_URL

    # Wait for portal to render
    try:
        await page.wait_for_selector('button:has-text("Get Care Now")', timeout=10000)
    except PlaywrightTimeout:
        log.warning("'Get Care Now' not found — page may not have loaded")

    # Get Care Now
    try:
        await page.locator('button:has-text("Get Care Now")').first.click()
        await page.wait_for_load_state("networkidle", timeout=15000)
        log.info(f"Services page: {page.url}")
    except Exception as e:
        log.error(f"Could not click Get Care Now: {e}")
        return [], booking_url

    # Acupuncture service
    try:
        await page.locator('button:has-text("Acupuncture")').first.click()
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(1000)
    except Exception as e:
        log.error(f"Could not select Acupuncture: {e}")
        return [], booking_url

    # By Visit
    try:
        await page.locator('button:has-text("By Visit")').first.click()
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(1500)
        log.info(f"Centers page: {page.url}")
    except Exception as e:
        log.error(f"Could not click By Visit: {e}")
        return [], booking_url

    # Choose center — try Mountain View/Shoreline first (has the acupuncturist),
    # fall back to others
    reached_date_picker = False
    for center_label in ["Shoreline", "Mountain View", "San Tomas", "Santa Clara"]:
        try:
            el = page.locator(f'button:has-text("{center_label}")').first
            if not await el.is_visible(timeout=2000):
                continue
            log.info(f"Selecting center: {center_label}")
            await el.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(1500)

            # Select visit type
            visit_clicked = False
            for vtype in ["Acupuncture Follow-Up", "Acupuncture Initial", "Acupuncture"]:
                try:
                    v = page.locator(f'button:has-text("{vtype}")').first
                    if await v.is_visible(timeout=3000):
                        log.info(f"Selecting visit type: {vtype}")
                        await v.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await page.wait_for_timeout(2000)
                        visit_clicked = True
                        break
                except Exception:
                    continue

            if not visit_clicked:
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                continue

            # Check for "no providers" dead end
            body_text = await page.inner_text("body")
            if "no providers available" in body_text.lower():
                log.info(f"No providers at {center_label}, trying next")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(500)
                continue

            reached_date_picker = True
            break
        except Exception as e:
            log.debug(f"Center {center_label}: {e}")
            continue

    if not reached_date_picker:
        log.warning("Could not reach date-picker page")
        return [], booking_url

    booking_url = page.url
    log.info(f"Date-picker URL: {booking_url}")

    # Check for available time slots
    appointments = await check_for_slots(page)
    return appointments, booking_url


async def check_for_slots(page) -> list:
    await page.wait_for_timeout(2000)
    page_text = await page.inner_text("body")

    # Time slot buttons look like "9:00 AM", "2:30 PM"
    time_re   = re.compile(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', re.IGNORECASE)
    date_re   = re.compile(
        r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\.?\s+\d{1,2}',
        re.IGNORECASE
    )

    times = time_re.findall(page_text)
    dates = date_re.findall(page_text)

    if not times:
        next_avail = re.search(r'Next available[:\s]+([^\n]+)', page_text, re.IGNORECASE)
        log.info(f"No slots. {next_avail.group(0).strip() if next_avail else 'No next-available info.'}")
        return []

    log.info(f"Slots visible: {times[:6]}")

    # Identify location from page text
    page_lower = page_text.lower()
    location = "Crossover"
    for loc, keywords in [("Santa Clara", ["san tomas", "santa clara"]),
                           ("Mountain View", ["shoreline", "mountain view"])]:
        if any(k in page_lower for k in keywords):
            location = loc
            break

    return [{
        "location": location,
        "date": dates[0] if dates else "See portal",
        "time": times[0],
    }]


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    now_pt = datetime.now(tz=PACIFIC)
    log.info(f"=== Crossover Appointment Check — {now_pt.strftime('%Y-%m-%d %H:%M %Z')} ===")

    if not within_check_hours():
        log.info(f"Outside check hours (7am–10pm PT weekdays, 9am–10pm PT weekends) — exiting")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            ok = await login(page)
            if not ok:
                log.error("Login failed — aborting")
                await browser.close()
                sys.exit(1)

            appointments, booking_url = await find_appointments(page)
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            await browser.close()
            sys.exit(1)

        await browser.close()

    if appointments:
        log.info(f"FOUND {len(appointments)} appointment(s):")
        for a in appointments:
            log.info(f"  {a['location']}: {a['date']} {a.get('time','')}")
        send_notifications(appointments, booking_url)
    else:
        log.info("No acupuncture appointments found")


if __name__ == "__main__":
    asyncio.run(main())
