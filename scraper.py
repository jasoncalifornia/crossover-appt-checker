"""
Shared booking-flow scraper for the Crossover patient portal.

Used by both check_appointments.py (Mac/local) and check_cloud.py (GitHub
Actions). Both scripts handle login + notification differently; this
module handles only the booking-flow navigation and the date-picker
parsing.
"""

import asyncio
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


async def check_target(ctx, target, weeks_ahead):
    """Returns list of findings. ctx is a playwright BrowserContext."""
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

        # Phase 1: scan date picker with a dedicated page, then close it
        page = await ctx.new_page()
        booking_url = dp = None
        try:
            if not await _nav_to_date_picker(page, service, center, visit_types):
                log.info(f"  Could not reach date-picker for {center}")
                continue
            page_text = await page.inner_text("body")
            if "no providers available" in page_text.lower():
                log.info(f"  No providers available at {center}")
                continue
            booking_url = page.url
            dp = await scrape_date_picker(page, weeks_ahead)
        finally:
            await page.close()

        slots = dp["available_days"] if dp else []
        if not slots and dp and dp["next_available_text"]:
            slots = [{
                "label": "Next available",
                "date": dp["next_available_text"],
                "visits": 1,
                "provider": "see portal",
                "times": [],
            }]
            log.info(f"  📣 'Next available: {dp['next_available_text']}' treated as found")

        # Phase 2: fetch times in parallel — each day gets its own page (max 3 at once)
        sem = asyncio.Semaphore(3)

        async def _fetch(d):
            if d.get("label") == "Next available":
                return []
            log.info(f"  → fetching times for {d['label']}")
            async with sem:
                p = await ctx.new_page()
                try:
                    times = await _fetch_times_for_day(p, d, service, center, visit_types)
                    log.info(f"     times: {times or '(none captured)'}")
                    return times
                finally:
                    await p.close()

        time_results = await asyncio.gather(*[_fetch(d) for d in slots])
        for d, times in zip(slots, time_results):
            if d.get("label") != "Next available":
                d["times"] = times

        if slots:
            findings.append({
                "service": service,
                "center": canonical_center(center),
                "slots": slots,
                "booking_url": booking_url,
            })
        else:
            weeks_scanned = dp["weeks_scanned"] if dp else 0
            log.info(f"  ❌ Confirmed NO appointments at {center} for {service} "
                     f"(weeks scanned: {weeks_scanned})")

    # Dedupe: collapse duplicate (service, center) entries from alias centers
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


_CENTER_SHORT = {
    "San Tomas/Santa Clara": "Santa Clara",
    "Shoreline/Mountain View": "Mountain View",
    "Sunnyvale/Mathilda": "Sunnyvale",
}

def short_center(name):
    return _CENTER_SHORT.get(name, name)


def _slot_date_key(s):
    from datetime import datetime
    try:
        return datetime.strptime(s.get("date", ""), "%b %d").replace(year=datetime.now().year)
    except ValueError:
        return datetime.max


def _sort_times(times):
    from datetime import datetime
    def time_key(t):
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                return datetime.strptime(t.strip(), fmt)
            except ValueError:
                continue
        return datetime.max
    return sorted(times, key=time_key)


_ICON_CALENDAR    = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>'
_ICON_ARROW       = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>'
_ICON_ACUPUNCTURE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="4" r="2"/><line x1="12" y1="6" x2="12" y2="22"/></svg>'
_ICON_EYE         = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
_ICON_CHIRO       = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="4" rx="1"/><rect x="9" y="10" width="6" height="4" rx="1"/><rect x="9" y="18" width="6" height="4" rx="1"/><line x1="12" y1="6" x2="12" y2="10"/><line x1="12" y1="14" x2="12" y2="18"/></svg>'
_ICON_PT          = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="16"/><path d="M7 10l5 2 5-2"/><line x1="10" y1="16" x2="8" y2="21"/><line x1="14" y1="16" x2="16" y2="21"/></svg>'
_ICON_MEDICAL     = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>'


def _service_icon(service):
    s = service.lower()
    if any(k in s for k in ("acupuncture", "needle", "acu")): return _ICON_ACUPUNCTURE
    if any(k in s for k in ("eye", "vision", "optom", "ophthalm")): return _ICON_EYE
    if any(k in s for k in ("chiro", "spine", "back")): return _ICON_CHIRO
    if any(k in s for k in ("physical", " pt", "therapy", "rehab")): return _ICON_PT
    return _ICON_MEDICAL


def displayed_slot_count(findings):
    """Count slots that will actually appear in the email (have times or are Next available)."""
    return sum(
        1 for f in findings for s in f["slots"]
        if s.get("times") or s.get("label") == "Next available"
    )


def build_html_email(findings):
    from collections import defaultdict
    from datetime import datetime

    now = datetime.now()
    groups = defaultdict(list)
    group_url = {}
    for f in findings:
        center = short_center(f["center"])
        for s in f["slots"]:
            if not s.get("times") and s.get("label") != "Next available":
                continue
            key = (f["service"], center, s.get("provider") or "")
            groups[key].append(s)
            group_url[key] = f.get("booking_url", PORTAL_URL)

    for key in groups:
        groups[key].sort(key=_slot_date_key)

    total_slots = sum(len(v) for v in groups.values())
    date_str = now.strftime("%B %-d, %Y")
    time_str = now.strftime("%-I:%M %p")

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
        booking_url = group_url.get(key, PORTAL_URL)
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


def signature(findings):
    """Stable string used to dedupe notifications across runs."""
    parts = []
    for f in sorted(findings, key=lambda x: (x["service"], x["center"])):
        for s in sorted(f["slots"], key=lambda x: (x.get("date",""), x.get("provider",""))):
            times = ",".join(s.get("times") or [])
            parts.append(f"{f['service']}|{f['center']}|{s.get('date','')}"
                         f"|{s.get('visits',0)}|{s.get('provider','')}|{times}")
    return "\n".join(parts)
