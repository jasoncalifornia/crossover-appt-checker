"""
Shared booking-flow scraper for the Crossover patient portal.

Used by both check_appointments.py (Mac/local) and check_cloud.py (GitHub
Actions). Both scripts handle login + notification differently; this
module handles only the booking-flow navigation and the date-picker
parsing.
"""

import re
import logging
from playwright.async_api import TimeoutError as PlaywrightTimeout

log = logging.getLogger("crossover.scraper")

PORTAL_URL = "https://care.crossoverhealth.com"

# ─── Selectors / constants ────────────────────────────────────────────────────

NEXT_AVAIL_PAT = re.compile(r'Next available[:\s]+([^\n]+)', re.IGNORECASE)
TIME_PAT = re.compile(r'\b\d{1,2}:\d{2}\s*(?:AM|PM)\b', re.IGNORECASE)
DAY_ARIA_PAT = re.compile(
    r'^(?P<dow>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+'
    r'(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+'
    r'(?P<visits>\d+|no)\s+visits?\s+with\s+(?P<provider>.+)$',
    re.IGNORECASE,
)
FORWARD_ARIA = "View slots for next week"
BACK_WEEK_ARIA = "View slots for previous week"
PAGE_BACK_ARIA = "Head back to the previous page"


# ─── Generic helpers ──────────────────────────────────────────────────────────

async def click_first_visible(page, selectors, timeout=2500):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=timeout):
                await el.click()
                return True
        except Exception:
            continue
    return False


async def click_aria(page, aria_label, timeout=2000):
    try:
        el = page.locator(f'button[aria-label="{aria_label}"]').first
        if await el.is_visible(timeout=timeout):
            await el.click()
            await page.wait_for_load_state("networkidle", timeout=8000)
            await page.wait_for_timeout(700)
            return True
    except Exception as e:
        log.debug(f"click_aria({aria_label!r}): {e}")
    return False


async def get_day_cards(page):
    """Read all visible date-strip day cards via their aria-labels."""
    raws = await page.evaluate("""
        () => Array.from(document.querySelectorAll('button[aria-label]'))
            .filter(b => !!(b.offsetWidth && b.offsetHeight))
            .map(b => b.getAttribute('aria-label'))
    """)
    days = []
    for aria in raws:
        m = DAY_ARIA_PAT.match(aria.strip())
        if not m:
            continue
        visits_raw = m.group("visits").lower()
        visits = 0 if visits_raw == "no" else int(visits_raw)
        days.append({
            "aria": aria.strip(),
            "dow": m.group("dow"),
            "date": f"{m.group('mon')} {m.group('day')}",
            "label": f"{m.group('dow')} {m.group('mon')} {m.group('day')}",
            "provider": m.group("provider").rstrip("."),
            "visits": visits,
        })
    return days


# ─── Booking-flow navigation ──────────────────────────────────────────────────

async def navigate_to_centers(page, service):
    """Portal home → Get Care Now → service → By Visit. Lands on the centers list."""
    await page.goto(PORTAL_URL + "/", timeout=30000, wait_until="networkidle")
    if not await click_first_visible(page, [
        'button:has-text("Get Care Now")', 'a:has-text("Get Care Now")',
    ]):
        log.warning("Get Care Now button not visible")
        return False
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1200)

    if not await click_first_visible(page, [
        f'button:has-text("{service}")',
        f'[role="button"]:has-text("{service}")',
    ]):
        log.warning(f"Service '{service}' not visible")
        return False
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1200)

    await click_first_visible(page, [
        'button:has-text("By Visit")', 'a:has-text("By Visit")',
    ])
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1200)
    return True


async def select_center(page, center):
    return await click_first_visible(page, [
        f'button:has-text("{center}")',
        f'[role="button"]:has-text("{center}")',
    ], timeout=3000)


async def select_visit_type(page, visit_types):
    if not visit_types:
        visit_types = [""]
    for vt in visit_types:
        sel = f'button:has-text("{vt}")' if vt else 'button'
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3000):
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_timeout(1500)
                return vt or "(first available)"
        except Exception:
            continue
    return ""


# ─── Date-picker scraping ────────────────────────────────────────────────────

async def scrape_date_picker(page, weeks_ahead):
    """Returns {available_days, next_available_text, booking_url, weeks_scanned}."""
    out = {
        "available_days": [],
        "next_available_text": None,
        "booking_url": page.url,
        "weeks_scanned": 0,
    }
    await page.wait_for_timeout(1200)
    body = await page.inner_text("body")
    m = NEXT_AVAIL_PAT.search(body)
    if m:
        out["next_available_text"] = m.group(1).strip()

    for w in range(weeks_ahead + 1):
        await page.wait_for_timeout(400)
        days = await get_day_cards(page)
        avail = [d for d in days if d["visits"] > 0]
        log.info(f"  Week {w}: {len(days)} cards, {len(avail)} with availability")
        for d in avail:
            log.info(f"    ✅ {d['label']}: {d['visits']} visit(s) with {d['provider']}")
            d["week_offset"] = w
            out["available_days"].append(d)
        out["weeks_scanned"] = w + 1
        if w < weeks_ahead:
            if not await click_aria(page, FORWARD_ARIA, timeout=1500):
                log.info("  No forward arrow (end of calendar)")
                break
    return out


# ─── Top-level check_target ──────────────────────────────────────────────────

async def check_target(page, target, weeks_ahead):
    """Returns list of findings: [{service, center, slots, booking_url}]."""
    service = target["service"]
    visit_types = target.get("visit_types", [])
    centers = target.get("centers", [])
    findings = []

    log.info(f"=== Checking: {service} ===")
    if not centers:
        log.warning(f"  No centers configured for {service}")
        return findings

    for center in centers:
        log.info(f"--- {service} @ {center} ---")
        if not await navigate_to_centers(page, service):
            continue
        if not await select_center(page, center):
            log.info(f"  Center '{center}' not visible for {service}")
            continue
        await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(1500)

        chosen = await select_visit_type(page, visit_types)
        if not chosen:
            log.info(f"  No matching visit type for {visit_types}")
            continue

        page_text = await page.inner_text("body")
        if "no providers available" in page_text.lower():
            log.info(f"  No providers available at {center}")
            continue

        booking_url = page.url
        dp = await scrape_date_picker(page, weeks_ahead)
        slots = dp["available_days"]
        if not slots and dp["next_available_text"]:
            slots = [{
                "label": "Next available",
                "date": dp["next_available_text"],
                "visits": 1,
                "provider": "see portal",
                "times": [],
            }]
            log.info(f"  📣 'Next available: {dp['next_available_text']}' treated as found")

        if slots:
            findings.append({
                "service": service,
                "center": center,
                "slots": slots,
                "booking_url": booking_url,
            })
        else:
            log.info(f"  ❌ Confirmed NO appointments at {center} for {service} "
                     f"(weeks scanned: {dp['weeks_scanned']})")

    return findings


def signature(findings):
    """Stable string used to dedupe notifications across runs."""
    parts = []
    for f in sorted(findings, key=lambda x: (x["service"], x["center"])):
        for s in sorted(f["slots"], key=lambda x: (x.get("date",""), x.get("provider",""))):
            parts.append(f"{f['service']}|{f['center']}|{s.get('date','')}"
                         f"|{s.get('visits',0)}|{s.get('provider','')}")
    return "\n".join(parts)
