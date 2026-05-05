#!/usr/bin/env python3
"""
Crossover Health Acupuncture Appointment Checker — GitHub Actions / Cloud version

Runs headless, logs in fresh every time, sends notifications via Resend.
Time-window check is handled in-script (7am–10pm PT weekdays, 9am–10pm PT weekends).
"""

import asyncio
import os
import re
import sys
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import resend
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── Config ──────────────────────────────────────────────────────────────────
CROSSOVER_USERNAME = os.environ["CROSSOVER_USERNAME"]
CROSSOVER_PASSWORD = os.environ["CROSSOVER_PASSWORD"]
NOTIFY_EMAIL       = os.environ["NOTIFY_EMAIL"]
NOTIFY_PHONE_SMS   = os.environ["NOTIFY_PHONE"] + "@" + os.environ.get("NOTIFY_PHONE_GATEWAY", "vtext.com")
FROM_EMAIL         = os.environ["NOTIFY_FROM_EMAIL"]

resend.api_key = os.environ["RESEND_API_KEY"]

PORTAL_URL   = "https://care.crossoverhealth.com"
PACIFIC      = ZoneInfo("America/Los_Angeles")
SCREENSHOT_DIR = Path("/tmp/screenshots")

# ─── Logging ─────────────────────────────────────────────────────────────────
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─── Screenshots ─────────────────────────────────────────────────────────────

async def snap(page, name: str):
    try:
        path = SCREENSHOT_DIR / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info(f"Screenshot: {path}")
    except Exception as e:
        log.debug(f"Screenshot '{name}' failed: {e}")


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
    log.info("Navigating to portal...")
    await page.goto(PORTAL_URL, timeout=30000, wait_until="networkidle")
    log.info(f"After navigation: {page.url}")
    await snap(page, "00-initial")

    # Any URL that is not a login/auth page means we're already authenticated.
    # Login pages live at secure.crossoverhealth.com or *.auth0.com.
    login_domains = ("secure.crossoverhealth.com", "auth0.com", "auth.crossoverhealth.com")
    if not any(d in page.url for d in login_domains):
        log.info(f"Already authenticated at: {page.url}")
        return True

    log.info("Login page detected — filling credentials...")
    try:
        await page.locator("#username").wait_for(state="visible", timeout=15000)
        await page.locator("#username").fill(CROSSOVER_USERNAME)
        await page.locator("#password").fill(CROSSOVER_PASSWORD)
        await snap(page, "01-credentials-filled")
        await page.locator("#password").press("Enter")
        log.info("Submitted credentials")
    except PlaywrightTimeout:
        log.error("Login form not found within timeout")
        await snap(page, "01-login-error")
        return False

    try:
        await page.wait_for_url(
            lambda url: "care.crossoverhealth.com" in url and "secure." not in url,
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=15000)
        log.info(f"Logged in successfully: {page.url}")
        await snap(page, "02-post-login")
        return True
    except PlaywrightTimeout:
        log.error(f"Login redirect timed out — current URL: {page.url}")
        await snap(page, "02-login-timeout")
        return False


# ─── Appointment check ───────────────────────────────────────────────────────

# Phrases that indicate a dead-end "no availability" page (case-insensitive).
NO_AVAILABILITY_PHRASES = [
    "no providers available",
    "no providers found",
    "no availability",
    "no appointments available",
    "there are no providers",
    "currently unavailable",
]


def page_has_no_availability(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in NO_AVAILABILITY_PHRASES)


async def find_appointments(page) -> tuple[list, str]:
    """Navigate the booking flow and return (appointments, booking_url)."""
    booking_url = PORTAL_URL

    # Wait for portal to render
    try:
        await page.wait_for_selector('button:has-text("Get Care Now")', timeout=15000)
        log.info("Portal home loaded — 'Get Care Now' visible")
    except PlaywrightTimeout:
        log.warning("'Get Care Now' not found within 15s — dumping page text for diagnosis")
        body = await page.inner_text("body")
        log.info(f"Page text (first 1000 chars):\n{body[:1000]}")
        await snap(page, "03-portal-missing-button")

    await snap(page, "03-portal-home")

    # Get Care Now
    try:
        await page.locator('button:has-text("Get Care Now")').first.click()
        await page.wait_for_load_state("networkidle", timeout=15000)
        log.info(f"Services page: {page.url}")
        await snap(page, "04-services")
    except Exception as e:
        log.error(f"Could not click 'Get Care Now': {e}")
        await snap(page, "04-services-error")
        return [], booking_url

    # Acupuncture service
    try:
        await page.locator('button:has-text("Acupuncture")').first.click()
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(1500)
        log.info(f"After Acupuncture click: {page.url}")
        await snap(page, "05-acupuncture")
    except Exception as e:
        log.error(f"Could not select Acupuncture: {e}")
        await snap(page, "05-acupuncture-error")
        return [], booking_url

    # By Visit
    try:
        await page.locator('button:has-text("By Visit")').first.click()
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(2000)
        log.info(f"Centers page: {page.url}")
        await snap(page, "06-centers")
    except Exception as e:
        log.error(f"Could not click 'By Visit': {e}")
        await snap(page, "06-centers-error")
        return [], booking_url

    # Choose center — iterate until we reach a date/time picker
    reached_date_picker = False
    for center_label in ["Shoreline", "Mountain View", "San Tomas", "Santa Clara"]:
        try:
            el = page.locator(f'button:has-text("{center_label}")').first
            if not await el.is_visible(timeout=2000):
                log.info(f"Center '{center_label}' not visible — skipping")
                continue

            log.info(f"Selecting center: {center_label}")
            await el.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(2000)
            await snap(page, f"07-center-{center_label.lower().replace(' ', '-')}")
            log.info(f"After center click: {page.url}")

            # Check immediately for dead end (no providers at this center)
            body_text = await page.inner_text("body")
            if page_has_no_availability(body_text):
                log.info(f"No providers at {center_label} (dead-end page) — going back")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)
                continue

            # Select visit type
            visit_clicked = False
            for vtype in ["Acupuncture Follow-Up", "Acupuncture Initial", "Acupuncture"]:
                try:
                    v = page.locator(f'button:has-text("{vtype}")').first
                    if await v.is_visible(timeout=3000):
                        log.info(f"Selecting visit type: {vtype}")
                        await v.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await page.wait_for_timeout(3000)
                        await snap(page, f"08-visit-type-{center_label.lower().replace(' ', '-')}")
                        log.info(f"After visit-type click: {page.url}")
                        visit_clicked = True
                        break
                except Exception:
                    continue

            if not visit_clicked:
                log.info(f"No visit type found at {center_label} — going back")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)
                continue

            # Check again for dead end after visit type selection
            body_text = await page.inner_text("body")
            if page_has_no_availability(body_text):
                log.info(f"No providers after visit-type at {center_label} — going back twice")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)
                continue

            reached_date_picker = True
            booking_url = page.url
            break

        except Exception as e:
            log.warning(f"Error navigating center {center_label}: {e}")
            continue

    if not reached_date_picker:
        log.warning("Could not reach date-picker page via any center")
        await snap(page, "09-no-date-picker")
        return [], booking_url

    log.info(f"Reached scheduling page: {booking_url}")
    await snap(page, "09-scheduling-page")

    appointments = await check_for_slots(page)
    return appointments, booking_url


async def check_for_slots(page) -> list:
    """
    Detect available time slots on the scheduling page.

    Primary method: find actual <button> elements whose text is a time (e.g. "9:00 AM").
    These only exist when a slot is truly available and clickable.
    Fallback: regex scan of page text (catches edge-case rendering).
    """
    # Give the SPA time to finish rendering slot data
    await page.wait_for_timeout(3000)

    page_text = await page.inner_text("body")
    log.info(f"Scheduling page text (first 1500 chars):\n{page_text[:1500]}")

    # ── Primary: look for visible, enabled time-slot buttons ──────────────────
    time_re = re.compile(r'^\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*$', re.IGNORECASE)
    slot_times = []
    try:
        buttons = await page.locator("button").all()
        for btn in buttons:
            try:
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text()).strip()
                if time_re.match(txt):
                    enabled = await btn.is_enabled()
                    if enabled:
                        slot_times.append(txt)
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Button scan error: {e}")

    if slot_times:
        log.info(f"Found {len(slot_times)} available slot button(s): {slot_times[:8]}")
    else:
        log.info("No time-slot buttons found via primary scan — trying text fallback")

        # ── Fallback: regex over all page text ────────────────────────────────
        # Use a stricter pattern to avoid matching business-hours text:
        # require time to appear standalone (not "8:00 AM - 5:00 PM" ranges)
        broad_re = re.compile(r'(?<!\d)\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b(?!\s*[-–])', re.IGNORECASE)
        slot_times = broad_re.findall(page_text)

        if slot_times:
            log.info(f"Fallback text scan found times: {slot_times[:8]}")
        else:
            # Log "next available" hint if present
            next_avail = re.search(r'Next available[:\s]+([^\n]+)', page_text, re.IGNORECASE)
            log.info(
                f"No slots found. "
                + (next_avail.group(0).strip() if next_avail else "No next-available info.")
            )
            return []

    # ── Extract date context from page text ───────────────────────────────────
    date_re = re.compile(
        r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\.?\s+\d{1,2}',
        re.IGNORECASE,
    )
    dates = date_re.findall(page_text)

    # ── Identify location ──────────────────────────────────────────────────────
    page_lower = page_text.lower()
    location = "Crossover"
    for loc, keywords in [
        ("Santa Clara", ["san tomas", "santa clara"]),
        ("Mountain View", ["shoreline", "mountain view"]),
    ]:
        if any(k in page_lower for k in keywords):
            location = loc
            break

    return [{
        "location": location,
        "date": dates[0] if dates else "See portal",
        "time": slot_times[0],
    }]


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    now_pt = datetime.now(tz=PACIFIC)
    log.info(f"=== Crossover Appointment Check — {now_pt.strftime('%Y-%m-%d %H:%M %Z')} ===")

    if not within_check_hours():
        log.info("Outside check hours (7am–10pm PT weekdays, 9am–10pm PT weekends) — exiting")
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
            log.error(f"Unexpected error: {e}", exc_info=True)
            await snap(page, "error")
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
