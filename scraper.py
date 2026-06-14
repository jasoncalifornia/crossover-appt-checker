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

# Some center labels on the portal are aliases for the same physical clinic.
# When the portal exposes both, we want to surface findings only once.
CENTER_ALIASES = {
    "shoreline": "Shoreline/Mountain View",
    "mountain view": "Shoreline/Mountain View",
    "san tomas": "San Tomas/Santa Clara",
    "santa clara": "San Tomas/Santa Clara",
    "sunnyvale": "Sunnyvale/Mathilda",
    "mathilda": "Sunnyvale/Mathilda",
}


def canonical_center(name: str) -> str:
    return CENTER_ALIASES.get(name.strip().lower(), name)

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
    # domcontentloaded (not networkidle) — portal keeps long-lived XHR/sockets
    # open, so networkidle frequently exceeds 30s. We must then wait
    # explicitly for the React app to render the "Get Care Now" button.
    await page.goto(PORTAL_URL + "/", timeout=45000, wait_until="domcontentloaded")
    try:
        await page.locator('button:has-text("Get Care Now"), a:has-text("Get Care Now")').first.wait_for(
            state="visible", timeout=30000,
        )
    except Exception:
        log.warning("Get Care Now button never rendered within 30s")
        return False
    if not await click_first_visible(page, [
        'button:has-text("Get Care Now")', 'a:has-text("Get Care Now")',
    ]):
        log.warning("Get Care Now button visible but not clickable")
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

async def _nav_to_date_picker(page, service, center, visit_types):
    """Re-walk Get Care Now → service → By Visit → center → visit type. Returns True on date-picker."""
    if not await navigate_to_centers(page, service):
        return False
    if not await select_center(page, center):
        return False
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1200)
    if not await select_visit_type(page, visit_types):
        return False
    return "date-picker" in page.url


async def _fetch_times_for_day(page, day, service, center, visit_types):
    """Re-navigate to the date picker, advance to day.week_offset, click the day card, scrape times."""
    for attempt in range(2):
        if not await _nav_to_date_picker(page, service, center, visit_types):
            return []
        for _ in range(day.get("week_offset", 0)):
            if not await click_aria(page, FORWARD_ARIA, timeout=1500):
                log.debug("  forward arrow gone before reaching target week")
                break
        else:
            if not await click_aria(page, day["aria"], timeout=2000):
                log.debug(f"  could not click day {day['aria']} (attempt {attempt + 1})")
                continue
            await page.wait_for_timeout(1200)
            labels = await page.evaluate("""
                () => Array.from(document.querySelectorAll('button[aria-label], button'))
                    .filter(b => !!(b.offsetWidth && b.offsetHeight))
                    .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())
            """)
            times = set()
            for s in labels:
                for t in TIME_PAT.findall(s):
                    times.add(re.sub(r'\s+', '', t).upper().replace('AM', ' AM').replace('PM', ' PM'))
            if times:
                return sorted(times)
            if attempt == 0:
                log.info(f"  no times captured for {day['label']}, retrying...")
    return []


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
        if not await _nav_to_date_picker(page, service, center, visit_types):
            log.info(f"  Could not reach date-picker for {center}")
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

        # Phase 2: re-navigate and fetch precise times for each available day
        for d in slots:
            if d.get("label") == "Next available":
                continue  # came from text, no aria to click
            log.info(f"  → fetching times for {d['label']}")
            d["times"] = await _fetch_times_for_day(page, d, service, center, visit_types)
            log.info(f"     times: {d['times'] or '(none captured)'}")

        if slots:
            findings.append({
                "service": service,
                "center": canonical_center(center),
                "slots": slots,
                "booking_url": booking_url,
            })
        else:
            log.info(f"  ❌ Confirmed NO appointments at {center} for {service} "
                     f"(weeks scanned: {dp['weeks_scanned']})")

    # Dedupe findings: same (service, canonical center) with same (date, provider)
    # gets collapsed — happens when two portal labels point to the same physical clinic.
    # Times are merged so a failed time-fetch on one pass doesn't produce a duplicate
    # empty-times entry alongside a successful one.
    seen = {}
    for f in findings:
        key = (f["service"], f["center"])
        if key not in seen:
            seen[key] = {"service": f["service"], "center": f["center"],
                          "slots": [], "booking_url": f["booking_url"],
                          "_slot_index": {}}
        slot_index = seen[key]["_slot_index"]
        for s in f["slots"]:
            slot_key = (s.get("date"), s.get("provider"))
            if slot_key in slot_index:
                # Merge times — prefer the richer set
                existing = slot_index[slot_key]
                new_times = s.get("times") or []
                if len(new_times) > len(existing.get("times") or []):
                    existing["times"] = new_times
            else:
                slot_copy = dict(s)
                slot_index[slot_key] = slot_copy
                seen[key]["slots"].append(slot_copy)
    deduped = []
    for v in seen.values():
        v.pop("_slot_index", None)
        deduped.append(v)
    return deduped


def signature(findings):
    """Stable string used to dedupe notifications across runs."""
    parts = []
    for f in sorted(findings, key=lambda x: (x["service"], x["center"])):
        for s in sorted(f["slots"], key=lambda x: (x.get("date",""), x.get("provider",""))):
            times = ",".join(s.get("times") or [])
            parts.append(f"{f['service']}|{f['center']}|{s.get('date','')}"
                         f"|{s.get('visits',0)}|{s.get('provider','')}|{times}")
    return "\n".join(parts)
