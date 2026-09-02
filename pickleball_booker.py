#!/usr/bin/env python3
"""
pickleball_booker.py — CourtReserve Open Play Booker
Pickleball Haven Lake Forest (site ID 13464)

Supports AM, PM, and Full Day membership tiers.
The tier comes from "membership_tier" in preferences.json; MEMBERSHIP_TYPE in
.env is only a fallback for a checkout that has no preferences.json. There is
no default — an unset tier is reported, never guessed.
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

SKILL_DIR  = Path(__file__).parent
ENV_PATH   = SKILL_DIR / ".env"
PREFS_PATH = SKILL_DIR / "preferences.json"

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


def _safe_read(fn, default, reason: str):
    """Run a page read that may race a navigation; log and fall back on failure."""
    try:
        return fn()
    except Exception as e:
        _log_internal(reason, f"{type(e).__name__}: {str(e)[:200]}")
        return default


def _read_preferences() -> dict:
    """Standing owner preferences, or {} when the file is absent or unreadable.

    `preferences.json` is `.gitignore`d, so a fresh checkout genuinely has no
    copy. That case has to surface — see _membership_tier.
    """
    try:
        with open(PREFS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log_internal("could not read preferences.json", f"{type(e).__name__}: {str(e)[:200]}")
        return {}


def _membership_tier() -> tuple[str | None, str]:
    """(tier, where it came from), or (None, "") when nothing is configured.

    `preferences.json` is the single source of truth — SKILL.md tells the agent
    to read it on every booking turn, so the booker has to enforce the same
    value or the two silently disagree. `MEMBERSHIP_TYPE` in `.env` is kept
    only as a fallback for a checkout without a preferences file.

    There is deliberately no default. Silently assuming AM made a FULL member's
    every evening request come back "No free Open Play sessions found for your
    AM membership (12:00 AM - 2:30 PM)" with nothing to indicate the tier had
    been guessed.
    """
    prefs = _read_preferences()
    raw = prefs.get("membership_tier")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper(), "preferences.json"
    raw = os.environ.get("MEMBERSHIP_TYPE", "")
    if raw.strip():
        return raw.strip().upper(), ".env"
    return None, ""


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

# fullmatch, not search: a partial match let "after 5" parse as 5:00 AM and
# "9 April" as 9:00 AM, both of which the caller then treats as a real target
# time. And AM|PM must precede A|P — the ordered alternation matched the "A"
# of "April" before it ever tried "AM".
_CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM|A|P)?\.?")


def _parse_start_time(time_str: str) -> tuple[int, int] | None:
    m = _CLOCK_RE.fullmatch((time_str or "").strip().upper())
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    meridiem = m.group(3)

    if meridiem:
        if meridiem.startswith('P') and h != 12: h += 12
        elif meridiem.startswith('A') and h == 12: h = 0
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h, mn

def _is_within_tier_window(h: int, m: int, tier: str) -> bool:
    """Check if a session start time falls within the allowed window for this tier."""
    if tier == "FULL":
        return True  # No time restriction
    rule = TIER_RULES[tier]
    return (rule.start_hour, rule.start_min) <= (h, m) <= (rule.end_hour, rule.end_min)

def _get_time_diff(h1: int, m1: int, h2: int, m2: int) -> int:
    return abs((h1 * 60 + m1) - (h2 * 60 + m2))


# ── Card parsing ───────────────────────────────────────────────────────────────

# Button labels that mean "you already hold this session". Kept as a constant
# because both the availability scan and the post-booking confirmation check
# have to agree on what a registered card looks like.
REGISTERED_BUTTON_TEXTS = ("EDIT REGISTRATION", "WITHDRAW", "CANCEL REGISTRATION")

# The collector matches button labels on the loose substring /REGIST|WITHDRAW/,
# because it has to: "REGISTER" is not a substring of "REGISTRATION", so a
# tighter filter loses every already-booked card. Which of those labels are
# actually actionable is decided here instead.
# Derived from the tuple above so the two can't drift: the tuple is what the
# tests assert against, and a label added there has to start classifying without
# a second edit here.
_REGISTERED_LABEL_RE  = re.compile(
    r"\b(?:" + "|".join(
        r"\s+".join(re.escape(word) for word in label.split())
        for label in REGISTERED_BUTTON_TEXTS
    ) + r")\b"
)
_UNAVAILABLE_LABEL_RE = re.compile(
    r"\b(?:CLOSED|FULL|SOLD\s*OUT|WAIT\s*-?\s*LIST(?:ED)?|PAST|ENDED|UNAVAILABLE|VIEW)\b"
)
_BOOKABLE_LABEL_RE    = re.compile(r"\bREGISTER\b")

# \b after the meridiem, or the alternation matches ordinary card words: "2 p"
# inside "2 players per court", "4 a" inside "4 available". A card whose title
# reads "50 of 50 spots remaining / 2 players per court" parsed as a 2 PM
# session and was offered to the owner as one.
_TIME_PAT  = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|a|p)\b)"
_RANGE_PAT = rf"{_TIME_PAT}\s*(?:[-\u2013\u2014]+|to)\s*{_TIME_PAT}"

_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
_FREE_RE  = re.compile(r"\bFREE\b", re.IGNORECASE)
# A whole line that says nothing but "FREE" (tolerating "Free!", "(FREE)") is
# CourtReserve's price field. "FREE" loose in a sentence is prose.
_FREE_LINE_RE = re.compile(r"^\W*free\W*$", re.IGNORECASE)

# A pattern that can never match, for callers with no date to match on.
_NEVER_MATCH_RE = re.compile(r"(?!)")


def _normalize_button_label(label: str) -> str:
    """Collapse a button's raw innerText into comparable upper-case words."""
    return re.sub(r"[\s\u00a0\u200b]+", " ", label or "").strip().upper()


def _classify_button(label: str) -> str:
    """'registered' | 'bookable' | 'unavailable' for a card's action button.

    Matched by word, never by equality against the raw innerText. CourtReserve
    renders icon glyphs and wrapped spans inside its buttons \u2014 the captured
    2026-07-16 page shows two buttons whose text ran together as
    "BackFinalize Registration" \u2014 so "\uf044 EDIT\\nREGISTRATION" has to
    classify the same as "Edit Registration". Exact equality reported such a
    card as not-yet-booked and re-registered a session already held.

    Anything that is neither a live Register control nor a registered-state
    control is `unavailable`: "Registration Closed", "Registration Full" and
    "Waitlist Registration" all satisfy the collector's REGIST filter but
    clicking one just bounces off a dead control, and treating them as
    bookable meant the genuinely bookable session further down the list was
    never tried.
    """
    up = _normalize_button_label(label)
    if _REGISTERED_LABEL_RE.search(up):
        return "registered"
    if _UNAVAILABLE_LABEL_RE.search(up):
        return "unavailable"
    if _BOOKABLE_LABEL_RE.search(up):
        return "bookable"
    return "unavailable"


def _card_date_pattern(card_date_str: str) -> re.Pattern:
    """Compile a whole-day matcher for a card date like "Aug 1".

    A plain substring test is wrong: "Aug 1" is inside "Aug 12th", so a booking
    for the 1st matched cards for the 10th-19th and could reserve the wrong
    day. Anchor on the day number and allow the ordinal suffix CourtReserve
    renders ("Mon, Aug 1st, 9a - 12p").

    An empty date compiles to a pattern that matches nothing. It used to
    produce `\\b\\s+(?:st|nd|rd|th)?\\b`, which matches any run of whitespace in
    any card \u2014 so a caller that forgot to pass a date confirmed a booking
    against an arbitrary card on an arbitrary day.
    """
    if not card_date_str or not card_date_str.strip():
        return _NEVER_MATCH_RE
    month, _, day = card_date_str.strip().partition(" ")
    if not month or not day:
        return _NEVER_MATCH_RE
    return re.compile(
        rf"\b{re.escape(month)}\s+{re.escape(day)}(?:st|nd|rd|th)?\b",
        re.IGNORECASE,
    )


def _extract_session_time(text: str, date_re: re.Pattern | None = None) -> tuple[tuple[int, int], str] | None:
    """((start_hour, start_minute), display_string) for a card, or None.

    CourtReserve prints the authoritative range immediately after the date
    ("Mon, Aug 31st, 7a - 9a"). The card title is human-written and can round
    or disagree — "$10 HAPPY HOUR OPEN PLAY (12-2:30 PM)" is a title, and the
    9:15 session that got mis-booked in 0.1.0.1 was titled "9AM-12PM". Anchor
    on the date line whenever the caller knows which date it is looking for,
    and only fall back to the first range anywhere in the card.
    """
    if date_re is not None:
        for date_match in date_re.finditer(text):
            tail = text[date_match.end():date_match.end() + 40]
            anchored = re.search(_RANGE_PAT, tail, re.IGNORECASE)
            if not anchored:
                continue
            parsed = _parse_start_time(anchored.group(1))
            if parsed:
                return parsed, f"{anchored.group(1)}\u2013{anchored.group(2)}"

    match = re.search(_RANGE_PAT, text, re.IGNORECASE)
    if match:
        start_str = match.group(1)
        display   = f"{match.group(1)}\u2013{match.group(2)}"
    else:
        match = re.search(_TIME_PAT, text, re.IGNORECASE)
        if not match:
            return None
        start_str = display = match.group(1)

    parsed = _parse_start_time(start_str)
    if not parsed:
        return None
    return parsed, display


# Card text that disqualifies a card even though it contains "OPEN PLAY".
# The substring test is unavoidably loose, so every exclusion here comes from a
# card seen in the wild:
#   BEGINNER      — "Beginner Open Play" (booked by mistake 2026-07-31)
#   NO OPEN PLAY  — "Haven Cup *Tournament Players & Spectators* - NO OPEN PLAY"
#                   (the only card on 2026-08-30; a booking run would have
#                    registered for a tournament spectator slot)
#   SPECIAL EVENT — the category CourtReserve prints on that same card; only
#                   Open Play is supported (see SKILL.md "Known Limitations")
_CARD_EXCLUSIONS = ("BEGINNER", "NO OPEN PLAY", "SPECIAL EVENT")


def _is_open_play_card(text: str) -> bool:
    """True when a card is a regular Open Play session we're allowed to book."""
    upper = text.upper()
    if "OPEN PLAY" not in upper:
        return False
    return not any(marker in upper for marker in _CARD_EXCLUSIONS)


def _card_is_free(text: str) -> bool:
    """True when a card advertises no fee.

    Precedence matters, because the two signals disagree in the wild:

      1. CourtReserve's own price field renders the word "FREE". It is
         structured data, it is priced for the account we are logged in as, and
         it wins outright. The live "$10 HAPPY HOUR OPEN PLAY (12-2:30 PM)"
         card carries $10 in its human-written *title* — the non-member rate —
         and FREE in its price field, because the membership covers it.
         Trusting the title here would refuse sessions the owner is entitled to
         book.

      2. The same field rendered INLINE with the date — "Sat, Aug 1st  9a - 12p
         FREE" in the captured debug/uncertain_2026-08-30T18-42-03 — counts too,
         but only on a card carrying no positive price anywhere. Matching a bare
         "FREE" anywhere unconditionally is too loose: on a "$12.00 per player -
         includes FREE paddle rental" card it books a paid session, which is the
         exact failure 0.1.0.2 was written to fix. Own-line beats a title price;
         loose-in-prose does not.
      3. Otherwise any "$N" above zero marks the card paid. An older rule looked
         for a bare number on its own line, which let real prices through
         whenever they were formatted as "$12.00 per player" and dropped free
         sessions whose card happened to end in an unrelated number such as a
         spots-remaining count.
      4. A card with no price information at all is treated as paid, i.e. not
         offered. Fail-closed is deliberate: booking a session that turns out
         to cost money is worse than missing one, and the `none_available`
         message now names the fee check as the reason so the drop is visible
         rather than silent.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    prices = [float(m.group(1)) for m in _PRICE_RE.finditer(text)]

    if any(_FREE_LINE_RE.match(line) for line in lines):
        return True
    if _FREE_RE.search(text) and not any(p > 0 for p in prices):
        return True
    if prices:
        return max(prices) == 0
    return False


# One browser round trip returns every Open Play card on the page together with
# its action-button label, and tags each button with data-pb-idx so it can be
# clicked later without holding a stale element handle. Doing this per button
# cost two round trips per card.
_COLLECT_CARDS_JS = """() => {
    const CARD_HINTS = ["card", "event-item", "panel", "list-group-item"];
    const ACTION_SEL = "button, a, input[type='submit'], input[type='button']";
    const ACTION_RE = /REGIST|WITHDRAW/;
    const labelOf = el => ((el.innerText || el.value || "").trim()).toUpperCase();
    const actionsInside = node => Array.from(node.querySelectorAll(ACTION_SEL))
        .filter(a => ACTION_RE.test(labelOf(a))).length;
    const out = [];
    document.querySelectorAll(ACTION_SEL).forEach(el => {
        const label = labelOf(el);
        // "REGISTER" is NOT a substring of "REGISTRATION" (no second E), so
        // matching on it silently dropped every "Edit Registration" and
        // "Cancel Registration" card — i.e. every session already booked.
        // Labels that only *look* bookable ("Registration Closed") pass this
        // filter too; _classify_button sorts them out on the Python side.
        if (!ACTION_RE.test(label)) return;
        // Climb to the nearest card-level container (Bootstrap card or
        // CourtReserve event wrapper) whose text names an Open Play session —
        // but stop dead at the first ancestor that holds a SECOND action
        // button, because that ancestor is a multi-card wrapper, not a card.
        //
        // Without that bound the walk kept climbing whenever a card-level
        // container lacked "OPEN PLAY": a Clinic's own card would fail the
        // test, the walk would reach the enclosing card-deck, and the deck's
        // aggregate innerText (date, time range and FREE, all from sibling
        // Open Play cards) got attached to the Clinic's Register button. Every
        // downstream check then passed on borrowed text and the booker clicked
        // the wrong event's button.
        let node = el;
        for (let depth = 0; node.parentElement && depth < 12; depth++) {
            node = node.parentElement;
            const cls = (typeof node.className === "string" ? node.className : "").toLowerCase();
            const isCard = CARD_HINTS.some(h => cls.includes(h)) ||
                           (node.tagName === "DIV" && node.getAttribute("data-event-id"));
            if (!isCard) continue;
            // Counted only on card-level ancestors: they are the only containers
            // this loop can settle on, so they are the only ones where a second
            // action button would mean "multi-card wrapper". Counting every
            // ancestor instead re-scanned an ever-larger subtree per button —
            // on the unfiltered list (205 cards) that is ~500k node visits.
            if (actionsInside(node) > 1) return;
            const txt = node.innerText || "";
            if (txt.toUpperCase().includes("OPEN PLAY")) {
                el.setAttribute("data-pb-idx", String(out.length));
                out.push([txt, label]);
                return;
            }
        }
    });
    return out;
}"""


def _collect_session_cards(page) -> list[tuple[str, str]]:
    """[(card_text, button_label)] for every Open Play card, in DOM order.

    The list index is also written to the button as data-pb-idx, so
    _register_session can re-locate the exact button it decided to click.
    """
    try:
        rows = page.evaluate(_COLLECT_CARDS_JS)
    except Exception as e:
        _log_internal("card collection failed", f"{type(e).__name__}: {str(e)[:200]}")
        return []
    return [(row[0], row[1]) for row in rows or []]


# ── Main entry point ───────────────────────────────────────────────────────────

def _format_clock(h: int, m: int) -> str:
    """'9:00 AM' for (9, 0)."""
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m:02d} {suffix}"


def _tier_window_label(tier: str) -> str:
    """Human-readable label for a tier's time window, e.g. '2:30 PM - 11:59 PM'."""
    if tier == "FULL":
        return "all day"
    rule = TIER_RULES[tier]
    return f"{_format_clock(rule.start_hour, rule.start_min)} - {_format_clock(rule.end_hour, rule.end_min)}"


def _none_available_message(skipped: dict, target_date_str: str, tier: str,
                            target_h: int = None, target_m: int = None) -> str:
    """Name the filter that actually rejected the candidates.

    Five separate checks drop cards, and they all used to collapse into "No
    free Open Play sessions found for your {tier} membership ({window})". For a
    FULL member that renders as "...for your FULL membership (all day)...",
    which names an all-day window as the reason nothing was found — and SKILL.md
    has the agent relay it verbatim, so an owner who asked for 9 AM on a day
    whose only free session is at 2 PM was told there was nothing all day.

    Ordered by how close the card came to being bookable, so the most
    actionable reason wins.
    """
    if skipped.get("far_from_target") and target_h is not None:
        return (f"No free Open Play session within 45 minutes of "
                f"{_format_clock(target_h, target_m)} on {target_date_str}. "
                f"Ask for a different time to see the rest of the day.")
    if skipped.get("not_free"):
        return f"Every Open Play session on {target_date_str} carries a fee, so none were booked."
    if skipped.get("not_bookable"):
        return f"Open Play is listed for {target_date_str}, but registration isn't open on those sessions."
    if skipped.get("outside_tier"):
        return (f"The Open Play sessions on {target_date_str} fall outside your "
                f"{tier} membership window ({_tier_window_label(tier)}).")
    return f"No free Open Play sessions found for {target_date_str}."


def book_pickleball_session(dry_run: bool = False, target_time: str = None, target_date_str: str = None, debug: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"status": "error", "message": "playwright not installed"}

    # Read membership tier after _load_env() has populated os.environ.
    # preferences.json wins; .env is the fallback; neither is an error, not AM.
    membership_type, tier_source = _membership_tier()
    if membership_type is None:
        return {"status": "error",
                "message": "No membership tier is configured. Set \"membership_tier\" to AM, PM, or FULL in preferences.json."}
    if membership_type not in VALID_TIERS:
        return {"status": "error",
                "message": f"Membership tier '{membership_type}' (from {tier_source}) is invalid. Use AM, PM, or FULL."}

    email    = os.environ.get("COURTRESERVE_EMAIL", "").strip()
    password = os.environ.get("COURTRESERVE_PASS", "").strip()

    if not email or not password:
        return {"status": "error", "message": "COURTRESERVE_EMAIL / COURTRESERVE_PASS not set"}

    # The registration form requires a "Self-Rated Level" selection (site-side
    # validation blocks Finalize without it — this is what caused every past
    # "uncertain" result). Fail fast rather than let every booking attempt
    # bounce off that validation error.
    #
    # Booking path only: --dry-run never opens a registration form and never
    # touches the Kendo UDF, so gating it turned "what's available tomorrow?"
    # into a seven-option configuration dump for the owner.
    self_rated_level = os.environ.get("SELF_RATED_LEVEL", "").strip()
    if not dry_run and self_rated_level not in VALID_SKILL_LEVELS:
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
        if not parsed:
            # Falling through with no target silently switched off the
            # proximity filter and booked the *earliest* session of the day —
            # so "book me tonight" landed on the 7 AM slot. The agent builds
            # this string from natural language, so it has to be checked.
            return {"status": "error",
                    "message": f"Couldn't read a time from \"{target_time}\". Use a form like \"9:00 AM\" or \"7:30 PM\"."}
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
                    # For dates 2-7 days out we don't touch the sidebar filter and
                    # rely on per-card date filtering in _scan_and_book instead.
                    # Note the unfiltered view is *large*: measured 2026-08-30 it
                    # rendered 205 Open Play cards across 70 distinct dates, out to
                    # ~2.5 months. So the per-card date match has to be exact — a
                    # substring test for "Sep 2" really does hit "Sep 25" here.
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

            return _scan_and_book(page, display_date_str, card_date_str, dry_run=dry_run, target_h=target_h, target_m=target_m, tier=membership_type, self_rated_level=self_rated_level, days_diff=days_diff)

        except Exception as e:
            _log_internal("unexpected exception inside browser session", f"{type(e).__name__}: {str(e)[:200]}")
            return {"status": "error", "message": "Booking site behaved unexpectedly. Try again."}
        finally:
            browser.close()


def _scan_and_book(page, target_date_str: str, card_date_str: str, dry_run: bool = False, target_h: int = None, target_m: int = None, tier: str = "AM", self_rated_level: str = "", days_diff: int = None) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    try:
        page.wait_for_selector(
            "button:has-text('Register'), a:has-text('Register'), "
            "button:has-text('Edit Registration'), a:has-text('Edit Registration')",
            timeout=2000,
        )
    except PlaywrightTimeout:
        pass

    date_re = _card_date_pattern(card_date_str)

    # Verify the target date appears somewhere on the page before scanning
    page_body = page.inner_text("body")
    if not date_re.search(page_body):
        return {"status": "none_available", "message": f"No sessions found for {target_date_str} ({card_date_str}). Sessions may not be posted yet."}

    cards = _collect_session_cards(page)

    if not cards:
        return {"status": "none_available", "message": f"No Register buttons found for {target_date_str}."}

    qualifying_sessions = []
    # Which filter rejected which card, so `none_available` can name the real
    # reason instead of always blaming the membership window.
    skipped = {"not_open_play": 0, "wrong_date": 0, "not_bookable": 0,
               "unreadable_time": 0, "outside_tier": 0, "not_free": 0,
               "far_from_target": 0}

    # This loop must not touch the browser: card text and button label already
    # arrived in the single _collect_session_cards round trip above.
    for card_index, (text, btn_text) in enumerate(cards):
        # The card filter matches "OPEN PLAY" as a substring, which also hits
        # "Beginner Open Play" and "... - NO OPEN PLAY". Those competed on
        # time-proximity like a regular session and won the sort whenever they
        # happened to sit closer to the target time.
        if not _is_open_play_card(text):
            skipped["not_open_play"] += 1
            continue

        # Skip cards not belonging to the target date.
        if not date_re.search(text):
            skipped["wrong_date"] += 1
            continue

        # "Registration Closed" / "Registration Full" / "Waitlist Registration"
        # all satisfy the collector's REGIST filter but cannot be booked.
        state = _classify_button(btn_text)
        if state == "unavailable":
            skipped["not_bookable"] += 1
            continue

        extracted = _extract_session_time(text, date_re)
        if not extracted:
            skipped["unreadable_time"] += 1
            continue
        (h, m), time_display = extracted

        if not _is_within_tier_window(h, m, tier):
            skipped["outside_tier"] += 1
            continue

        # Fee before proximity, so a card counted in far_from_target is one
        # that was genuinely free and in-window — which is what makes that the
        # most useful reason to report.
        if not _card_is_free(text):
            skipped["not_free"] += 1
            continue

        if target_h is not None and target_m is not None:
            if _get_time_diff(h, m, target_h, target_m) > 45:
                skipped["far_from_target"] += 1
                continue

        qualifying_sessions.append({
            "time_str": time_display,
            "start_h": h, "start_m": m,
            "already_booked": state == "registered",
            "card_index": card_index,
        })

    if not qualifying_sessions:
        return {"status": "none_available",
                "message": _none_available_message(skipped, target_date_str, tier, target_h, target_m)}

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

    # The session the owner actually asked for is the best-ranked one, so it is
    # the one the already-booked check has to look at. Skipping past it to the
    # next unbooked card registered a *second*, overlapping session: with the
    # ±45-minute window a 9:00 request that was already held booked 9:15 as
    # well, and with no target time at all an owner holding 7 AM got 9 AM.
    target = qualifying_sessions[0]
    if target["already_booked"]:
        return {"status": "already_booked", "time": target["time_str"], "date": target_date_str}

    return _register_session(page, target, target_date_str, card_date_str, self_rated_level, days_diff=days_diff)


def _registered_state_for_session(page, card_date_str: str, session: dict) -> str:
    """Registered-state button label on the card for THIS session, else "".

    Scoped to the card matching the session's date *and* start time on purpose.
    Searching the whole page for "Withdraw" also matched every other session
    the member already held, so a booking that silently failed and landed back
    on the events list was reported as `booked`.

    The empty-date guard lives here rather than in one of the two callers: with
    no date there is nothing to scope to, and the pattern an empty string used
    to compile matched every card on the page.
    """
    if not card_date_str:
        return ""
    date_re = _card_date_pattern(card_date_str)
    for text, btn_text in _collect_session_cards(page):
        if _classify_button(btn_text) != "registered":
            continue
        if not date_re.search(text):
            continue
        extracted = _extract_session_time(text, date_re)
        if not extracted:
            continue
        (h, m), _ = extracted
        if (h, m) == (session["start_h"], session["start_m"]):
            return btn_text
    return ""


def _snapshot_uncertain(page) -> None:
    """Preserve a post-booking page we couldn't classify, for post-mortem.

    Every diagnosis this skill has made — the blank Kendo dropdown, the
    portal-dashboard bounce — came out of one of these, so it has to show the
    page the finalize click actually landed on. The events-list reconfirmation
    deliberately runs in its own tab to keep that page intact.
    """
    try:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        snap_dir = SKILL_DIR / "debug" / f"uncertain_{ts}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(snap_dir / "page.png"), full_page=True)
        (snap_dir / "body.txt").write_text(page.inner_text("body"))
        (snap_dir / "body.html").write_text(page.content())
        (snap_dir / "url.txt").write_text(page.url)
        _log_internal("uncertain snapshot saved", str(snap_dir))
    except Exception as snap_err:
        _log_internal("uncertain snapshot failed", f"{type(snap_err).__name__}: {str(snap_err)[:200]}")


def _apply_date_filter(page, days_diff: int = None) -> None:
    """Click the Today / Tomorrow sidebar radio, exactly as the main scan does.

    This site filters by sidebar radio, not by URL param, and the unfiltered
    events list does not reliably render today's or tomorrow's cards. A probe
    that skips the click reads a view that may not contain the card it is
    trying to confirm, and a completed booking comes back `uncertain`.
    """
    label = {0: "Today", 1: "Tomorrow"}.get(days_diff)
    if label is None:
        return
    try:
        page.get_by_text(label, exact=True).first.click()
        page.wait_for_load_state("networkidle")
    except Exception as e:
        _log_internal(f"could not apply the {label} filter", f"{type(e).__name__}: {str(e)[:200]}")


def _confirm_on_events_list(page, card_date_str: str, session: dict, days_diff: int = None) -> str:
    """Re-read the events list and report this session's button label, or "".

    The authoritative post-booking check: the events list is the same source
    the availability scan trusts, and a registered session shows there as
    "Edit Registration" / "Withdraw" regardless of where the finalize click
    happened to land.
    """
    if not card_date_str:
        return ""
    probe = None
    try:
        # A second tab, so the page the finalize click landed on stays intact
        # for _snapshot_uncertain. Navigating `page` itself would overwrite the
        # only evidence we get when the booking is genuinely ambiguous.
        probe = page.context.new_page()
        probe.set_default_timeout(30_000)
        probe.goto(EVENTS_URL, wait_until="networkidle")
        _apply_date_filter(probe, days_diff)
        return _registered_state_for_session(probe, card_date_str, session)
    except Exception as e:
        _log_internal("could not reload events list to confirm", f"{type(e).__name__}: {str(e)[:200]}")
        return ""
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


# Waivers and terms boxes have to be ticked; paid add-ons and marketing
# opt-ins must not be. The old code clicked every visible unchecked checkbox on
# the form, so an "Add a guest" or "Bring a partner" box on a session the fee
# check had cleared as FREE could attach a charge nothing downstream re-reads.
_CHECKBOX_JS = """() => {
    const RISKY = /GUEST|PARTNER|ADD[- ]?ON|ADDITIONAL PLAYER|BRING A |SUBSCRIB|NEWSLETTER|MARKETING|PROMOTION|DONAT|\\bTIP\\b|PURCHASE|UPGRADE|OPT[- ]?IN|EMAIL ME|TEXT ME/;
    const textFor = cb => {
        const bits = [cb.id || "", cb.name || "", cb.value || "",
                      cb.getAttribute("aria-label") || ""];
        (cb.labels ? Array.from(cb.labels) : []).forEach(l => bits.push(l.innerText || ""));
        const wrap = cb.closest("label, .form-group, .checkbox, li, td, div");
        if (wrap) bits.push((wrap.innerText || "").slice(0, 300));
        return bits.join(" ").toUpperCase();
    };
    let clicked = 0;
    const skipped = [];
    document.querySelectorAll("input[type='checkbox']").forEach(cb => {
        if (cb.offsetParent === null || cb.checked || cb.disabled) return;
        const required = cb.required || cb.getAttribute("aria-required") === "true";
        if (!required && RISKY.test(textFor(cb))) {
            skipped.push(textFor(cb).slice(0, 80));
            return;
        }
        cb.click();
        clicked++;
    });
    return {clicked: clicked, skipped: skipped};
}"""


def _register_session(page, session: dict, target_date_str: str, card_date_str: str = "", self_rated_level: str = "", days_diff: int = None) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    time_display = session["time_str"]
    first_btn = page.locator(f'[data-pb-idx="{session["card_index"]}"]')

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
    level_set = "not-attempted"

    try:
        checkbox_result = page.evaluate(_CHECKBOX_JS)
        if isinstance(checkbox_result, dict) and checkbox_result.get("skipped"):
            _log_internal("left optional checkboxes unticked on the registration form",
                          "; ".join(checkbox_result["skipped"])[:400])
        page.wait_for_timeout(500)

        # Required "Self-Rated Level" field (Kendo DropDownList UDF). The site
        # blocks Finalize with a validation error if this isn't set — this was
        # the actual cause of every past "uncertain" result.
        # Read the value back. kendoDropDownList.value() silently no-ops when the
        # argument is not in a loaded dataSource, so returning "set" straight
        # after the call reported success the field never had — which sent the
        # `uncertain` branch below to the generic message and hid the real cause.
        level_set = page.evaluate('''(level) => {
            if (typeof jQuery === "undefined") return "no-jquery";
            var $el = jQuery("#_0__Udfs_0_Value");
            if ($el.length === 0) return "not-present";
            var widget = $el.data("kendoDropDownList");
            if (!widget) return "no-widget";
            widget.value(level);
            if (widget.value() !== level) {
                // dataSource may not have been read yet; fall back to selecting
                // the matching item by its rendered text.
                try {
                    var data = (widget.dataSource && widget.dataSource.data)
                        ? widget.dataSource.data() : [];
                    for (var i = 0; i < data.length; i++) {
                        var item = data[i];
                        var txt = (item && item.Value !== undefined) ? item.Value
                                : (item && item.text !== undefined) ? item.text : item;
                        if (String(txt).trim() === level) { widget.select(i); break; }
                    }
                } catch (err) { /* fall through to the verification below */ }
            }
            widget.trigger("change");
            return widget.value() === level
                ? "set"
                : "value-rejected:" + (widget.value() || "empty");
        }''', self_rated_level)
        # "not-present" means this event simply has no Self-Rated Level field,
        # which is normal — only a field that exists and refused the value is a
        # problem worth reporting.
        if level_set not in ("set", "not-present"):
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

    # Reading the landing page can itself throw — CourtReserve redirects to
    # /Portal/Index?forceDashboard=True after finalize, and a Playwright call
    # against a page still navigating raises. That exception used to escape
    # into book_pickleball_session's catch-all and turn a *completed* booking
    # into a generic `error` with no snapshot, which SKILL.md relays verbatim
    # and preferences.json's auto_confirm turns into a duplicate booking on the
    # owner's natural retry. Each read is guarded so classification can carry
    # on to the events-list check, which is the reliable signal anyway.
    page_text = _safe_read(lambda: page.inner_text("body").upper(), "",
                           "could not read the post-booking page")
    current_url = _safe_read(lambda: page.url.upper(), "",
                             "could not read the post-booking URL")

    # Explicit confirmation copy. Every phrase here has to be unambiguous on its
    # own, because it is matched against the whole page. The old list carried
    # bare "COMPLETE", "REGISTERED" and "WITHDRAW", which also matched a
    # "Complete your profile" nag and any *other* session the member already
    # held — so a booking that never went through returned `booked`.
    #
    # "REGISTRATION SUCCESSFUL" is the string CourtReserve actually prints:
    # debug/uncertain_2026-08-30T19-54-46 captured it on the post-booking page,
    # and that run was classified `uncertain` precisely because neither
    # "SUCCESSFULLY REGISTERED" nor "REGISTRATION COMPLETE" is a substring of it.
    success_keywords = ["SPOT IS SAVED", "YOU ARE IN", "YOU'RE IN",
                        "THANK YOU FOR REGISTERING", "SUCCESSFULLY REGISTERED",
                        "REGISTRATION SUCCESSFUL",
                        "REGISTRATION COMPLETE", "REGISTRATION CONFIRMED",
                        "RESERVATION CONFIRMED", "ADDED TO YOUR RESERVATIONS"]

    # Still sitting on the sign-up form means Finalize never submitted —
    # usually the required Self-Rated Level failing validation.
    still_on_form = "/SIGNUPTOEVENT" in current_url

    if not still_on_form:
        if any(word in page_text for word in success_keywords):
            return {"status": "booked", "time": time_display, "date": target_date_str}

        # After a successful booking CourtReserve redirects to a page where this
        # session's own card now offers Withdraw / Edit Registration. Checked on
        # that one card, never page-wide.
        if _registered_state_for_session(page, card_date_str, session):
            return {"status": "booked", "time": time_display, "date": target_date_str}

        # Landing page didn't say. Rather than guess at its shape, go back to the
        # events list and read the answer off the card itself. CourtReserve
        # bounces to the portal dashboard often enough (2026-06-03, 2026-08-30)
        # that classifying the landing page was never going to be reliable, and
        # this is the same check a human would make.
        if _confirm_on_events_list(page, card_date_str, session, days_diff):
            return {"status": "booked", "time": time_display, "date": target_date_str}

    if second_clicked or finalize_clicked:
        _snapshot_uncertain(page)
        if level_set not in ("set", "not-present"):
            return {"status": "uncertain", "time": time_display, "date": target_date_str,
                    "message": "Registration steps completed, but the Self-Rated Level field couldn't be set automatically. CourtReserve may have blocked Finalize because of this — please check CourtReserve to verify."}
        if still_on_form:
            return {"status": "uncertain", "time": time_display, "date": target_date_str,
                    "message": "The registration form didn't submit, so the session is most likely NOT booked. Please check CourtReserve and try again."}
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
