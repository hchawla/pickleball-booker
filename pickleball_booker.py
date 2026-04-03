#!/usr/bin/env python3
"""
pickleball_booker.py — CourtReserve AM Open Play Booker
Pickleball Haven Lake Forest (site ID 13464)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Load environment ───────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent
ENV_PATH  = SKILL_DIR / ".env"

def _load_env() -> None:
    """
    Priority:
    1. System environment (already set)
    2. macOS Keychain (via keyring)
    3. .env file fallback
    """
    # 1. Try Keychain if keyring is available
    try:
        import keyring
        service = "openclaw-pickleball-booker"
        email = keyring.get_password(service, "courtreserve-email")
        password = keyring.get_password(service, "courtreserve-pass")
        if email: os.environ["COURTRESERVE_EMAIL"] = email
        if password: os.environ["COURTRESERVE_PASS"] = password
    except Exception:
        pass

    # 2. Try .env fallback if keys are still missing
    if not os.environ.get("COURTRESERVE_EMAIL") or not os.environ.get("COURTRESERVE_PASS"):
        if ENV_PATH.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(ENV_PATH)
            except ImportError:
                # Manual parse as last resort
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

_load_env()

LOGIN_URL   = "https://app.courtreserve.com/Online/Account/LogIn/13464"
EVENTS_URL  = "https://app.courtreserve.com/Online/Events/List/13464"

MAX_START_H = 14   # sessions must start before 14:30 (2:30 PM)
MAX_START_M = 30


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

def _is_before_cutoff(h: int, m: int) -> bool:
    return (h, m) < (MAX_START_H, MAX_START_M)

def _get_time_diff(h1: int, m1: int, h2: int, m2: int) -> int:
    return abs((h1 * 60 + m1) - (h2 * 60 + m2))


# ── Main entry point ───────────────────────────────────────────────────────────

def book_pickleball_session(dry_run: bool = False, target_time: str = None, target_date_str: str = None, debug: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"status": "error", "message": "playwright not installed"}

    email    = os.environ.get("COURTRESERVE_EMAIL", "").strip()
    password = os.environ.get("COURTRESERVE_PASS", "").strip()

    if not email or not password:
        return {"status": "error", "message": "COURTRESERVE_EMAIL / COURTRESERVE_PASS not set"}

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
    picker_date      = target_date.strftime("%m/%d/%Y")          # 04/06/2026 — for custom inputs

    # Multiple patterns to verify the page is showing the right date
    date_check_patterns = [
        target_date.strftime("%a %b %-d"),  # Mon Apr 6
        target_date.strftime("%b %-d"),     # Apr 6
        target_date.strftime("%B %-d"),     # April 6
    ]

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            # ── Login ──────────────────────────────────────────────────────────
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            email_field = page.locator("input[placeholder='Enter Your Email'], input[type='email']")
            if email_field.count() > 0:
                try:
                    email_field.first.fill(email)
                    page.locator("input[placeholder='Enter Your Password'], input[type='password']").first.fill(password)
                    page.locator("button:has-text('Continue')").first.click()
                    email_field.first.wait_for(state="hidden", timeout=15000)
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    return {"status": "login_failed", "message": f"Login Error: {str(e)[:80]}"}

            # ── Navigate to Events List ────────────────────────────────────────
            # This site uses a sidebar filter panel (Today/Tomorrow/This Week/Custom)
            # NOT a datepicker URL param — navigate plain and use the radio buttons.
            page.goto(EVENTS_URL, wait_until="networkidle")
            page.wait_for_timeout(2000)

            if page.locator("input[placeholder='Enter Your Email']").count() > 0:
                return {"status": "error", "message": "Bounced back to login screen."}

            # ── Apply date filter via sidebar radio buttons ────────────────────
            try:
                if days_diff == 0:
                    page.get_by_text("Today", exact=True).click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)

                elif days_diff == 1:
                    page.get_by_text("Tomorrow", exact=True).click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)

                else:
                    # Click "Custom" to reveal date range inputs
                    page.get_by_text("Custom", exact=True).click()
                    page.wait_for_timeout(1500)

                    if debug:
                        snap = SKILL_DIR / f"debug_{iso_date}_after_custom_click.png"
                        page.screenshot(path=str(snap), full_page=True)
                        sys.stderr.write(f"[debug] after Custom click: {snap}\n")

                    # Try multiple selector patterns for start/end date inputs
                    custom_filled = False
                    candidates = [
                        ("input[id*='startDate' i], input[id*='start_date' i], input[name*='startDate' i]",
                         "input[id*='endDate' i],   input[id*='end_date' i],   input[name*='endDate' i]"),
                        ("input[placeholder*='Start' i], input[placeholder*='From' i]",
                         "input[placeholder*='End' i],   input[placeholder*='To' i]"),
                        ("input[type='date']:nth-of-type(1)", "input[type='date']:nth-of-type(2)"),
                    ]
                    for start_sel, end_sel in candidates:
                        try:
                            s = page.locator(start_sel).first
                            e = page.locator(end_sel).first
                            if s.count() > 0 and e.count() > 0:
                                s.fill(picker_date)
                                e.fill(picker_date)
                                e.press("Enter")
                                custom_filled = True
                                break
                        except Exception:
                            pass

                    if not custom_filled:
                        return {"status": "error", "message": f"Could not fill custom date inputs for {picker_date}. Run --debug to inspect the page after clicking Custom."}

                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)

            except Exception as e:
                return {"status": "error", "message": f"Date filter error: {str(e)[:100]}"}

            if debug:
                debug_path = SKILL_DIR / f"debug_{iso_date}.png"
                text_path  = SKILL_DIR / f"debug_{iso_date}.txt"
                page.screenshot(path=str(debug_path), full_page=True)
                text_path.write_text(page.inner_text("body"))
                sys.stderr.write(f"[debug] final screenshot: {debug_path}\n")
                sys.stderr.write(f"[debug] days_diff: {days_diff}, date_check_patterns: {date_check_patterns}\n")

            return _scan_and_book(page, display_date_str, date_check_patterns, dry_run=dry_run, target_h=target_h, target_m=target_m)

        except Exception as e:
            return {"status": "error", "message": f"Unexpected error: {str(e)[:120]}"}
        finally:
            browser.close()


def _scan_and_book(page, target_date_str: str, date_check_patterns: list, dry_run: bool = False, target_h: int = None, target_m: int = None) -> dict:
    page.wait_for_timeout(2000)

    # Verify the page is showing the target date (any format pattern suffices)
    page_body = page.inner_text("body")
    page_body_lower = page_body.lower()
    if not any(p.lower() in page_body_lower for p in date_check_patterns):
        return {"status": "error", "message": f"Page does not show sessions for {target_date_str}. Checked patterns: {date_check_patterns}. Run --debug to inspect."}

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
                let txt = node.innerText || "";
                if (txt.toUpperCase().includes('OPEN PLAY')) return txt;
            }
            return "";
        }''')

        if not card_text: continue
        text = card_text

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

        if not _is_before_cutoff(h, m): continue

        if target_h is not None and target_m is not None:
            diff_mins = _get_time_diff(h, m, target_h, target_m)
            if diff_mins > 45:
                continue

        fee_cents = 0
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        is_free = any(line.upper() == 'FREE' for line in lines)
        
        if not is_free:
            for line in reversed(lines): 
                m_fee = re.search(r"^\$?(\d+(?:\.\d{2})?)", line)
                if m_fee and len(line) < 15:
                    fee_cents = int(float(m_fee.group(1)) * 100)
                    break

        if fee_cents > 0: continue

        already = bool(re.search(r"registered|already registered|edit registration|withdraw", text, re.IGNORECASE))

        qualifying_sessions.append({
            "time_str": time_display,
            "start_h": h, "start_m": m,
            "already_booked": already, 
            "element": btn 
        })

    if not qualifying_sessions:
        return {"status": "none_available", "message": "No FREE AM Open Play found."}

    if target_h is not None and target_m is not None:
        qualifying_sessions.sort(key=lambda s: (_get_time_diff(s["start_h"], s["start_m"], target_h, target_m), s["start_h"], s["start_m"]))
    else:
        qualifying_sessions.sort(key=lambda s: (s["start_h"], s["start_m"]))

    already_booked = [s for s in qualifying_sessions if s["already_booked"]]
    if already_booked:
        return {"status": "already_booked", "time": already_booked[0]["time_str"], "date": target_date_str}

    target = qualifying_sessions[0]

    if dry_run:
        return {"status": "dry_run", "would_book": target["time_str"], "date": target_date_str}

    return _register_session(page, target, target_date_str)


def _register_session(page, session: dict, target_date_str: str) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    first_btn = session.get("element")
    time_display = session["time_str"]

    if first_btn is None:
        return {"status": "error", "message": "Element lost during scan"}

    try:
        first_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        first_btn.click(force=True) 
        page.wait_for_timeout(3000) 
        
    except Exception as e:
        return {"status": "error", "message": f"Step 1 failed: {str(e)[:50]}"}

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

        second_clicked = page.evaluate('''() => {
            let elements = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a.btn'));
            let visibleTargets = elements.filter(el => {
                let text = (el.innerText || el.value || "").toUpperCase();
                let isVisible = el.offsetParent !== null; 
                return isVisible && (text.includes("REGISTER") || text.includes("SAVE") || text.includes("CONFIRM"));
            });

            if (visibleTargets.length > 0) {
                visibleTargets[visibleTargets.length - 1].click();
                return true;
            }
            return false;
        }''')

        if second_clicked:
            page.wait_for_timeout(3000) 

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
        return {"status": "error", "message": f"JS Execution failed: {str(e)[:50]}"}

    page.wait_for_timeout(4000) 
    
    page_text = page.inner_text("body").upper()
    success_keywords = ["SUCCESS", "CONFIRMED", "MY BOOKINGS", "REGISTERED", "COMPLETE", "THANK YOU", "SPOT IS SAVED"]
    
    if any(word in page_text for word in success_keywords):
        return {"status": "booked", "time": time_display, "date": target_date_str}
    
    if second_clicked or finalize_clicked:
        return {"status": "uncertain", "time": time_display, "date": target_date_str,
                "message": "Registration steps completed but no confirmation message detected. Please check CourtReserve to verify."}

    return {"status": "error", "message": "No confirmation message found and no registration buttons were clicked."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", type=str, help="Target date in YYYY-MM-DD format")
    parser.add_argument("--target-time", type=str)
    parser.add_argument("--debug", action="store_true", help="Save screenshot and page text for inspection")
    args = parser.parse_args()

    print(json.dumps(book_pickleball_session(args.dry_run, args.target_time, args.date, args.debug), indent=2))
