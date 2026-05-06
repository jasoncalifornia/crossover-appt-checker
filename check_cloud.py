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

# Phrases that indicate a hard dead-end — no acupuncture provider at all.
# Keep this list narrow: broader phrases like "no appointments available" also
# appear on the scheduling page's current-week view and must NOT trigger here.
NO_AVAILABILITY_PHRASES = [
    "no providers available",
    "no providers found",
    "there are no providers",
]

# Markers that confirm we are already on the scheduling page.
# When these are present, dead-end checks must be skipped.
SCHEDULING_PAGE_MARKERS = [
    "choose a provider",
    "provider, date and time",
    "all providers",
    "next available",
]


def page_has_no_availability(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in NO_AVAILABILITY_PHRASES)


def page_is_scheduling(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in SCHEDULING_PAGE_MARKERS)


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

    # Save the centers page URL so we can jump back to it between centers.
    centers_url = page.url

    # ── Discover centers dynamically ──────────────────────────────────────────
    # Read every visible button on the centers page; exclude San Francisco and
    # known non-center UI labels so we catch any location without hardcoding.
    _NON_CENTER_TEXTS = {
        "back", "next", "continue", "cancel", "close", "submit",
        "by visit", "by message", "get care now", "schedule",
        "acupuncture", "acupuncture follow-up", "acupuncture initial",
        "california", "all providers", "all centers",
    }
    _EXCLUDE_LOCATIONS = ["san francisco"]

    discovered_centers: list[str] = []
    try:
        for btn in await page.locator("button").all():
            try:
                txt = (await btn.inner_text()).strip()
                if (
                    len(txt) > 2
                    and txt.lower() not in _NON_CENTER_TEXTS
                    and not any(excl in txt.lower() for excl in _EXCLUDE_LOCATIONS)
                    and await btn.is_visible()
                ):
                    discovered_centers.append(txt)
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Center discovery error: {e}")

    if not discovered_centers:
        log.warning("No centers discovered dynamically — using fallback list")
        discovered_centers = ["Shoreline", "Mountain View", "San Tomas", "Santa Clara"]

    log.info(f"Centers to check ({len(discovered_centers)}): {discovered_centers}")

    # Check EVERY center and collect all available slots — don't stop at the first hit.
    all_appointments: list = []
    checked_any = False

    for center_label in discovered_centers:
        # Always start each iteration from the centers page.
        if page.url != centers_url:
            await page.goto(centers_url, timeout=20000, wait_until="networkidle")
            await page.wait_for_timeout(1500)

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
                log.info(f"No providers at {center_label} (dead-end page) — skipping")
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
                # Some centers skip the visit-type step and land directly on the
                # scheduling page with the visit type pre-selected as a label.
                # Detect this by checking for scheduling-page markers.
                body_text = await page.inner_text("body")
                if page_is_scheduling(body_text):
                    log.info(f"[{center_label}] No visit-type button but already on scheduling page — proceeding")
                    # Skip the dead-end check and go straight to slot scanning.
                    checked_any = True
                    if not booking_url or booking_url == PORTAL_URL:
                        booking_url = page.url
                    await snap(page, f"08-direct-scheduling-{center_label.lower().replace(' ', '-')}")
                    slots = await check_for_slots(page, center_label)
                    if slots:
                        log.info(f"  → {len(slots)} slot(s) at {center_label}")
                        all_appointments.extend(slots)
                    else:
                        log.info(f"  → No slots at {center_label}")
                    continue
                else:
                    log.info(f"No visit type found at {center_label} — skipping")
                    continue

            # After clicking visit type we should be on the scheduling page.
            # Only bail out if we're NOT on the scheduling page AND the page
            # clearly says there are no providers (hard dead-end).
            # Do NOT bail on "no appointments available [this week]" — that
            # phrase can appear on a valid scheduling page with future slots.
            body_text = await page.inner_text("body")
            if not page_is_scheduling(body_text) and page_has_no_availability(body_text):
                log.info(f"No providers after visit-type at {center_label} — skipping")
                continue

            # We're on the scheduling/date-picker page for this center.
            checked_any = True
            if not booking_url or booking_url == PORTAL_URL:
                booking_url = page.url
            await snap(page, f"09-scheduling-{center_label.lower().replace(' ', '-')}")
            log.info(f"Checking slots for {center_label}: {page.url}")

            slots = await check_for_slots(page, center_label)
            if slots:
                log.info(f"  → {len(slots)} slot(s) at {center_label}")
                all_appointments.extend(slots)
            else:
                log.info(f"  → No slots at {center_label}")

        except Exception as e:
            log.warning(f"Error navigating center {center_label}: {e}")
            continue

    if not checked_any:
        log.warning("Could not reach scheduling page via any center")
        await snap(page, "09-no-date-picker")

    return all_appointments, booking_url


def _center_to_location(center_text: str) -> str:
    """Map a center button label (possibly a full name) to a display location."""
    lower = center_text.lower()
    if "san tomas" in lower or "santa clara" in lower:
        return "Santa Clara"
    if "shoreline" in lower or "mountain view" in lower:
        return "Mountain View"
    if "los altos" in lower:
        return "Los Altos"
    if "palo alto" in lower:
        return "Palo Alto"
    if "san jose" in lower:
        return "San Jose"
    if "fremont" in lower:
        return "Fremont"
    return center_text  # fallback: use the raw button text


_TIME_BTN_RE  = re.compile(r'^\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*$', re.IGNORECASE)
_TIME_TEXT_RE = re.compile(r'(?<!\d)\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b(?!\s*[-–])', re.IGNORECASE)
_DATE_RE      = re.compile(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\.?\s+\d{1,2}', re.IGNORECASE)
_NA_RE        = re.compile(r'Next available visit with provider is\s+([^\n.]+)', re.IGNORECASE)
# Only match the short button form "Next available: <date>" — NOT the longer
# "Next available visit…" phrase (handled by _NA_RE above).
_NA_BROAD_RE  = re.compile(r'Next available:\s*([^\n]+)', re.IGNORECASE)

# Calendar next-page selectors tried in order.
_NEXT_PAGE_SELECTORS = [
    '[aria-label="Next week"]',
    '[aria-label="next week"]',
    '[aria-label="Next"]',
    'button[aria-label="next"]',
    'button.fc-next-button',
    'button:has-text("›")',
    'button:has-text(">")',
]

# How many calendar pages (weeks) to check beyond the current view.
_MAX_CAL_PAGES = 12


async def _scan_slots(page) -> tuple[list, str]:
    """
    Scan the current calendar view for available time slots.
    Returns (list_of_time_strings, full_page_text).
    Primary: visible + enabled buttons whose full text is a time.
    Fallback: regex over all page text (excluding range strings like "8:00 AM – 5:00 PM").
    """
    page_text = await page.inner_text("body")
    slots = []
    try:
        for btn in await page.locator("button").all():
            try:
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text()).strip()
                if _TIME_BTN_RE.match(txt) and await btn.is_enabled():
                    slots.append(txt)
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Button scan error: {e}")

    if not slots:
        slots = _TIME_TEXT_RE.findall(page_text)

    return slots, page_text


async def _try_next_page(page) -> bool:
    """Click the calendar's next-page control. Returns True if successful."""
    for sel in _NEXT_PAGE_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


async def check_for_slots(page, center_label: str = "Crossover") -> list:
    """
    Detect available time slots on the scheduling page.

    Checks the current calendar view, then paginates forward up to _MAX_CAL_PAGES
    weeks. Also handles the "Next available visit with provider is <date>" banner
    by trying to jump directly to that date and, if navigation fails, still
    notifying with the date from the banner.
    """
    await page.wait_for_timeout(3000)

    location = _center_to_location(center_label)
    slug = center_label.lower().replace(" ", "-")

    # ── Scan current view ─────────────────────────────────────────────────────
    slot_times, page_text = await _scan_slots(page)
    log.info(f"[{center_label}] Scheduling page text (first 1500):\n{page_text[:1500]}")

    if slot_times:
        log.info(f"[{center_label}] Slots on current view: {slot_times[:8]}")
        dates = _DATE_RE.findall(page_text)
        return [{"location": location, "date": dates[0] if dates else "See portal", "time": slot_times[0]}]

    # ── Check for "next available" banner ─────────────────────────────────────
    na_match = _NA_RE.search(page_text) or _NA_BROAD_RE.search(page_text)
    if na_match:
        avail_date_str = na_match.group(1).strip().rstrip(".")
        log.info(f"[{center_label}] Next-available banner: '{na_match.group(0).strip()}'")

        # Try jumping directly to the advertised date before falling back to
        # one-page-at-a-time pagination.
        for sel in [
            f'button:has-text("{avail_date_str}")',
            f'a:has-text("{avail_date_str}")',
            'button:has-text("Next available")',
            'a:has-text("Next available")',
        ] + _NEXT_PAGE_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    log.info(f"[{center_label}] Clicking '{sel}' to reach next-available date")
                    await el.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await page.wait_for_timeout(2000)
                    await snap(page, f"10-na-jump-{slug}")
                    slot_times, page_text = await _scan_slots(page)
                    if slot_times:
                        log.info(f"[{center_label}] Slots after jump: {slot_times[:8]}")
                        dates = _DATE_RE.findall(page_text)
                        return [{"location": location, "date": dates[0] if dates else avail_date_str, "time": slot_times[0]}]
                    break
            except Exception:
                continue

        # Navigation didn't yield times — but the banner is itself confirmation
        # that an appointment exists. Notify with the date from the banner.
        log.info(f"[{center_label}] Reporting next-available date from banner: {avail_date_str}")
        return [{"location": location, "date": avail_date_str, "time": "See portal"}]

    # ── Paginate forward through the calendar ─────────────────────────────────
    log.info(f"[{center_label}] No slots or banner on current view — paginating calendar")
    for page_num in range(1, _MAX_CAL_PAGES + 1):
        clicked = await _try_next_page(page)
        if not clicked:
            log.info(f"[{center_label}] No calendar next-button found — stopping pagination")
            break

        await snap(page, f"10-cal-p{page_num}-{slug}")
        slot_times, page_text = await _scan_slots(page)
        log.info(f"[{center_label}] Cal page {page_num} text (first 600):\n{page_text[:600]}")

        if slot_times:
            log.info(f"[{center_label}] Slots on cal page {page_num}: {slot_times[:8]}")
            dates = _DATE_RE.findall(page_text)
            return [{"location": location, "date": dates[0] if dates else "See portal", "time": slot_times[0]}]

        # Also honour a next-available banner that appears mid-pagination.
        na_match = _NA_RE.search(page_text) or _NA_BROAD_RE.search(page_text)
        if na_match:
            avail_date_str = na_match.group(1).strip().rstrip(".")
            log.info(f"[{center_label}] Next-available banner on cal page {page_num}: {avail_date_str}")
            return [{"location": location, "date": avail_date_str, "time": "See portal"}]

    log.info(f"[{center_label}] No slots found within {_MAX_CAL_PAGES} calendar pages")
    return []


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
