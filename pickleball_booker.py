#!/usr/bin/env python3
"""
pickleball_booker.py — CourtReserve Open Play Booker
Pickleball Haven Lake Forest (site ID 13464)

Supports AM, PM, and Full Day membership tiers.
Set MEMBERSHIP_TYPE in .env (AM, PM, or FULL). Defaults to AM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path

# ── Load environment ───────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent
ENV_PATH  = SKILL_DIR / ".env"

def _parse_env_file() -> None:
    """Parse .env file into os.environ. Does NOT overwrite existing keys."""
    if not ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=False)
    except ImportError:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


def _load_env() -> None:
    """
    Precedence (highest wins):
      1. System environment variables (already set)
      2. macOS Keychain (via keyring) — sets env vars, won't overwrite existing
      3. .env file — fills in anything still missing (e.g. MEMBERSHIP_TYPE)

    The .env parse always runs so non-credential config like MEMBERSHIP_TYPE
    is loaded even when credentials come from Keychain.
    """
    # 1. Try Keychain if keyring is available
    try:
        import keyring
        service = "openclaw-pickleball-booker"
        email = keyring.get_password(service, "courtreserve-email")
        password = keyring.get_password(service, "courtreserve-pass")
        if email and "COURTRESERVE_EMAIL" not in os.environ:
            os.environ["COURTRESERVE_EMAIL"] = email
        if password and "COURTRESERVE_PASS" not in os.environ:
            os.environ["COURTRESERVE_PASS"] = password
    except Exception:
        pass

    # 2. Always parse .env for non-credential config (e.g. MEMBERSHIP_TYPE)
    _parse_env_file()

_load_env()


def _log_internal(reason: str, detail: str = "") -> None:
    """Write technical error detail to stderr only — never user-visible.

    The user-facing `message` field in returned dicts must stay clean of
    Python exception text, stack frames, or internal diagnostic strings.
    Anything we want preserved for post-mortem (cron logs, --debug runs)
    goes here.
    """
    if detail:
        sys.stderr.write(f"[pickleball_booker] {reason}: {detail}\n")
    else:
        sys.stderr.write(f"[pickleball_booker] {reason}\n")


LOGIN_URL   = "https://app.courtreserve.com/Online/Account/LogIn/13464"
EVENTS_URL  = "https://app.courtreserve.com/Online/Events/List/13464"

# ── Membership tier config ────────────────────────────────────────────────────

TierWindow = namedtuple("TierWindow", ["start_hour", "start_min", "end_hour", "end_min"])

# Time windows per membership tier (24-hour clock, inclusive both ends).
# TODO: PM floor of 14:30 is assumed from AM cutoff — verify against
#       Pickleball Haven membership docs or CourtReserve before shipping.
TIER_RULES = {
    "AM": TierWindow(start_hour=0, start_min=0, end_hour=14, end_min=30),
    "PM": TierWindow(start_hour=14, start_min=30, end_hour=23, end_min=59),
    # FULL has no entry — it skips time filtering entirely.
}

VALID_TIERS = {"AM", "PM", "FULL"}

# Options for the site's required "Self-Rated Level" registration field
# (a Kendo DropDownList UDF, id="_0__Udfs_0_Value"). Values must match the
# site's dataSource exactly — confirmed from a live registration form snapshot.
VALID_SKILL_LEVELS = {
    "New to PB", "2.0 - 2.5", "2.5 to 3.0", "3.0 to 3.5",
    "3.5 to 4.0", "4.0+", "Here for the Vibe!",
}


# ── Time helpers ───────────────────────────────────────────────────────────────

def _parse_start_time(time_str: str) -> tuple[int, int] | None:
    time_str = time_str.strip().upper()
    m = re.search(r"(\d{1,2})(?::(\d{2}))?(?:\s*(A|P|AM|PM))?", time_str)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    meridiem = m.group(3)

    if meridiem:
        if meridiem.startswith('P') and h != 12: h += 12
        elif meridiem.startswith('A') and h == 12: h = 0
    return h, mn

def _is_within_tier_window(h: int, m: int, tier: str) -> bool:
    """Check if a session start time falls within the allowed window for this tier."""
    if tier == "FULL":
        return True  # No time restriction
    rule = TIER_RULES[tier]
    return (rule.start_hour, rule.start_min) <= (h, m) <= (rule.end_hour, rule.end_min)

def _get_time_diff(h1: int, m1: int, h2: int, m2: int) -> int:
    return abs((h1 * 60 + m1) - (h2 * 60 + m2))


# ── Main entry point ───────────────────────────────────────────────────────────

def _tier_window_label(tier: str) -> str:
    """Human-readable label for a tier's time window, e.g. '2:30 PM - 11:59 PM'."""
    if tier == "FULL":
        return "all day"
    rule = TIER_RULES[tier]
    def _fmt(h, m):
        suffix = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        return f"{display_h}:{m:02d} {suffix}"
    return f"{_fmt(rule.start_hour, rule.start_min)} - {_fmt(rule.end_hour, rule.end_min)}"


def book_pickleball_session(dry_run: bool = False, target_time: str = None, target_date_str: str = None, debug: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"status": "error", "message": "playwright not installed"}

    # Read membership tier after _load_env() has populated os.environ
    membership_type = os.environ.get("MEMBERSHIP_TYPE", "AM").strip().upper()
    if membership_type not in VALID_TIERS:
        return {"status": "error", "message": f"MEMBERSHIP_TYPE '{membership_type}' is invalid. Use AM, PM, or FULL."}

    email    = os.environ.get("COURTRESERVE_EMAIL", "").strip()
    password = os.environ.get("COURTRESERVE_PASS", "").strip()

    if not email or not password:
        return {"status": "error", "message": "COURTRESERVE_EMAIL / COURTRESERVE_PASS not set"}

    # The registration form requires a "Self-Rated Level" selection (site-side
    # validation blocks Finalize without it — this is what caused every past
    # "uncertain" result). Fail fast rather than let every booking attempt
    # bounce off that validation error.
    self_rated_level = os.environ.get("SELF_RATED_LEVEL", "").strip()
    if self_rated_level not in VALID_SKILL_LEVELS:
        return {"status": "error", "message": f"SELF_RATED_LEVEL not set to a valid option. Use one of: {', '.join(sorted(VALID_SKILL_LEVELS))}."}

    # Determine target date
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
        except ValueError:
            return {"status": "error", "message": f"Invalid date format: {target_date_str}. Use YYYY-MM-DD."}
    else:
        target_date = datetime.now()

    display_date_str = target_date.strftime("%A, %B %-d, %Y")   # Monday, April 6, 2026
    iso_date         = target_date.strftime("%Y-%m-%d")          # 2026-04-06

    # Short pattern used in CourtReserve card text e.g. "Mon, Apr 6th, 9a - 12p"
    # "Apr 6" is a substring of "Apr 6th" so strftime("%-d") works without ordinal suffix
    card_date_str = target_date.strftime("%b %-d")  # Apr 6

    # Days from today (floor to midnight for accurate diff)
    today_date  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    target_floor = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    days_diff   = (target_floor - today_date).days

    if days_diff < 0:
        return {"status": "error", "message": f"Target date {display_date_str} is in the past."}
    if days_diff > 7:
        return {"status": "error", "message": f"Target date {display_date_str} is more than 7 days out. CourtReserve limit is 7 days."}

    target_h, target_m = None, None
    if target_time:
        parsed = _parse_start_time(target_time)
        if parsed:
            target_h, target_m = parsed

    # Pre-scan: catch tier/time conflicts before launching the browser
    if target_h is not None and not _is_within_tier_window(target_h, target_m, membership_type):
        window = _tier_window_label(membership_type)
        return {"status": "error", "message": f"Your {membership_type} membership covers sessions {window}. {target_time} is outside your tier window."}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.set_default_timeout(30_000)

        try:
            # ── Login ──────────────────────────────────────────────────────────
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(
                    "input[placeholder='Enter Your Email'], input[type='email'], "
                    "input[placeholder='Enter Your Password'], input[type='password']",
                    timeout=2000,
                )
            except PlaywrightTimeout:
                pass

            email_field = page.locator("input[placeholder='Enter Your Email'], input[type='email']")
            if email_field.count() > 0:
                try:
                    email_field.first.fill(email)
                    page.locator("input[placeholder='Enter Your Password'], input[type='password']").first.fill(password)
                    page.locator("button:has-text('Continue')").first.click()
                    email_field.first.wait_for(state="hidden", timeout=15000)
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    _log_internal("login step failed", f"{type(e).__name__}: {str(e)[:200]}")
                    return {"status": "login_failed", "message": "Couldn't log in to CourtReserve. Credentials may have changed."}

            # ── Navigate to Events List ────────────────────────────────────────
            # This site uses a sidebar filter panel (Today/Tomorrow/This Week/Custom)
            # NOT a datepicker URL param — navigate plain and use the radio buttons.
            page.goto(EVENTS_URL, wait_until="networkidle")
            try:
                page.wait_for_selector(
                    "button:has-text('Register'), a:has-text('Register'), "
                    "input[placeholder='Enter Your Email']",
                    timeout=2000,
                )
            except PlaywrightTimeout:
                pass

            if page.locator("input[placeholder='Enter Your Email']").count() > 0:
                _log_internal("session bounced back to login screen after navigating to events list")
                return {"status": "login_failed", "message": "Couldn't stay logged in to CourtReserve. Try again."}

            # ── Apply date filter via sidebar radio buttons ────────────────────
            try:
                if days_diff == 0:
                    page.get_by_text("Today", exact=True).first.click()
                    page.wait_for_load_state("networkidle")
                    try:
                        page.wait_for_selector(
                            "button:has-text('Register'), a:has-text('Register'), "
                            "button:has-text('Edit Registration'), a:has-text('Edit Registration')",
                            timeout=2000,
                        )
                    except PlaywrightTimeout:
                        pass

                elif days_diff == 1:
                    page.get_by_text("Tomorrow", exact=True).first.click()
                    page.wait_for_load_state("networkidle")
                    try:
                        page.wait_for_selector(
                            "button:has-text('Register'), a:has-text('Register'), "
                            "button:has-text('Edit Registration'), a:has-text('Edit Registration')",
                            timeout=2000,
                        )
                    except PlaywrightTimeout:
                        pass

                else:
                    # For dates 2-7 days out, the default page load already shows
                    # ~10 days of sessions. Per-card date filtering in _scan_and_book
                    # (using card_date_str = "Apr 6") scopes results to the target day.
                    try:
                        page.wait_for_selector(
                            "button:has-text('Register'), a:has-text('Register'), "
                            "button:has-text('Edit Registration'), a:has-text('Edit Registration')",
                            timeout=3000,
                        )
                    except PlaywrightTimeout:
                        pass

            except Exception as e:
                _log_internal("date filter click failed", f"{type(e).__name__}: {str(e)[:200]}")
                return {"status": "error", "message": "Couldn't load sessions for that date. Try again."}

            if debug:
                debug_path = SKILL_DIR / f"debug_{iso_date}.png"
                text_path  = SKILL_DIR / f"debug_{iso_date}.txt"
                page.screenshot(path=str(debug_path), full_page=True)
                body_text = page.inner_text("body")
                text_path.write_text(body_text)
                sys.stderr.write(f"[debug] final screenshot: {debug_path}\n")
                sys.stderr.write(f"[debug] days_diff: {days_diff}, card_date_str: {card_date_str}\n")
                # Show first dates mentioned so we can confirm target date is present
                dates_found = re.findall(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+", body_text)
                sys.stderr.write(f"[debug] dates on page: {list(dict.fromkeys(dates_found))[:10]}\n")

            return _scan_and_book(page, display_date_str, card_date_str, dry_run=dry_run, target_h=target_h, target_m=target_m, tier=membership_type, self_rated_level=self_rated_level)

        except Exception as e:
            _log_internal("unexpected exception inside browser session", f"{type(e).__name__}: {str(e)[:200]}")
            return {"status": "error", "message": "Booking site behaved unexpectedly. Try again."}
        finally:
            browser.close()


def _scan_and_book(page, target_date_str: str, card_date_str: str, dry_run: bool = False, target_h: int = None, target_m: int = None, tier: str = "AM", self_rated_level: str = "") -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    try:
        page.wait_for_selector(
            "button:has-text('Register'), a:has-text('Register'), "
            "button:has-text('Edit Registration'), a:has-text('Edit Registration')",
            timeout=2000,
        )
    except PlaywrightTimeout:
        pass

    # Verify the target date appears somewhere on the page before scanning
    page_body = page.inner_text("body")
    if card_date_str.lower() not in page_body.lower():
        return {"status": "none_available", "message": f"No sessions found for {target_date_str} ({card_date_str}). Sessions may not be posted yet."}

    reg_buttons = page.locator(
        "button:has-text('Register'), a:has-text('Register'), "
        "button:has-text('Edit Registration'), a:has-text('Edit Registration'), "
        "button:has-text('Withdraw'), a:has-text('Withdraw')"
    ).all()

    if not reg_buttons:
        return {"status": "none_available", "message": f"No Register buttons found for {target_date_str}."}

    qualifying_sessions = []

    for btn in reg_buttons:
        card_text = btn.evaluate('''el => {
            let node = el;
            while(node && node.parentElement) {
                node = node.parentElement;
                // Stop at the nearest card-level container (Bootstrap card or
                // CourtReserve event wrapper). This prevents grabbing text from
                // sibling event cards that happen to share a grandparent.
                let cls = (node.className || "").toLowerCase();
                if (cls.includes("card") || cls.includes("event-item") ||
                    cls.includes("panel") || cls.includes("list-group-item") ||
                    (node.tagName === "DIV" && node.getAttribute("data-event-id"))) {
                    let txt = node.innerText || "";
                    if (txt.toUpperCase().includes("OPEN PLAY")) return txt;
                }
            }
            return "";
        }''')

        if not card_text: continue
        text = card_text

        # Skip cards not belonging to the target date.
        # Card text contains e.g. "Mon, Apr 6th, 9a - 12p" so "Apr 6" is a substring.
        if card_date_str.lower() not in text.lower():
            continue

        time_pat = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|a|p))"
        range_pat = rf"{time_pat}\s*[-–—to]+\s*{time_pat}"
        
        time_match = re.search(range_pat, text, re.IGNORECASE)
        if time_match:
            start_str = time_match.group(1)
            time_display = f"{time_match.group(1)}–{time_match.group(2)}"
        else:
            time_match = re.search(time_pat, text, re.IGNORECASE)
            if not time_match: continue
            start_str = time_match.group(1)
            time_display = start_str

        parsed = _parse_start_time(start_str)
        if not parsed: continue
        h, m = parsed

        if not _is_within_tier_window(h, m, tier): continue

        if target_h is not None and target_m is not None:
            diff_mins = _get_time_diff(h, m, target_h, target_m)
            if diff_mins > 45:
                continue

        fee_cents = 0
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        is_free = any(line.upper() == 'FREE' for line in lines)
        
        if not is_free:
            for line in reversed(lines):
                m_fee = re.fullmatch(r"\$?(\d+(?:\.\d{2})?)", line)
                if m_fee:
                    fee_cents = int(float(m_fee.group(1)) * 100)
                    break

        if fee_cents > 0: continue

        btn_text = btn.inner_text().strip().upper()
        already = btn_text in ("EDIT REGISTRATION", "WITHDRAW")

        qualifying_sessions.append({
            "time_str": time_display,
            "start_h": h, "start_m": m,
            "already_booked": already, 
            "element": btn 
        })

    if not qualifying_sessions:
        window = _tier_window_label(tier)
        return {"status": "none_available", "message": f"No free Open Play sessions found for your {tier} membership ({window}) on {target_date_str}."}

    if target_h is not None and target_m is not None:
        qualifying_sessions.sort(key=lambda s: (_get_time_diff(s["start_h"], s["start_m"], target_h, target_m), s["start_h"], s["start_m"]))
    else:
        qualifying_sessions.sort(key=lambda s: (s["start_h"], s["start_m"]))

    if dry_run:
        return {
            "status": "dry_run",
            "sessions": [{"time": s["time_str"], "already_booked": s["already_booked"]} for s in qualifying_sessions],
            "date": target_date_str,
        }

    unbooked_sessions = [s for s in qualifying_sessions if not s["already_booked"]]
    if not unbooked_sessions:
        return {"status": "already_booked", "time": qualifying_sessions[0]["time_str"], "date": target_date_str}

    target = unbooked_sessions[0]

    return _register_session(page, target, target_date_str, self_rated_level)


def _register_session(page, session: dict, target_date_str: str, self_rated_level: str = "") -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    first_btn = session.get("element")
    time_display = session["time_str"]

    if first_btn is None:
        _log_internal("registration button reference was lost between scan and click")
        return {"status": "error", "message": "Booking site changed mid-scan. Try again."}

    try:
        first_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        first_btn.click(force=True)
        try:
            page.wait_for_selector("input[type='checkbox'], #_0__Udfs_0_Value", timeout=3000)
        except PlaywrightTimeout:
            pass

    except Exception as e:
        _log_internal("could not open the registration form", f"{type(e).__name__}: {str(e)[:200]}")
        return {"status": "error", "message": "Couldn't open the registration form. Try again."}

    second_clicked = False
    finalize_clicked = False

    try:
        page.evaluate('''() => {
            document.querySelectorAll("input[type='checkbox']").forEach(cb => {
                if (cb.offsetParent !== null && !cb.checked) {
                    cb.click();
                }
            });
        }''')
        page.wait_for_timeout(500)

        # Required "Self-Rated Level" field (Kendo DropDownList UDF). The site
        # blocks Finalize with a validation error if this isn't set — this was
        # the actual cause of every past "uncertain" result.
        level_set = page.evaluate('''(level) => {
            if (typeof jQuery === "undefined") return "no-jquery";
            var $el = jQuery("#_0__Udfs_0_Value");
            if ($el.length === 0) return "not-present";
            var widget = $el.data("kendoDropDownList");
            if (!widget) return "no-widget";
            widget.value(level);
            widget.trigger("change");
            return "set";
        }''', self_rated_level)
        if level_set != "set":
            _log_internal("self-rated level field not set", level_set)
        page.wait_for_timeout(500)

        second_clicked = page.evaluate('''() => {
            let elements = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a.btn'));
            let visibleTargets = elements.filter(el => {
                let text = (el.innerText || el.value || "").toUpperCase();
                let isVisible = el.offsetParent !== null;
                let isFinalizeButton = text.includes("FINALIZE") || text.includes("COMPLETE") || text.includes("CHECK OUT");
                return isVisible && !isFinalizeButton && (text.includes("REGISTER") || text.includes("SAVE") || text.includes("CONFIRM"));
            });

            if (visibleTargets.length > 0) {
                visibleTargets[visibleTargets.length - 1].click();
                return true;
            }
            return false;
        }''')

        if second_clicked:
            try:
                page.wait_for_selector(
                    "button:has-text('Finalize'), button:has-text('Complete'), "
                    "button:has-text('Check Out')",
                    timeout=3000,
                )
            except PlaywrightTimeout:
                pass

        finalize_clicked = page.evaluate('''() => {
            let elements = Array.from(document.querySelectorAll('button, input, a.btn'));
            let target = elements.find(el => {
                let text = (el.innerText || el.value || "").toUpperCase();
                return el.offsetParent !== null && (text.includes("FINALIZE") || text.includes("COMPLETE") || text.includes("CHECK OUT"));
            });
            if (target) {
                target.click();
                return true;
            }
            return false;
        }''')

        if finalize_clicked:
            page.wait_for_load_state("networkidle")
            
    except Exception as e:
        _log_internal("registration JS step failed", f"{type(e).__name__}: {str(e)[:200]}")
        return {"status": "uncertain", "time": time_display, "date": target_date_str,
                "message": "Booking form didn't respond. Please verify on CourtReserve."}

    if second_clicked or finalize_clicked:
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightTimeout:
            pass

    page_text = page.inner_text("body").upper()

    # Primary: explicit confirmation copy.
    success_keywords = ["SUCCESS", "CONFIRMED", "MY BOOKINGS", "REGISTERED",
                        "THANK YOU", "SPOT IS SAVED", "YOU ARE IN", "YOU'RE IN",
                        "REGISTRATION COMPLETE", "BOOKING SUMMARY", "ADDED TO YOUR RESERVATIONS",
                        "VIEW MY RESERVATIONS", "RESERVATION CONFIRMED"]
    # Secondary: after a successful booking CourtReserve redirects back to the events
    # list where your session now shows "Edit Registration" or "Withdraw" or "Cancel Registration" —
    # all mean you are registered. "COMPLETE" alone is too generic so it's grouped here.
    post_redirect_keywords = ["EDIT REGISTRATION", "WITHDRAW", "CANCEL REGISTRATION", "COMPLETE"]

    if any(word in page_text for word in success_keywords + post_redirect_keywords):
        return {"status": "booked", "time": time_display, "date": target_date_str}

    if second_clicked or finalize_clicked:
        # Auto-capture the post-booking page when we can't classify it. Three consecutive
        # `uncertain` returns (May 18/19/20 2026) all turned out to be successful bookings
        # CourtReserve probably changed its post-booking markup. Snapshots let us patch the
        # keyword list with concrete evidence instead of guessing.
        try:
            ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            snap_dir = SKILL_DIR / "debug" / f"uncertain_{ts}"
            snap_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(snap_dir / "page.png"), full_page=True)
            (snap_dir / "body.txt").write_text(page.inner_text("body"))
            (snap_dir / "body.html").write_text(page.content())
            (snap_dir / "url.txt").write_text(page.url)
            _log_internal("uncertain snapshot saved", str(snap_dir))
        except Exception as _snap_err:
            _log_internal("uncertain snapshot failed", f"{type(_snap_err).__name__}: {str(_snap_err)[:200]}")
        if level_set != "set":
            return {"status": "uncertain", "time": time_display, "date": target_date_str,
                    "message": f"Registration steps completed, but the Self-Rated Level field couldn't be set automatically ({level_set}). CourtReserve may have blocked Finalize because of this — please check CourtReserve to verify."}
        return {"status": "uncertain", "time": time_display, "date": target_date_str,
                "message": "Registration steps completed but no confirmation message detected. Please check CourtReserve to verify."}

    _log_internal("no confirmation keywords matched and no registration buttons were detected as clicked")
    return {"status": "error", "message": "Booking didn't complete. Please verify on CourtReserve."}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Always emits a single line of JSON to stdout.

    Top-level safety net: any exception that escapes book_pickleball_session
    is converted to a user-facing error JSON; the traceback goes to stderr
    only. The agent reading stdout never sees a Python exception, so it
    cannot accidentally narrate one to the user.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", type=str, help="Target date in YYYY-MM-DD format")
    parser.add_argument("--target-time", type=str)
    parser.add_argument("--debug", action="store_true", help="Save screenshot and page text for inspection")
    args = parser.parse_args(argv)

    try:
        result = book_pickleball_session(args.dry_run, args.target_time, args.date, args.debug)
        print(json.dumps(result))
        return 0
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({
            "status": "error",
            "message": "Couldn't reach the booking site. Try again.",
        }))
        return 1


if __name__ == "__main__":
    sys.exit(main())
