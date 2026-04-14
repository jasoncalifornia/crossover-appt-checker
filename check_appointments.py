#!/usr/bin/env python3
"""
Crossover Health Acupuncture Appointment Checker

Usage:
  python check_appointments.py --login   # First run: log in manually, save session
  python check_appointments.py --debug   # Visible browser + saved session (for debugging selectors)
  python check_appointments.py           # Headless check (used by scheduler)
"""

import asyncio
import os
import sys
import json
import subprocess
import logging
import re
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── Config ──────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL", "")
NOTIFY_PHONE   = os.getenv("NOTIFY_PHONE", "")

STATE_FILE     = Path(__file__).parent / ".playwright-state.json"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
LOG_FILE       = Path(__file__).parent / "check.log"

DEBUG          = "--debug" in sys.argv or "--login" in sys.argv
FORCE_LOGIN    = "--login" in sys.argv
HEADLESS       = not DEBUG

LOCATION_PRIORITY = ["santa clara", "mountain view"]
TARGET_SERVICE    = "acupuncture"

PORTAL_URL = "https://care.crossoverhealth.com"

# ─── Logging ─────────────────────────────────────────────────────────────────
SCREENSHOT_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─── Notifications ────────────────────────────────────────────────────────────

def notify_mac(title: str, message: str):
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


def notify_imessage(phone: str, message: str):
    """Send via iMessage; fall back to SMS service."""
    for service_type in ["iMessage", "SMS"]:
        script = f'''
tell application "Messages"
    set svc to 1st service whose service type = {service_type}
    set buddy to buddy "{phone}" of svc
    send "{message}" to buddy
end tell
'''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode == 0:
            break


def notify_email(subject: str, body: str, to: str = NOTIFY_EMAIL):
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    safe_subject = subject.replace('"', '\\"')
    script = f'''
tell application "Mail"
    set msg to make new outgoing message with properties {{subject:"{safe_subject}", content:"{safe_body}", visible:false}}
    tell msg
        make new to recipient at end of to recipients with properties {{address:"{to}"}}
    end tell
    send msg
end tell
'''
    subprocess.run(["osascript", "-e", script], check=False)


def open_browser_url(url: str):
    subprocess.run(["open", url], check=False)


def send_all_notifications(appointments: list, booking_url: str):
    count = len(appointments)
    summary = ", ".join(a["location"] for a in appointments[:3])
    title = "Crossover Acupuncture Available!"
    short_msg = f"{count} slot(s) found: {summary}"
    details = "\n".join(
        f"- {a['location']}: {a.get('date', '?')} {a.get('time', '')}"
        for a in appointments
    )
    long_msg = f"Acupuncture appointments available!\n\n{details}\n\nBook now: {booking_url}"

    log.info("APPOINTMENT FOUND — firing all notifications")
    notify_mac(title, short_msg)
    notify_imessage(NOTIFY_PHONE, f"{title}\n{short_msg}\n{booking_url}")
    notify_email(title, long_msg)
    open_browser_url(booking_url)


# ─── Screenshots (async) ──────────────────────────────────────────────────────

async def snap(page, name: str):
    try:
        path = SCREENSHOT_DIR / f"{name}.png"
        await page.screenshot(path=str(path))
        log.info(f"Screenshot saved: {path.name}")
    except Exception as e:
        log.debug(f"Screenshot '{name}' failed: {e}")


# ─── Manual login (--login mode) ─────────────────────────────────────────────

async def do_automated_login(page):
    """
    Automated login via care.crossoverhealth.com → Auth0 form → portal.
    """
    username = os.getenv("CROSSOVER_USERNAME")
    password = os.getenv("CROSSOVER_PASSWORD")
    if not username or not password:
        log.error("CROSSOVER_USERNAME / CROSSOVER_PASSWORD not set in .env")
        return False

    log.info("Navigating to care portal (will redirect to login form)...")
    await page.goto("https://care.crossoverhealth.com/", timeout=30000, wait_until="networkidle")
    log.info(f"Redirected to: {page.url}")
    await snap(page, "login-01-form")

    # Fill email
    try:
        await page.locator('#username').wait_for(state="visible", timeout=10000)
        await page.locator('#username').fill(username)
        log.info("Filled username")
    except PlaywrightTimeout:
        log.error("Username field not found")
        await snap(page, "login-error")
        return False

    # Fill password
    await page.locator('#password').fill(password)
    log.info("Filled password")
    await snap(page, "login-02-credentials")

    # Submit by pressing Enter (the submit button is obscured by the input overlay)
    await page.locator('#password').press("Enter")
    log.info("Submitted login form")

    # Wait for the portal (either subdomain)
    try:
        await page.wait_for_url(
            lambda url: ("app.crossoverhealth.com" in url or "care.crossoverhealth.com" in url)
                        and "secure.crossoverhealth.com" not in url,
            timeout=30000,
        )
    except PlaywrightTimeout:
        await snap(page, "login-timeout")
        log.error("Did not reach portal after login — check screenshots")
        return False

    await page.wait_for_load_state("networkidle", timeout=15000)
    await snap(page, "post-login")
    log.info(f"Logged in successfully. URL: {page.url}")
    await page.context.storage_state(path=str(STATE_FILE))
    log.info(f"Session saved to {STATE_FILE}")
    return True


# ─── Appointment checking ─────────────────────────────────────────────────────

async def find_appointments(page) -> tuple:
    """
    Navigate to scheduling, look for acupuncture at target locations.
    Returns (list_of_appointments, booking_url).
    """
    # Go to portal home first to ensure we're authenticated
    await page.goto(PORTAL_URL, timeout=30000, wait_until="networkidle")
    await snap(page, "01-portal-home")

    current_url = page.url
    log.info(f"Portal URL after nav: {current_url}")

    # Detect session expiry — if redirected to login or auth0
    if "secure.crossoverhealth.com" in current_url or "auth0" in current_url or \
       ("care.crossoverhealth.com" not in current_url and "app.crossoverhealth.com" not in current_url):
        log.warning("Session expired — attempting auto re-login...")
        success = await do_automated_login(page)
        if not success:
            log.error("Auto re-login failed")
            return [], current_url
        # Reload portal after re-login
        await page.goto(PORTAL_URL, timeout=30000, wait_until="networkidle")
        current_url = page.url
        log.info(f"Portal URL after re-login: {current_url}")

    # Wait for portal content to fully render
    try:
        await page.wait_for_selector('button:has-text("Get Care Now")', timeout=10000)
    except Exception:
        pass

    # Click "Get Care Now" to enter the booking flow
    nav_clicked = False
    for sel in [
        'button:has-text("Get Care Now")',
        'button:has-text("Book Care Now")',
        'a:has-text("Get Care Now")',
        'a:has-text("Schedule")',
        'button:has-text("Schedule")',
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                log.info(f"Clicking: '{sel}'")
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_timeout(2000)
                nav_clicked = True
                break
        except Exception:
            continue

    if not nav_clicked:
        log.warning("No booking button found — reading current page for slots")

    await snap(page, "02-schedule-page")
    booking_url = page.url
    log.info(f"Schedule page URL: {booking_url}")

    # Step: select Acupuncture service
    for sel in ['button:has-text("Acupuncture")', '[role="button"]:has-text("Acupuncture")', '*:text-is("Acupuncture")']:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                log.info(f"Clicking acupuncture option: {sel}")
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1500)
                await snap(page, "03-acupuncture-selected")
                break
        except Exception:
            continue

    # Step: choose "By Visit" (not By Message)
    for sel in ['button:has-text("By Visit")', 'a:has-text("By Visit")', '[role="button"]:has-text("By Visit")']:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3000):
                log.info("Clicking 'By Visit'")
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(2000)
                await snap(page, "04-by-visit")
                break
        except Exception:
            continue

    # We're on "Choose a center" — try each center until we reach the date-picker
    booking_url = page.url
    reached_date_picker = False

    for center_label in ["Shoreline", "Mountain View", "San Tomas", "Santa Clara"]:
        try:
            el = page.locator(f'button:has-text("{center_label}")').first
            if not await el.is_visible(timeout=2000):
                continue
            log.info(f"Entering scheduling via center: {center_label}")
            await el.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(1500)

            await snap(page, "05-visit-type")
            log.info(f"Visit type page URL: {page.url}")

            # Select visit type
            visit_clicked = False
            for visit_label in ["Acupuncture Follow-Up", "Acupuncture Initial", "Acupuncture"]:
                try:
                    v = page.locator(f'button:has-text("{visit_label}")').first
                    if await v.is_visible(timeout=3000):
                        log.info(f"Selecting visit type: {visit_label}")
                        await v.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await page.wait_for_timeout(2000)
                        visit_clicked = True
                        break
                except Exception:
                    continue

            if not visit_clicked:
                log.info(f"No visit type found at {center_label}, trying next center")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)
                continue

            # Check if we reached the date-picker (not a "no providers" page)
            page_text = await page.inner_text("body")
            if "no providers available" in page_text.lower():
                log.info(f"No providers at {center_label} — trying next center")
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)
                continue

            reached_date_picker = True
            break
        except Exception as e:
            log.debug(f"Center {center_label} error: {e}")
            continue

    if not reached_date_picker:
        log.warning("Could not reach scheduling page via any center")
        return [], booking_url

    await snap(page, "06-scheduling-page")
    booking_url = page.url
    log.info(f"Scheduling page URL: {booking_url}")

    # Switch to "All Centers" dropdown to see all providers
    try:
        # The dropdown button shows the current center name
        dropdown = page.locator('button[aria-haspopup="listbox"], button[role="combobox"], button:has-text("Crossover")').first
        if await dropdown.is_visible(timeout=3000):
            await dropdown.click()
            await page.wait_for_timeout(1000)
            opt = page.locator('li:has-text("All Centers"), [role="option"]:has-text("All Centers")').first
            if await opt.is_visible(timeout=3000):
                await opt.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(2000)
                log.info("Switched to All Centers")
                await snap(page, "07-all-centers")
    except Exception as e:
        log.debug(f"All Centers switch: {e}")

    appointments = await check_scheduling_page(page)
    return appointments, booking_url


async def check_scheduling_page(page) -> list:
    """
    On the 'Choose a provider, date and time' page:
    - If time-slot buttons (e.g. '9:00 AM') are visible → appointments available NOW
    - Otherwise no availability
    """
    await page.wait_for_timeout(2000)

    page_text = await page.inner_text("body")
    if DEBUG:
        log.info(f"Scheduling page text:\n{page_text[:800]}")

    # Look for clickable time buttons — these only appear when slots are open
    time_re = re.compile(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', re.IGNORECASE)
    times_found = time_re.findall(page_text)

    if not times_found:
        # Check if the page shows a "Next available" hint (for logging only)
        next_avail = re.search(r'Next available[:\s]+([^\n]+)', page_text, re.IGNORECASE)
        if next_avail:
            log.info(f"No slots now. {next_avail.group(0).strip()}")
        else:
            log.info("No time slots visible on scheduling page")
        return []

    log.info(f"Time slots visible: {times_found[:8]}")

    # Extract location + date context for each slot
    appointments = []
    date_re = re.compile(
        r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\s+\d{1,2}',
        re.IGNORECASE
    )
    dates_found = date_re.findall(page_text)

    # Group slots by location (Santa Clara / Mountain View priority)
    for loc in LOCATION_PRIORITY:
        if loc.lower() in page_text.lower():
            appointments.append({
                "location": loc.title(),
                "date": dates_found[0] if dates_found else "See portal",
                "time": times_found[0],
            })
            log.info(f"Slot at {loc.title()}: {dates_found[0] if dates_found else '?'} {times_found[0]}")
            break

    if not appointments and times_found:
        # Slots found but location unclear — still notify
        appointments.append({
            "location": "Crossover",
            "date": dates_found[0] if dates_found else "See portal",
            "time": times_found[0],
        })

    return appointments


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"=== Crossover Appointment Check — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=300 if DEBUG else 0,
        )

        context_kwargs = dict(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # ── Login mode ──
        if FORCE_LOGIN:
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            success = await do_automated_login(page)
            await browser.close()
            if success:
                print("\nSession saved. Now run ./run.sh or ./enable.sh to start scheduling.")
            sys.exit(0 if success else 1)

        # ── Normal / debug mode: use saved session ──
        if not STATE_FILE.exists():
            log.error("No saved session found. Run first: python check_appointments.py --login")
            sys.exit(1)

        log.info("Loading saved session...")
        context = await browser.new_context(storage_state=str(STATE_FILE), **context_kwargs)
        page = await context.new_page()

        try:
            appointments, booking_url = await find_appointments(page)
        except Exception as e:
            log.error(f"Error during appointment check: {e}")
            await snap(page, "error")
            await browser.close()
            sys.exit(1)

        await browser.close()

    if appointments:
        log.info(f"FOUND {len(appointments)} appointment(s):")
        for a in appointments:
            log.info(f"  {a['location']}: {a['date']} {a.get('time', '')}")
        send_all_notifications(appointments, booking_url)
    else:
        log.info("No acupuncture appointments found at target locations")


if __name__ == "__main__":
    asyncio.run(main())
