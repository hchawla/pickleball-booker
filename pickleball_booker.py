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

def book_pickleball_session(dry_run: bool = False, target_time: str = None, target_date_str: str = None) -> dict:
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
    
    # CourtReserve display format for matching in scan
    display_date_str = target_date.strftime("%a %b %-d")
    # YYYY-MM-DD for URL/Picker
    iso_date = target_date.strftime("%Y-%m-%d")

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

            # Navigate directly to the date if possible, otherwise use the picker
            # Most CourtReserve sites support a 'datepicker' query param
            page.goto(f"{EVENTS_URL}?datepicker={iso_date}", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            if page.locator("input[placeholder='Enter Your Email']").count() > 0:
                return {"status": "error", "message": "Bounced back to login screen."}

            # Verification: Check if the datepicker shows the requested date
            # If the URL param didn't work, we try to manually set the date input
            try:
                date_input = page.locator("input#datepicker, input.datepicker").first
                if date_input.count() > 0:
                    current_val = date_input.input_value()
                    # If it's not our target date, try to fill it and trigger change
                    if iso_date not in current_val:
                        date_input.fill(iso_date)
                        date_input.press("Enter")
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(2000)
            except Exception:
                pass

            return _scan_and_book(page, display_date_str, dry_run=dry_run, target_h=target_h, target_m=target_m)

        except Exception as e:
            return {"status": "error", "message": f"Unexpected error: {str(e)[:120]}"}
        finally:
            browser.close()


def _scan_and_book(page, target_date_str: str, dry_run: bool = False, target_h: int = None, target_m: int = None) -> dict:
    page.wait_for_timeout(2000)

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
        return {"status": "already_booked", "time": already_booked[0]["time_str"]}

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
        return {"status": "booked", "time": time_display, "date": target_date_str}
    
    return {"status": "error", "message": "Buttons clicked but no success message found."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", type=str, help="Target date in YYYY-MM-DD format")
    parser.add_argument("--target-time", type=str)
    args = parser.parse_args()

    print(json.dumps(book_pickleball_session(args.dry_run, args.target_time, args.date), indent=2))
