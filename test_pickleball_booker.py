"""Unit tests for pickleball booker — membership tier logic and browser config."""

import json
import os
import re
import pytest
from unittest.mock import patch, MagicMock

# Import the module functions and constants we need to test.
# _load_env() runs at import time, so we set dummy creds to prevent errors.
# SELF_RATED_LEVEL has to be seeded too: without it the suite only passed on a
# machine whose real .env supplied one, and on a clean checkout the tier tests
# below got "SELF_RATED_LEVEL not set to a valid option" instead of the error
# they assert on.
os.environ.setdefault("COURTRESERVE_EMAIL", "test@example.com")
os.environ.setdefault("COURTRESERVE_PASS", "testpass")
os.environ.setdefault("SELF_RATED_LEVEL", "3.5 to 4.0")

from pickleball_booker import (
    _parse_start_time,
    _is_within_tier_window,
    _tier_window_label,
    _scan_and_book,
    _register_session,
    _card_date_pattern,
    _card_is_free,
    _classify_button,
    _is_open_play_card,
    _extract_session_time,
    _none_available_message,
    _COLLECT_CARDS_JS,
    REGISTERED_BUTTON_TEXTS,
    TIER_RULES,
    VALID_TIERS,
    TierWindow,
    book_pickleball_session,
)

# preferences.json is the source of truth for the tier, and the developer's own
# copy says FULL — so any test that drives the tier through MEMBERSHIP_TYPE has
# to blank the preferences file first or it is silently testing FULL.
# Passed as `new`, so it injects no extra argument into the tests it decorates.
no_prefs = patch("pickleball_booker._read_preferences", lambda: {})


# ── _parse_start_time ─────────────────────────────────────────────────────────

class TestParseStartTime:
    def test_am_time(self):
        assert _parse_start_time("9:00 AM") == (9, 0)

    def test_pm_time(self):
        assert _parse_start_time("3:00 PM") == (15, 0)

    def test_short_am(self):
        assert _parse_start_time("9a") == (9, 0)

    def test_short_pm(self):
        assert _parse_start_time("3p") == (15, 0)

    def test_noon(self):
        assert _parse_start_time("12:00 PM") == (12, 0)

    def test_midnight(self):
        assert _parse_start_time("12:00 AM") == (0, 0)

    def test_no_meridiem(self):
        assert _parse_start_time("14:30") == (14, 30)

    def test_invalid(self):
        assert _parse_start_time("not a time") is None


# ── _is_within_tier_window ────────────────────────────────────────────────────

class TestIsWithinTierWindow:
    # AM tier: 0:00 - 14:30
    def test_am_morning_session(self):
        assert _is_within_tier_window(8, 0, "AM") is True

    def test_am_afternoon_outside(self):
        assert _is_within_tier_window(15, 0, "AM") is False

    def test_am_boundary_inclusive(self):
        assert _is_within_tier_window(14, 30, "AM") is True

    def test_am_just_past_boundary(self):
        assert _is_within_tier_window(14, 31, "AM") is False

    # PM tier: 14:30 - 23:59
    def test_pm_afternoon_session(self):
        assert _is_within_tier_window(15, 0, "PM") is True

    def test_pm_morning_outside(self):
        assert _is_within_tier_window(8, 0, "PM") is False

    def test_pm_boundary_inclusive(self):
        assert _is_within_tier_window(14, 30, "PM") is True

    def test_pm_evening(self):
        assert _is_within_tier_window(21, 0, "PM") is True

    # FULL tier: no restriction
    def test_full_morning(self):
        assert _is_within_tier_window(8, 0, "FULL") is True

    def test_full_evening(self):
        assert _is_within_tier_window(21, 0, "FULL") is True

    def test_full_midnight(self):
        assert _is_within_tier_window(0, 0, "FULL") is True


# ── _tier_window_label ────────────────────────────────────────────────────────

class TestTierWindowLabel:
    def test_am_label(self):
        label = _tier_window_label("AM")
        assert "12:00 AM" in label
        assert "2:30 PM" in label

    def test_pm_label(self):
        label = _tier_window_label("PM")
        assert "2:30 PM" in label
        assert "11:59 PM" in label

    def test_full_label(self):
        assert _tier_window_label("FULL") == "all day"


# ── MEMBERSHIP_TYPE validation ────────────────────────────────────────────────

class TestMembershipValidation:
    """Test that book_pickleball_session validates the tier before launching a browser."""

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "MORNING"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_invalid_tier_returns_error(self, mock_load):
        result = book_pickleball_session(dry_run=True)
        assert result["status"] == "error"
        assert "MORNING" in result["message"]
        assert "invalid" in result["message"].lower()

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "AM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_valid_am_not_rejected(self, mock_load):
        # Give it a date far enough out that it won't launch the browser (>7 days)
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        # Should fail with "more than 7 days out", NOT "invalid tier"
        assert "invalid" not in result.get("message", "").lower()

    @no_prefs
    @patch.dict(os.environ, {}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_missing_tier_is_reported_not_defaulted(self, mock_load):
        """No preferences.json and no MEMBERSHIP_TYPE must ask, not assume AM.

        Silently defaulting to AM made a FULL member's every evening request
        come back "No free Open Play sessions found for your AM membership",
        with nothing to say the tier had been guessed.
        """
        os.environ.pop("MEMBERSHIP_TYPE", None)
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert result["status"] == "error"
        assert "preferences.json" in result["message"]
        assert "invalid" not in result["message"].lower()
        _assert_message_is_user_facing(result["message"])

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": " pm "}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_whitespace_trimmed(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "invalid" not in result.get("message", "").lower()

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "Pm"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_case_insensitive(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "invalid" not in result.get("message", "").lower()


# ── The booker must enforce the tier preferences.json records ────────────────

# The 0.1.0.2 note claimed the tier "lives in preferences.json alone and is
# enforced by the booker". It wasn't: the booker read MEMBERSHIP_TYPE from .env
# and defaulted to AM, so on a checkout without preferences.json (it is
# .gitignore'd) a FULL member silently got the AM window.

class TestTierComesFromPreferences:
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "AM"}, clear=False)
    @patch("pickleball_booker._read_preferences", lambda: {"membership_tier": "FULL"})
    def test_preferences_outrank_the_env_var(self):
        import pickleball_booker as pb
        assert pb._membership_tier() == ("FULL", "preferences.json")

    @no_prefs
    @patch.dict(os.environ, {}, clear=False)
    def test_nothing_configured_has_no_default(self):
        import pickleball_booker as pb
        os.environ.pop("MEMBERSHIP_TYPE", None)
        assert pb._membership_tier() == (None, "")

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL"}, clear=False)
    @patch("pickleball_booker._read_preferences", lambda: {"membership_tier": "PM"})
    @patch("pickleball_booker._load_env")
    def test_the_booker_enforces_the_preferences_tier(self, mock_load):
        """Stops at the pre-scan tier check, so no browser is launched."""
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = book_pickleball_session(dry_run=True, target_time="9:00 AM",
                                         target_date_str=tomorrow)
        assert result["status"] == "error"
        assert "PM membership" in result["message"], \
            "preferences.json is the tier the booker must enforce, not MEMBERSHIP_TYPE"

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "PM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_env_is_the_fallback_when_preferences_are_absent(self, mock_load):
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = book_pickleball_session(dry_run=True, target_time="9:00 AM",
                                         target_date_str=tomorrow)
        assert "outside your tier window" in result["message"]

    @patch("pickleball_booker._read_preferences", lambda: {"membership_tier": "MORNING"})
    @patch("pickleball_booker._load_env")
    def test_invalid_tier_message_names_where_it_came_from(self, mock_load):
        result = book_pickleball_session(dry_run=True)
        assert result["status"] == "error"
        assert "preferences.json" in result["message"]
        _assert_message_is_user_facing(result["message"])


# ── Pre-scan target-time validation ───────────────────────────────────────────

class TestPreScanValidation:
    """Test that target-time / tier conflicts are caught before launching the browser."""

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "PM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_pm_tier_morning_target_error(self, mock_load):
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = book_pickleball_session(dry_run=True, target_time="9:00 AM", target_date_str=tomorrow)
        assert result["status"] == "error"
        assert "outside your tier window" in result["message"]

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "AM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_am_tier_afternoon_target_error(self, mock_load):
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = book_pickleball_session(dry_run=True, target_time="3:00 PM", target_date_str=tomorrow)
        assert result["status"] == "error"
        assert "outside your tier window" in result["message"]

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_full_tier_any_time_passes(self, mock_load):
        # FULL tier + far-out date = should hit "more than 7 days" not "tier window"
        result = book_pickleball_session(dry_run=True, target_time="9:00 AM", target_date_str="2099-01-01")
        assert "outside your tier window" not in result.get("message", "")

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "PM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_no_target_time_skips_validation(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "outside your tier window" not in result.get("message", "")


# ── TIER_RULES structure ──────────────────────────────────────────────────────

class TestTierRulesStructure:
    def test_am_in_tier_rules(self):
        assert "AM" in TIER_RULES
        assert isinstance(TIER_RULES["AM"], TierWindow)

    def test_pm_in_tier_rules(self):
        assert "PM" in TIER_RULES
        assert isinstance(TIER_RULES["PM"], TierWindow)

    def test_full_not_in_tier_rules(self):
        # FULL skips filtering, should not have a TIER_RULES entry
        assert "FULL" not in TIER_RULES

    def test_valid_tiers_set(self):
        assert VALID_TIERS == {"AM", "PM", "FULL"}


# ── Browser anti-detection config ────────────────────────────────────────────

class TestBrowserConfig:
    """Verify that the Playwright launch includes Cloudflare bypass flags."""

    def test_source_has_automation_controlled_flag(self):
        import inspect
        source = inspect.getsource(book_pickleball_session)
        assert "disable-blink-features=AutomationControlled" in source, \
            "Browser launch must include --disable-blink-features=AutomationControlled to bypass Cloudflare"

    def test_source_has_webdriver_override(self):
        import inspect
        source = inspect.getsource(book_pickleball_session)
        assert "navigator" in source and "webdriver" in source, \
            "Must override navigator.webdriver to bypass bot detection"

    def test_source_has_modern_user_agent(self):
        import inspect
        source = inspect.getsource(book_pickleball_session)
        # User agent should be Chrome 120+ (not the old 122 that Cloudflare blocks)
        match = re.search(r"Chrome/(\d+)", source)
        assert match, "Browser launch must include a Chrome user agent"
        version = int(match.group(1))
        assert version >= 120, f"Chrome user agent version {version} is too old, use 120+"


# ── DOM traversal scoping ────────────────────────────────────────────────────

class TestDomTraversalScoping:
    """Verify card collection uses card-level DOM scoping, not an unbounded parent walk."""

    def test_collect_js_has_card_class_check(self):
        assert "className" in _COLLECT_CARDS_JS and "CARD_HINTS" in _COLLECT_CARDS_JS, \
            "Card collection must check CSS class for a card-level container to prevent cross-card text leaking"

    def test_collect_js_does_not_use_unbounded_walk(self):
        """The old bug: walking up DOM until any parent has 'OPEN PLAY' grabs sibling cards.

        Checking for a card-class test is not enough on its own — the walk had
        one and still climbed past it, because a card-level container without
        "OPEN PLAY" fell through to the next iteration instead of ending the
        search. The bound that actually stops it is the action-button count: an
        ancestor holding a second Register/Withdraw button is a multi-card
        wrapper, so the climb has to end there.
        """
        assert "OPEN PLAY" in _COLLECT_CARDS_JS, \
            "Card collection must still require OPEN PLAY in the card it settles on"
        assert "isCard" in _COLLECT_CARDS_JS, \
            "DOM traversal must check element class to stop at the card boundary"
        assert "actionsInside" in _COLLECT_CARDS_JS, \
            "DOM traversal must count action buttons to detect a multi-card wrapper"
        assert re.search(r"if \(actionsInside\(node\) > 1\) return;", _COLLECT_CARDS_JS), \
            "the climb must abandon a button once it reaches a container holding a second one"
        assert re.search(r"depth < \d+", _COLLECT_CARDS_JS), \
            "the climb must be depth-bounded"

    def test_scan_makes_one_round_trip_per_page(self):
        """Card text + button label used to cost two CDP round trips per card.

        Asserted on the card loop itself. The previous form checked the whole
        function for the literal "inner_text()" — which never appears anywhere,
        with or without the bug — so it could not fail.
        """
        import inspect
        source = inspect.getsource(_scan_and_book)
        assert "_collect_session_cards(page)" in source, \
            "_scan_and_book must batch card collection into one round trip"
        loop_body = source.split("for card_index,", 1)[1]
        assert "page." not in loop_body, \
            "the per-card loop must not touch the browser; use the collected card text"


# ── Beginner Open Play exclusion ─────────────────────────────────────────────

# Root cause of the 2026-07-31 mis-booking: "Beginner Open Play" cards also
# contain the substring "OPEN PLAY", so they satisfied the card filter and
# competed on time-proximity like any regular Open Play card. Requested a
# 9:00 AM regular session; the Beginner 9:00 AM card was closer to the
# target time than the regular one (which started at 9:15 that day), so it
# won the sort and got booked instead.

def _make_page(cards, page_body=None):
    """Mock page whose _collect_session_cards() yields `cards`.

    `cards` is a list of (card_text, button_label) exactly as the browser-side
    collector returns them.
    """
    page = MagicMock()
    page.inner_text = MagicMock(
        return_value=page_body if page_body is not None else "\n".join(c[0] for c in cards)
    )
    page.evaluate = MagicMock(return_value=[list(c) for c in cards])
    return page


def _card(text, label="REGISTER"):
    return (text, label)


class TestBeginnerSessionExclusion:
    def test_beginner_card_not_booked_over_regular(self):
        page = _make_page(
            [
                _card(
                    "Beginner Open Play\n"
                    "Beginner Open Play/Skills 9AM-12PM\n"
                    "Fri, Jul 31st\n"
                    "9a - 12p\n"
                    "FREE"
                ),
                _card(
                    "Open Play\n"
                    "Open Play/Challenge Courts 9:15AM-12PM\n"
                    "Fri, Jul 31st\n"
                    "9:15a - 12p\n"
                    "FREE"
                ),
            ],
            page_body="Sessions for Fri, Jul 31st are listed below.",
        )

        result = _scan_and_book(
            page,
            target_date_str="Friday, July 31, 2026",
            card_date_str="Jul 31",
            dry_run=True,
            target_h=9, target_m=0,
            tier="FULL",
        )

        assert result["status"] == "dry_run"
        times = [s["time"] for s in result["sessions"]]
        assert len(times) == 1, (
            f"Beginner Open Play card should be excluded from qualifying sessions, got {times}"
        )
        assert "15" in times[0], f"Expected the regular 9:15 AM session to survive, got {times}"

    def test_source_excludes_beginner_cards(self):
        import inspect
        source = inspect.getsource(_scan_and_book)
        assert "BEGINNER" in source.upper(), \
            "_scan_and_book must filter out Beginner Open Play cards, not just any 'OPEN PLAY' match"


# ── User-facing message hygiene ──────────────────────────────────────────────

# The agent (especially small local models like gemma) treats the `message`
# field in the script's stdout JSON as user-facing text and may relay it
# verbatim to WhatsApp. So `message` must never contain Python exception
# class names, "Error:" prefixes, file paths, or other technical leakage.
# Internal diagnostic detail goes to stderr via _log_internal().

_TECHNICAL_LEAKAGE_PATTERNS = [
    "Traceback",
    "Exception",
    "TimeoutError",
    "PlaywrightTimeout",
    "AttributeError",
    "TypeError",
    "Error:",
    "Step 1",
    "JS Execution",
    "Login Error",
    "Date filter error",
    "Unexpected error",
    "/Users/",
    "<class ",
]


def _assert_message_is_user_facing(message: str):
    for pat in _TECHNICAL_LEAKAGE_PATTERNS:
        assert pat not in message, \
            f"User-facing message leaked technical detail '{pat}': {message!r}"


class TestErrorMessageHygiene:
    """Each error path must return a clean, user-facing message — no tracebacks,
    no exception class names, no file paths, no 'Error:' prefixes. The agent
    relays this verbatim, so leaks land in the user's WhatsApp."""

    @patch.dict(os.environ, {"COURTRESERVE_EMAIL": "", "COURTRESERVE_PASS": ""}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_missing_credentials_message_is_user_facing(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert result["status"] == "error"
        _assert_message_is_user_facing(result["message"])

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "MORNING"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_invalid_tier_message_is_user_facing(self, mock_load):
        result = book_pickleball_session(dry_run=True)
        assert result["status"] == "error"
        _assert_message_is_user_facing(result["message"])

    @patch("pickleball_booker._load_env")
    def test_invalid_date_format_message_is_user_facing(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="not-a-date")
        assert result["status"] == "error"
        _assert_message_is_user_facing(result["message"])

    @patch("pickleball_booker._load_env")
    def test_past_date_message_is_user_facing(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2000-01-01")
        assert result["status"] == "error"
        _assert_message_is_user_facing(result["message"])

    @patch("pickleball_booker._load_env")
    def test_far_out_date_message_is_user_facing(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert result["status"] == "error"
        _assert_message_is_user_facing(result["message"])

    def test_source_has_no_raw_exception_in_user_message(self):
        """Defensive source-level check: no return path should embed `str(e)`
        into the user-facing `message` field. Internal detail goes to stderr
        via _log_internal()."""
        import inspect
        import pickleball_booker as pb
        # Check message strings inside book_pickleball_session,
        # _scan_and_book, and _register_session
        for fn in (pb.book_pickleball_session, pb._scan_and_book, pb._register_session):
            source = inspect.getsource(fn)
            # Pattern: "message": f"...{str(e)..."  or "message": f"...{e}..."
            offenders = re.findall(r'"message":\s*f"[^"]*\{(?:str\()?e[^"]*"', source)
            assert offenders == [], \
                f"{fn.__name__} embeds raw exception text in user-facing message: {offenders}"


class TestMainCrashSafety:
    """The CLI entry point must never let a Python traceback escape to stdout.
    Any uncaught exception inside book_pickleball_session must be converted
    to a user-facing JSON error on stdout, with the traceback only on stderr."""

    def test_main_returns_clean_json_on_uncaught_exception(self, monkeypatch, capsys):
        import pickleball_booker as pb

        def boom(*args, **kwargs):
            raise RuntimeError("simulated playwright/network/anything failure")

        monkeypatch.setattr(pb, "book_pickleball_session", boom)

        rv = pb.main(["--date", "2099-01-01"])
        captured = capsys.readouterr()

        assert rv == 1
        # stdout must be exactly one line of JSON, parseable, status=error
        stdout_lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(stdout_lines) == 1, f"expected single JSON line, got {len(stdout_lines)}: {captured.out!r}"
        parsed = json.loads(stdout_lines[0])
        assert parsed["status"] == "error"
        _assert_message_is_user_facing(parsed["message"])

        # stderr should carry the traceback for post-mortem
        assert "Traceback" in captured.err
        assert "RuntimeError" in captured.err
        assert "simulated" in captured.err

    def test_main_normal_path_returns_zero(self, monkeypatch, capsys):
        import pickleball_booker as pb

        def fake(*args, **kwargs):
            return {"status": "dry_run", "sessions": [{"time": "9a-12p"}], "date": "Mon, Apr 6, 2026"}

        monkeypatch.setattr(pb, "book_pickleball_session", fake)
        rv = pb.main(["--dry-run", "--date", "2026-04-06"])
        captured = capsys.readouterr()

        assert rv == 0
        stdout_lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(stdout_lines) == 1
        parsed = json.loads(stdout_lines[0])
        assert parsed["status"] == "dry_run"


class TestStdoutContract:
    """The stdout contract: a single line of JSON, terminated by a newline.
    The agent's invocation pattern parses stdout — multi-line or malformed
    output breaks the contract."""

    def test_main_emits_single_line_no_indent(self, monkeypatch, capsys):
        import pickleball_booker as pb

        result = {"status": "booked", "time": "9a-12p", "date": "Mon, Apr 6, 2026"}
        monkeypatch.setattr(pb, "book_pickleball_session", lambda *a, **kw: result)

        pb.main(["--date", "2026-04-06"])
        out = capsys.readouterr().out

        # Exactly one newline at end, exactly one line of content, no \n inside
        assert out.endswith("\n")
        assert out.count("\n") == 1, f"expected single-line output, got: {out!r}"
        parsed = json.loads(out)
        assert parsed == result


# ── Regression: confirmation detection (0.1.0.2) ─────────────────────────────

# The old check matched success keywords as bare substrings against the whole
# page, and the list carried "WITHDRAW", "REGISTERED" and "COMPLETE". Since the
# events list shows a Withdraw button for *every* session the member already
# holds, a booking that silently failed and bounced back reported `booked`.
# So did a dashboard reading "Complete your profile".

_OUR_CARD = "Open Play\nOpen Play/Challenge Courts 9AM-12PM\nSat, Aug 1st\n9a - 12p\nFREE"
_OTHER_CARD = "Open Play\nOpen Play/Challenge Courts 5PM-8PM\nSat, Aug 1st\n5p - 8p\nFREE"

_EVENTS_LIST_URL = "https://app.courtreserve.com/Online/Events/List/13464"
_SIGNUP_URL = "https://app.courtreserve.com/Online/Events/SignUpToEvent/13464?eventId=1985534"


def _make_register_page(body, url, cards=(), second=False, finalize=True, level="set"):
    page = MagicMock()
    page.url = url
    page.inner_text = MagicMock(return_value=body)
    page.content = MagicMock(return_value="<html></html>")
    page.screenshot = MagicMock(return_value=b"\x89PNG fake")

    def _ev(js, *args, **kwargs):
        if "data-pb-idx" in js:
            return [list(c) for c in cards]
        if "kendoDropDownList" in js:
            return level
        if "isFinalizeButton" in js:
            return second
        if "FINALIZE" in js:
            return finalize
        return None

    page.evaluate = MagicMock(side_effect=_ev)

    # The events-list reconfirmation opens its own tab. By default it finds
    # nothing, so tests that don't care about it are unaffected.
    probe = MagicMock()
    probe.evaluate = MagicMock(return_value=[])
    probe.inner_text = MagicMock(return_value="")
    page.context.new_page = MagicMock(return_value=probe)
    page.reconfirm_probe = probe
    return page


_SESSION = {"time_str": "9a-12p", "start_h": 9, "start_m": 0, "card_index": 0}


def _attempt(page, tmp_path):
    import pickleball_booker as pb
    with patch.object(pb, "SKILL_DIR", tmp_path):
        return _register_session(page, dict(_SESSION), "Sat, Aug 1, 2026", "Aug 1")


class TestConfirmationDetection:
    def test_unrelated_withdraw_button_is_not_success(self, tmp_path):
        """A different session the member already holds must not confirm this booking."""
        page = _make_register_page(
            body=f"Events\n{_OUR_CARD}\nRegister\n{_OTHER_CARD}\nWithdraw\n",
            url=_EVENTS_LIST_URL,
            cards=[(_OUR_CARD, "REGISTER"), (_OTHER_CARD, "WITHDRAW")],
        )
        result = _attempt(page, tmp_path)
        assert result["status"] != "booked", \
            "A Withdraw button on an unrelated session must not report this booking as booked"
        assert result["status"] == "uncertain"

    def test_withdraw_on_our_own_card_is_success(self, tmp_path):
        """Post-redirect confirmation still works when it's *our* card that changed."""
        page = _make_register_page(
            body=f"Events\n{_OUR_CARD}\nWithdraw\n",
            url=_EVENTS_LIST_URL,
            cards=[(_OUR_CARD, "WITHDRAW")],
        )
        result = _attempt(page, tmp_path)
        assert result["status"] == "booked"
        assert result["time"] == "9a-12p"

    def test_complete_your_profile_nag_is_not_success(self, tmp_path):
        page = _make_register_page(
            body="Welcome back\nComplete your profile to get the most out of your membership.\nBook a Court\n",
            url="https://app.courtreserve.com/Online/Portal/Index/13464?forceDashboard=True",
            cards=[],
        )
        assert _attempt(page, tmp_path)["status"] != "booked"

    def test_still_on_signup_form_is_never_booked(self, tmp_path):
        """The three captured debug/uncertain_* snapshots all look like this."""
        page = _make_register_page(
            body="Register to Event\nOpen Play\nSat, Aug 1st\n9a - 12p\nFREE\nBackFinalize Registration\n",
            url=_SIGNUP_URL,
            cards=[],
        )
        result = _attempt(page, tmp_path)
        assert result["status"] == "uncertain"
        assert "NOT booked" in result["message"]
        _assert_message_is_user_facing(result["message"])

    def test_explicit_confirmation_copy_is_success(self, tmp_path):
        page = _make_register_page(
            body="Registration Complete\nYour spot is saved.\n",
            url=_EVENTS_LIST_URL,
            cards=[],
        )
        assert _attempt(page, tmp_path)["status"] == "booked"

    def test_generic_words_are_not_in_the_keyword_list(self):
        import inspect
        source = inspect.getsource(_register_session)
        keywords = re.search(r"success_keywords = \[(.*?)\]", source, re.DOTALL).group(1)
        for generic in ['"COMPLETE"', '"WITHDRAW"', '"REGISTERED"', '"SUCCESS"', '"CONFIRMED"', '"THANK YOU"']:
            assert generic not in keywords, \
                f"{generic} is too generic to confirm a booking from whole-page text"


# ── Regression: Self-Rated Level must be verified, not assumed (0.1.0.2) ─────

class TestSelfRatedLevelVerification:
    def test_setter_reads_the_value_back(self):
        import inspect
        source = inspect.getsource(_register_session)
        assert 'return "set";' not in source, \
            "The Kendo setter must not report success unconditionally"
        assert "widget.value() === level" in source, \
            "The Kendo setter must read the value back before claiming it was set"

    def test_rejected_level_produces_a_clean_specific_message(self, tmp_path):
        page = _make_register_page(
            body="Register to Event\nSelf-Rated Level\nBackFinalize Registration\n",
            url=_SIGNUP_URL,
            cards=[],
            level="value-rejected:empty",
        )
        result = _attempt(page, tmp_path)
        assert result["status"] == "uncertain"
        assert "Self-Rated Level" in result["message"]
        # The internal reason code must stay on stderr, not reach the user.
        assert "value-rejected" not in result["message"]
        _assert_message_is_user_facing(result["message"])


# ── Regression: card date matching must respect day boundaries (0.1.0.2) ────

class TestCardDateMatching:
    def test_single_digit_day_does_not_match_double_digit(self):
        assert not _card_date_pattern("Aug 1").search("Wed, Aug 12th, 9a - 12p")
        assert not _card_date_pattern("Aug 1").search("Aug 10th")
        assert not _card_date_pattern("Sep 2").search("Sep 20th")

    def test_matches_its_own_day_with_and_without_ordinal(self):
        assert _card_date_pattern("Aug 1").search("Sat, Aug 1st, 9a - 12p")
        assert _card_date_pattern("Aug 1").search("Aug 1, 2026")
        assert _card_date_pattern("Aug 12").search("Wed, Aug 12th")

    def test_scan_rejects_a_card_from_the_wrong_day(self):
        page = _make_page(
            [_card("Open Play\nChallenge Courts\nWed, Aug 12th\n9a - 12p\nFREE")],
            page_body="Wed, Aug 12th",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1", dry_run=True, tier="FULL")
        assert result["status"] == "none_available", \
            "A card for Aug 12 must not satisfy a booking for Aug 1"


# ── Regression: fee detection (0.1.0.2) ─────────────────────────────────────

class TestFeeDetection:
    @pytest.mark.parametrize("price_line", [
        "$12.00 per player",
        "$12 (Member)",
        "Fee: $12.00",
        "$12.00",
        "$5",
    ])
    def test_priced_card_is_never_free(self, price_line):
        assert not _card_is_free(f"Open Play\nSat, Aug 1st\n9a - 12p\n{price_line}")

    @pytest.mark.parametrize("card_text", [
        "Open Play\nSat, Aug 1st\n9a - 12p\nFREE",
        "Open Play\nSat, Aug 1st\n9a - 12p\nFREE\n16",
        "Open Play\nSat, Aug 1st\n9a - 12p\nFree for members\n16",
        "Open Play\nSat, Aug 1st\n9a - 12p\n$0.00",
    ])
    def test_free_card_is_free(self, card_text):
        assert _card_is_free(card_text)

    def test_price_field_wins_over_a_dollar_amount_in_the_title(self):
        """Live 2026-08-31 card: $10 in the title, FREE in CourtReserve's price
        field. The $10 is the non-member rate; the price field is rendered for
        the logged-in account and the membership covers this session."""
        card = (
            "Open Play\n"
            "$10 HAPPY HOUR OPEN PLAY (12-2:30 PM)\n"
            "Mon, Aug 31st, 12p - 2:30p\n"
            "FREE\n"
            "50 of 50 spots remaining"
        )
        assert _card_is_free(card), \
            "an explicit FREE price field must outrank a dollar amount in the event title"

    def test_scan_skips_a_priced_session(self):
        page = _make_page(
            [_card("Open Play\nChallenge Courts\nSat, Aug 1st\n9a - 12p\n$12.00 per player")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1", dry_run=True, tier="FULL")
        assert result["status"] == "none_available", "A paid session must never be offered"

    def test_scan_keeps_a_free_session_with_a_trailing_count(self):
        page = _make_page(
            [_card("Open Play\nChallenge Courts\nSat, Aug 1st\n9a - 12p\nFREE\n16")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1", dry_run=True, tier="FULL")
        assert result["status"] == "dry_run"


# ── Regression: the documented status contract must cover every status ──────

# SKILL.md drives the agent's reply behaviour. A status the code can return but
# SKILL.md doesn't list is one the agent has no instructions for — its rule is
# "anything else is not a terminal status, poll again", which is the silent
# no-reply failure mode.

class TestStatusContract:
    def _skill_md(self):
        import pathlib
        return pathlib.Path(__file__).parent.joinpath("SKILL.md").read_text()

    def _statuses_in_code(self):
        import pathlib
        source = pathlib.Path(__file__).parent.joinpath("pickleball_booker.py").read_text()
        return set(re.findall(r'"status":\s*"([a-z_]+)"', source))

    def test_every_status_is_documented(self):
        skill = self._skill_md()
        documented = set(re.findall(r"^- `([a-z_]+)`", skill, re.MULTILINE))
        missing = self._statuses_in_code() - documented
        assert not missing, f"statuses returned by the booker but absent from SKILL.md: {sorted(missing)}"

    def test_every_status_is_listed_as_terminal(self):
        skill = self._skill_md()
        sentence = re.search(r"Terminal statuses returned by the booker:([^.]*)\.", skill).group(1)
        listed = set(re.findall(r"`([a-z_]+)`", sentence))
        missing = self._statuses_in_code() - listed
        assert not missing, (
            "statuses the agent is not told are terminal (it will poll forever "
            f"instead of replying): {sorted(missing)}"
        )


# ── Regression: "OPEN PLAY" substring must not match a negated/other event ──

# Seen live on 2026-08-30 — the only card on the page that day:
_HAVEN_CUP_CARD = (
    "Special Events\n"
    "Haven Cup *Tournament Players & Spectators* - NO OPEN PLAY "
    "(Does NOT include Anniversary Party)\n"
    "Sun, Aug 30th, 7a - 10p\n"
    "FREE\n"
    "Special Event\n"
    "FEATURED"
)


class TestOpenPlayCardFilter:
    def test_no_open_play_card_is_rejected(self):
        assert not _is_open_play_card(_HAVEN_CUP_CARD)

    def test_beginner_card_is_rejected(self):
        assert not _is_open_play_card("Beginner Open Play/Skills 9AM-12PM\nFri, Jul 31st\n9a - 12p\nFREE")

    def test_regular_open_play_card_is_accepted(self):
        assert _is_open_play_card("Open Play\nOpen Play/Challenge Courts 9AM-12PM\nSat, Aug 1st\n9a - 12p\nFREE")

    def test_card_without_open_play_is_rejected(self):
        assert not _is_open_play_card("Cardio Clinic\nSat, Aug 1st\n9a - 10p\nFREE")

    def test_scan_offers_nothing_when_only_a_tournament_card_is_listed(self):
        """The live 2026-08-30 page. A booking run must not register for this."""
        page = _make_page([_card(_HAVEN_CUP_CARD)], page_body="Sun, Aug 30th")
        result = _scan_and_book(page, "Sunday, August 30, 2026", "Aug 30", dry_run=True, tier="FULL")
        assert result["status"] == "none_available", \
            f"tournament/spectator card must not be offered as Open Play, got {result}"


# ── Regression: an unreadable --target-time must not silently pick a slot ────

# Falling through with target_h=None switched the proximity filter off and
# booked the earliest session of the day, so an agent that passed "evening"
# or "now" (it builds this string from natural language) booked 7 AM.

class TestTargetTimeValidation:
    @pytest.mark.parametrize("bad_time", ["evening", "tonight", "now", "morning", "soon"])
    @patch("pickleball_booker._load_env")
    def test_unreadable_target_time_is_an_error(self, mock_load, bad_time):
        with patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL", "SELF_RATED_LEVEL": "3.5 to 4.0"}):
            result = book_pickleball_session(dry_run=True, target_time=bad_time)
        assert result["status"] == "error", f"{bad_time!r} must not fall through to the earliest slot"
        assert bad_time in result["message"]
        _assert_message_is_user_facing(result["message"])

    @pytest.mark.parametrize("good_time", ["9:00 AM", "7:30 PM", "12p", "9a"])
    @patch("pickleball_booker._load_env")
    def test_readable_target_time_passes_validation(self, mock_load, good_time):
        """Must fail later (no browser in tests), not at the time check."""
        with patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL", "SELF_RATED_LEVEL": "3.5 to 4.0"}):
            result = book_pickleball_session(dry_run=True, target_time=good_time,
                                             target_date_str="2000-01-01")
        assert "Couldn't read a time" not in result.get("message", "")

    @pytest.mark.parametrize("out_of_range", ["25:00", "13 PM", "9:75"])
    def test_out_of_range_clock_values_are_rejected(self, out_of_range):
        assert _parse_start_time(out_of_range) is None


# ── Regression: structured date line outranks the human-written title ───────

class TestStructuredTimeWins:
    _CARD = (
        "Open Play\n"
        "Open Play/Challenge Courts 9AM - 12PM\n"   # title, rounded
        "Mon, Aug 31st, 9:15a - 12p\n"              # CourtReserve's own line
        "FREE"
    )

    def test_date_line_wins_when_anchored(self):
        (h, m), _ = _extract_session_time(self._CARD, _card_date_pattern("Aug 31"))
        assert (h, m) == (9, 15), "the range printed after the date is authoritative"

    def test_title_is_the_fallback_without_an_anchor(self):
        (h, m), _ = _extract_session_time(self._CARD)
        assert (h, m) == (9, 0)

    def test_happy_hour_title_does_not_override_the_date_line(self):
        card = (
            "Open Play\n"
            "$10 HAPPY HOUR OPEN PLAY (12-2:30 PM)\n"
            "Mon, Aug 31st, 12p - 2:30p\n"
            "FREE"
        )
        (h, m), display = _extract_session_time(card, _card_date_pattern("Aug 31"))
        assert (h, m) == (12, 0)
        assert display == "12p\u20132:30p"

    def test_scan_uses_the_structured_time_for_tier_and_proximity(self):
        """A 9:15 session must not be treated as 9:00 when sorting by target time."""
        page = _make_page([_card(self._CARD)], page_body="Mon, Aug 31st")
        result = _scan_and_book(page, "Monday, August 31, 2026", "Aug 31",
                                dry_run=True, tier="FULL")
        assert result["sessions"][0]["time"] == "9:15a\u201312p"


# ── Regression: the collector must match "Edit Registration" (0.1.0.2) ──────

# Found by running a real booking on 2026-08-30. "REGISTER" is not a substring
# of "REGISTRATION" — REGIST-R-ATION has no second E — so a label filter built
# on includes("REGISTER") silently dropped every already-registered card. That
# broke `already_booked` outright and made every successful booking report
# `uncertain`, because the post-booking check could never find its own card.

class TestCollectorLabelFilter:
    def test_the_substring_trap_itself(self):
        assert "REGISTER" not in "EDIT REGISTRATION"
        assert "REGISTER" not in "CANCEL REGISTRATION"

    def test_collector_regex_matches_every_label_we_classify(self):
        found = re.search(r"ACTION_RE = /([^/]+)/", _COLLECT_CARDS_JS)
        assert found, "expected a regex label filter in the card collector"
        pattern = re.compile(found.group(1))
        for label in ("REGISTER",) + tuple(REGISTERED_BUTTON_TEXTS):
            assert pattern.search(label), \
                f"collector would drop {label!r} — already-booked cards go invisible"
        assert "if (!ACTION_RE.test(label)) return;" in _COLLECT_CARDS_JS, \
            "the collector must actually apply ACTION_RE to the button label"

    def test_scan_flags_an_already_registered_card(self):
        """Live shape on 2026-08-31 after booking: the button becomes Edit Registration."""
        page = _make_page(
            [_card("Open Play\nOpen Play/Challenge Courts 7AM - 9AM\nMon, Aug 31st, 7a - 9a\n"
                   "FREE\n48 of 50 spots remaining", "EDIT REGISTRATION")],
            page_body="Mon, Aug 31st",
        )
        result = _scan_and_book(page, "Monday, August 31, 2026", "Aug 31", dry_run=True, tier="FULL")
        assert result["sessions"][0]["already_booked"] is True


# ── Regression: an absent Self-Rated Level field is not a failure ───────────

class TestLevelFieldAbsence:
    def test_not_present_does_not_blame_the_level_field(self, tmp_path):
        """Most Open Play events have no Self-Rated Level field at all."""
        page = _make_register_page(
            body="Welcome to Your Pickleball Haven\nBook a Court\n",
            url="https://app.courtreserve.com/Online/Portal/Index/13464?forceDashboard=True",
            cards=[],
            level="not-present",
        )
        result = _attempt(page, tmp_path)
        assert "Self-Rated Level" not in result["message"], \
            "a field the event never had must not be reported as the cause"


# ── Regression: the reconfirmation must not clobber the post-mortem ────────

# The events-list reconfirmation used to navigate `page` itself, so the
# snapshot taken afterwards captured the events list instead of the page we
# actually failed to classify — the one piece of evidence every past diagnosis
# has relied on. It now runs in a second tab.

class TestSnapshotOrdering:
    def test_snapshot_captures_the_landing_page_not_the_events_list(self, tmp_path):
        dashboard = "https://app.courtreserve.com/Online/Portal/Index/13464?forceDashboard=True"
        page = _make_register_page(body="Welcome to Your Pickleball Haven\n", url=dashboard, cards=[])
        _attempt(page, tmp_path)

        snaps = list((tmp_path / "debug").glob("uncertain_*"))
        assert len(snaps) == 1, f"expected exactly one snapshot, got {snaps}"
        captured = (snaps[0] / "url.txt").read_text()
        assert "forceDashboard" in captured, \
            f"snapshot must preserve the inconclusive landing page, captured {captured!r}"
        assert "/Events/List/" not in captured

    def test_reconfirmation_uses_a_separate_tab(self, tmp_path):
        page = _make_register_page(body="dashboard", url="https://x/Portal/Index", cards=[])
        _attempt(page, tmp_path)
        page.context.new_page.assert_called()
        page.goto.assert_not_called()
        page.reconfirm_probe.close.assert_called()

    def test_only_one_snapshot_per_attempt(self, tmp_path):
        page = _make_register_page(
            body="Register to Event\nBackFinalize Registration\n",
            url="https://app.courtreserve.com/Online/Events/SignUpToEvent/13464?eventId=1",
            cards=[],
        )
        _attempt(page, tmp_path)
        assert len(list((tmp_path / "debug").glob("uncertain_*"))) == 1


# ── Regression: a successful booking must not leave a post-mortem behind ────

# CourtReserve bounces to the portal dashboard on success as well as failure,
# so the landing page has to be read before the reconfirmation navigates away.
# Writing it at capture time filed a full-page PNG under debug/uncertain_* on
# every successful booking.

class TestSnapshotOnlyOnFailure:
    def test_successful_booking_writes_no_snapshot(self, tmp_path):
        dashboard = "https://app.courtreserve.com/Online/Portal/Index/13464?forceDashboard=True"
        our_card = "Open Play\nOpen Play/Challenge Courts 7AM - 9AM\nMon, Aug 31st, 7a - 9a\nFREE"
        page = _make_register_page(body="Welcome to Your Pickleball Haven\n", url=dashboard, cards=[])

        page.reconfirm_probe.evaluate = MagicMock(return_value=[[our_card, "EDIT REGISTRATION"]])
        session = {"time_str": "7a-9a", "start_h": 7, "start_m": 0, "card_index": 0}
        import pickleball_booker as pb
        with patch.object(pb, "SKILL_DIR", tmp_path):
            result = _register_session(page, session, "Mon, Aug 31, 2026", "Aug 31")

        assert result["status"] == "booked"
        assert not list((tmp_path / "debug").glob("uncertain_*")), \
            "a confirmed booking must not leave an 'uncertain' post-mortem on disk"


# ── Regression: button state is classified by word, not by equality (0.1.0.3) ─

# The 0.1.0.2 fix broadened the collector to the substring /REGIST|WITHDRAW/ but
# left classification on exact equality against a raw innerText. Two bugs fell
# out of the gap: labels that merely look bookable ("Registration Closed") were
# treated as available, and any glyph or line break inside a real "Edit
# Registration" button made an already-held session look unbooked.

class TestButtonClassification:
    @pytest.mark.parametrize("label", [
        "Registration Closed", "REGISTRATION FULL", "Waitlist Registration",
        "View Registrations", "Registration Sold Out",
    ])
    def test_non_bookable_registration_states(self, label):
        assert _classify_button(label) == "unavailable", \
            f"{label!r} passes the collector's REGIST filter but cannot be booked"

    @pytest.mark.parametrize("label", [
        "Edit Registration", "EDIT REGISTRATION", " EDIT REGISTRATION",
        "EDIT\nREGISTRATION", "Edit Registration", " Withdraw ",
        "Cancel Registration",
    ])
    def test_registered_states_survive_decoration(self, label):
        assert _classify_button(label) == "registered", \
            f"{label!r} must be recognised as a session already held"

    @pytest.mark.parametrize("label", ["Register", "REGISTER", "Register Now", " Register"])
    def test_bookable_states(self, label):
        assert _classify_button(label) == "bookable"

    def test_every_registered_button_text_classifies_as_registered(self):
        for label in REGISTERED_BUTTON_TEXTS:
            assert _classify_button(label) == "registered"

    def test_scan_skips_a_closed_card_and_offers_the_next(self):
        page = _make_page(
            [
                _card("Open Play\nSat, Aug 1st, 9a - 12p\nFREE", "Registration Closed"),
                _card("Open Play\nSat, Aug 1st, 9:15a - 12p\nFREE", "REGISTER"),
            ],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, target_h=9, target_m=0, tier="FULL")
        times = [s["time"] for s in result["sessions"]]
        assert times == ["9:15a–12p"], \
            f"a closed card must not win the sort and block the bookable one, got {times}"

    def test_scan_sees_a_glyph_decorated_edit_registration(self):
        page = _make_page(
            [_card("Open Play\nMon, Aug 31st, 7a - 9a\nFREE", " EDIT\nREGISTRATION")],
            page_body="Mon, Aug 31st",
        )
        result = _scan_and_book(page, "Monday, August 31, 2026", "Aug 31",
                                dry_run=True, tier="FULL")
        assert result["sessions"][0]["already_booked"] is True


# ── Regression: asking for a slot already held must not book a second one ────

# `already_booked` only fired when EVERY qualifying session was held, so a 9:00
# request the owner already had fell through to the next unbooked card and
# registered an overlapping 9:15 session as well.

class TestAlreadyBookedClosest:
    def test_requested_slot_already_held_does_not_book_the_neighbour(self):
        page = _make_page(
            [
                _card("Open Play\nSat, Aug 1st, 9a - 12p\nFREE", "EDIT REGISTRATION"),
                _card("Open Play\nSat, Aug 1st, 9:15a - 12p\nFREE", "REGISTER"),
            ],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                target_h=9, target_m=0, tier="FULL")
        assert result["status"] == "already_booked", \
            "the 9:00 the owner already holds must not fall through to a second 9:15 booking"
        assert result["time"] == "9a–12p"

    def test_earliest_slot_already_held_does_not_book_a_later_one(self):
        """With no --target-time the sort is by start time, so the 7 AM the
        owner holds is the requested session."""
        page = _make_page(
            [
                _card("Open Play\nSat, Aug 1st, 7a - 9a\nFREE", "WITHDRAW"),
                _card("Open Play\nSat, Aug 1st, 9a - 12p\nFREE", "REGISTER"),
            ],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1", tier="FULL")
        assert result["status"] == "already_booked"
        assert result["time"] == "7a–9a"


# ── Regression: the price field is not always on its own line (0.1.0.3) ──────

# debug/uncertain_2026-08-30T18-42-03 line 3 is "Sat, Aug 1st  9a - 12p  FREE" —
# date, range and price in one block. The `== "FREE"` whole-line test read that
# as paid and dropped a free, bookable session.

class TestInlineFreePriceField:
    def test_inline_free_is_free(self):
        assert _card_is_free("Register to Event\nOpen Play\nSat, Aug 1st  9a - 12p  FREE")

    def test_inline_free_does_not_beat_a_title_price(self):
        """The deliberately conservative corner.

        Two card shapes are backed by captures: own-line FREE alongside a title
        price (the happy-hour card), and inline FREE on a card with no price
        anywhere (uncertain_2026-08-30T18-42-03). This hybrid — inline FREE AND
        a dollar amount — has never been observed. Treating it as free is what
        makes "$12.00 per player - includes FREE paddle rental" bookable, so it
        resolves to paid: miss a session rather than charge the owner for one.
        Revisit if a real card ever turns up in this shape.
        """
        assert not _card_is_free("$10 HAPPY HOUR OPEN PLAY (12-2:30 PM)\nMon, Aug 31st  12p - 2:30p  FREE")

    @pytest.mark.parametrize("card", [
        "Open Play\nSat, Aug 1st, 9a - 12p\n$12.00 per player - includes FREE paddle rental",
        "Open Play\nSat, Aug 1st, 9a - 12p\n$15.00\nFREE PARKING",
        "Open Play\nSat, Aug 1st, 9a - 12p\n$12 (Member)\nFree clinic for first-timers",
    ])
    def test_the_word_free_in_prose_never_beats_a_real_price(self, card):
        """Caught by the adversarial pass: matching FREE anywhere unconditionally
        books a paid session, which is the 0.1.0.2 bug all over again. Own-line
        FREE outranks a title price; FREE loose in a sentence does not."""
        assert not _card_is_free(card)

    @pytest.mark.parametrize("line", ["FREE", "Free!", "(FREE)", "  free  "])
    def test_a_standalone_price_field_still_wins(self, line):
        assert _card_is_free(f"$10 HAPPY HOUR OPEN PLAY\nMon, Aug 31st, 12p - 2:30p\n{line}\n50 spots")

    def test_a_card_with_no_price_information_is_not_offered(self):
        """Fail-closed: booking a session that turns out to cost money is worse
        than missing one, and _none_available_message now names the fee check."""
        assert not _card_is_free("Open Play\nSat, Aug 1st\n9a - 12p\n16 spots left")

    def test_scan_keeps_an_inline_free_session(self):
        page = _make_page(
            [_card("Open Play\nChallenge Courts\nSat, Aug 1st  9a - 12p  FREE")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, tier="FULL")
        assert result["status"] == "dry_run", \
            "an inline FREE price field must not read as paid"


# ── Regression: CourtReserve's actual confirmation copy (0.1.0.3) ────────────

class TestRegistrationSuccessfulCopy:
    def test_registration_successful_confirms_the_booking(self, tmp_path):
        """debug/uncertain_2026-08-30T19-54-46 line 72, on a run classified
        `uncertain` because no keyword in the list is a substring of it."""
        page = _make_register_page(
            body="Your Pickleball Haven\n7:00 AM - 10:00 PM\nRegistration successful.\n",
            url="https://app.courtreserve.com/Online/Portal/Index/13464?forceDashboard=True",
            cards=[],
        )
        assert _attempt(page, tmp_path)["status"] == "booked"

    def test_the_captured_phrase_is_in_the_keyword_list(self):
        import inspect
        source = inspect.getsource(_register_session)
        keywords = re.search(r"success_keywords = \[(.*?)\]", source, re.DOTALL).group(1)
        assert '"REGISTRATION SUCCESSFUL"' in keywords


# ── Regression: an empty card date must match nothing (0.1.0.3) ─────────────

# _card_date_pattern("") compiled to `\b\s+(?:st|nd|rd|th)?\b`, which matches
# every run of whitespace in every card — so a caller with no date confirmed a
# booking against an arbitrary card.

class TestEmptyCardDate:
    def test_empty_date_matches_nothing(self):
        pattern = _card_date_pattern("")
        assert not pattern.search("Open Play\nSat, Aug 1st, 9a - 12p\nFREE")
        assert not pattern.search("   ")

    def test_registered_state_returns_nothing_without_a_date(self):
        import pickleball_booker as pb
        page = _make_page([_card("Open Play\nSat, Aug 1st, 9a - 12p\nFREE", "WITHDRAW")])
        assert pb._registered_state_for_session(page, "", dict(_SESSION)) == "", \
            "with no date to scope to, no card may confirm a booking"


# ── Regression: the meridiem must be word-anchored (0.1.0.3) ────────────────

class TestTimePatternAnchoring:
    def test_card_prose_does_not_parse_as_a_time(self):
        """"2 p" in "2 players per court", "4 a" in "4 available"."""
        card = "Open Play\nSat, Aug 1st\n50 of 50 spots remaining\n2 players per court\n4 available\nFREE"
        assert _extract_session_time(card) is None, \
            "prose beginning with a or p must not parse as a session time"

    @pytest.mark.parametrize("bad", ["after 5", "9 April", "5 people", "before 7"])
    def test_partial_matches_are_rejected(self, bad):
        assert _parse_start_time(bad) is None, \
            f"{bad!r} must not parse — a partial match booked the wrong hour"

    @pytest.mark.parametrize("good,expected", [
        ("9:00 AM", (9, 0)), ("7:30 PM", (19, 30)), ("12p", (12, 0)),
        ("9a", (9, 0)), ("14:30", (14, 30)), ("9:15a", (9, 15)),
    ])
    def test_real_times_still_parse(self, good, expected):
        assert _parse_start_time(good) == expected

    def test_range_pattern_needs_two_real_times(self):
        (h, m), display = _extract_session_time("Sat, Aug 1st\n12p - 3 players\n12p - 2:30p\nFREE")
        assert (h, m) == (12, 0)
        assert display == "12p–2:30p", \
            f"'12p - 3 players' must not parse as a range, got {display!r}"


# ── Regression: none_available must name the filter that actually fired ─────

class TestNoneAvailableReason:
    def test_proximity_miss_is_not_blamed_on_the_tier(self):
        page = _make_page(
            [_card("Open Play\nSat, Aug 1st, 2p - 5p\nFREE")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, target_h=9, target_m=0, tier="FULL")
        assert result["status"] == "none_available"
        assert "45 minutes" in result["message"] and "9:00 AM" in result["message"]
        assert "all day" not in result["message"], \
            "a FULL member must never be told an all-day window is why nothing matched"
        _assert_message_is_user_facing(result["message"])

    def test_fee_miss_says_fee(self):
        page = _make_page(
            [_card("Open Play\nSat, Aug 1st, 9a - 12p\n$12.00 per player")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, tier="FULL")
        assert "fee" in result["message"].lower()

    def test_closed_registration_says_so(self):
        page = _make_page(
            [_card("Open Play\nSat, Aug 1st, 9a - 12p\nFREE", "Registration Closed")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, tier="FULL")
        assert "registration isn't open" in result["message"]

    def test_tier_miss_still_names_the_window(self):
        page = _make_page(
            [_card("Open Play\nSat, Aug 1st, 5p - 8p\nFREE")],
            page_body="Sat, Aug 1st",
        )
        result = _scan_and_book(page, "Saturday, August 1, 2026", "Aug 1",
                                dry_run=True, tier="AM")
        assert "AM membership window" in result["message"]
        assert "2:30 PM" in result["message"]

    def test_reason_ordering_prefers_the_closest_near_miss(self):
        skipped = {"far_from_target": 1, "not_free": 1, "not_bookable": 1, "outside_tier": 1}
        msg = _none_available_message(skipped, "Saturday, August 1, 2026", "FULL", 9, 0)
        assert "45 minutes" in msg


# ── Regression: the reconfirmation probe must filter to the same day ────────

# The main scan clicks the Today / Tomorrow sidebar radio because the
# unfiltered list does not reliably render those cards. The probe skipped it,
# so a same-day booking could be confirmed against a view without its card.

class TestProbeDateFilter:
    def test_probe_clicks_today_for_a_same_day_booking(self, tmp_path):
        import pickleball_booker as pb
        page = _make_register_page(body="dashboard", url="https://x/Portal/Index", cards=[])
        with patch.object(pb, "SKILL_DIR", tmp_path):
            _register_session(page, dict(_SESSION), "Sat, Aug 1, 2026", "Aug 1", days_diff=0)
        page.reconfirm_probe.get_by_text.assert_called_with("Today", exact=True)

    def test_probe_clicks_tomorrow_for_a_next_day_booking(self, tmp_path):
        import pickleball_booker as pb
        page = _make_register_page(body="dashboard", url="https://x/Portal/Index", cards=[])
        with patch.object(pb, "SKILL_DIR", tmp_path):
            _register_session(page, dict(_SESSION), "Sat, Aug 1, 2026", "Aug 1", days_diff=1)
        page.reconfirm_probe.get_by_text.assert_called_with("Tomorrow", exact=True)

    def test_probe_leaves_the_filter_alone_further_out(self, tmp_path):
        import pickleball_booker as pb
        page = _make_register_page(body="dashboard", url="https://x/Portal/Index", cards=[])
        with patch.object(pb, "SKILL_DIR", tmp_path):
            _register_session(page, dict(_SESSION), "Sat, Aug 1, 2026", "Aug 1", days_diff=4)
        page.reconfirm_probe.get_by_text.assert_not_called()


# ── Regression: a page read that races a navigation must not lose a booking ──

# page.inner_text / page.url sat outside every try. CourtReserve redirects after
# finalize, so a Playwright call against a page mid-navigation raised, escaped
# to the catch-all in book_pickleball_session, and reported a COMPLETED booking
# as a generic `error` with no snapshot — which auto_confirm turns into a
# duplicate booking on the owner's retry.

class TestPostFinalizeReadFailure:
    def test_a_raising_page_read_still_confirms_via_the_events_list(self, tmp_path):
        import pickleball_booker as pb
        our_card = "Open Play\nMon, Aug 31st, 7a - 9a\nFREE"
        page = _make_register_page(body="irrelevant", url="https://x/Portal/Index", cards=[])
        page.inner_text = MagicMock(side_effect=RuntimeError("page is navigating"))
        page.reconfirm_probe.evaluate = MagicMock(return_value=[[our_card, "EDIT REGISTRATION"]])

        session = {"time_str": "7a-9a", "start_h": 7, "start_m": 0, "card_index": 0}
        with patch.object(pb, "SKILL_DIR", tmp_path):
            result = _register_session(page, session, "Mon, Aug 31, 2026", "Aug 31")

        assert result["status"] == "booked", \
            "a completed booking must not be lost because reading the landing page raised"

    def test_a_raising_url_read_does_not_escape(self, tmp_path):
        import pickleball_booker as pb
        page = _make_register_page(body="dashboard", url="https://x/Portal/Index", cards=[])
        type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("navigating")))
        try:
            with patch.object(pb, "SKILL_DIR", tmp_path):
                result = _register_session(page, dict(_SESSION), "Sat, Aug 1, 2026", "Aug 1")
        finally:
            del type(page).url
        assert result["status"] == "uncertain", \
            "an unreadable URL must classify as uncertain, not raise into the generic error path"


# ── Regression: the form filler must not tick paid add-ons (0.1.0.3) ────────

class TestCheckboxScoping:
    def test_risky_checkboxes_are_skipped(self):
        from pickleball_booker import _CHECKBOX_JS
        assert re.search(r"if \(!required && RISKY\.test\(textFor\(cb\)\)\)", _CHECKBOX_JS), \
            "a visible unchecked checkbox must be screened before it is clicked"
        for word in ("GUEST", "PARTNER", "SUBSCRIB", "DONAT", "PURCHASE"):
            assert word in _CHECKBOX_JS, \
                f"{word} add-ons attach a charge to a session the fee check cleared as FREE"

    def test_register_session_has_no_blanket_checkbox_click(self):
        import inspect
        source = inspect.getsource(_register_session)
        assert "querySelectorAll(\"input[type='checkbox']\")" not in source, \
            "_register_session must use the screened _CHECKBOX_JS, not click every box"


# ── Regression: --dry-run must not require a booking-only field (0.1.0.3) ───

class TestDryRunDoesNotNeedTheLevelField:
    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL", "SELF_RATED_LEVEL": ""}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_dry_run_skips_the_self_rated_level_gate(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "SELF_RATED_LEVEL" not in result.get("message", ""), \
            "an availability check never opens a registration form"
        assert "7 days" in result["message"], "it should fail on the date instead"

    @no_prefs
    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "FULL", "SELF_RATED_LEVEL": ""}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_booking_still_requires_the_self_rated_level(self, mock_load):
        result = book_pickleball_session(dry_run=False, target_date_str="2099-01-01")
        assert result["status"] == "error"
        assert "SELF_RATED_LEVEL" in result["message"]


# ── Error paths on the new helpers (0.1.0.3 coverage) ───────────────────────

# These are the branches a bad preferences file, an odd button label or a
# missing sidebar radio actually take. Each one fails toward "carry on" rather
# than raising, so an untested one fails silently by design — exactly the shape
# of the bug this release spent most of its findings on.

class TestReadPreferencesErrorPaths:
    def test_missing_file_is_empty(self, tmp_path):
        import pickleball_booker as pb
        with patch.object(pb, "PREFS_PATH", tmp_path / "nope.json"):
            assert pb._read_preferences() == {}

    def test_valid_file_is_returned(self, tmp_path):
        import pickleball_booker as pb
        p = tmp_path / "preferences.json"
        p.write_text(json.dumps({"membership_tier": "FULL", "auto_confirm": True}))
        with patch.object(pb, "PREFS_PATH", p):
            assert pb._read_preferences()["membership_tier"] == "FULL"

    def test_malformed_json_is_empty_not_a_crash(self, tmp_path, capsys):
        import pickleball_booker as pb
        p = tmp_path / "preferences.json"
        p.write_text("{ this is not json")
        with patch.object(pb, "PREFS_PATH", p):
            assert pb._read_preferences() == {}
        assert "preferences.json" in capsys.readouterr().err, \
            "the parse failure must be logged to stderr for post-mortem"

    def test_non_dict_json_is_empty(self, tmp_path):
        """A top-level list would make .get() blow up downstream."""
        import pickleball_booker as pb
        p = tmp_path / "preferences.json"
        p.write_text('["FULL"]')
        with patch.object(pb, "PREFS_PATH", p):
            assert pb._read_preferences() == {}

    @patch("pickleball_booker._read_preferences", lambda: {"membership_tier": "   "})
    @patch.dict(os.environ, {}, clear=False)
    def test_blank_tier_falls_through_to_env(self):
        import pickleball_booker as pb
        os.environ.pop("MEMBERSHIP_TYPE", None)
        assert pb._membership_tier() == (None, "")


class TestClassifierAndPatternEdges:
    @pytest.mark.parametrize("label", ["", "   ", None, "Details", ""])
    def test_unrecognised_labels_are_unavailable(self, label):
        """Fail closed: an unreadable label must never be treated as bookable."""
        assert _classify_button(label) == "unavailable"

    @pytest.mark.parametrize("bad", ["Aug", "", "   "])
    def test_malformed_card_date_matches_nothing(self, bad):
        assert not _card_date_pattern(bad).search("Sat, Aug 1st, 9a - 12p")

    def test_generic_none_available_message_when_nothing_was_filtered(self):
        """Zero cards on the page — no filter fired, so name no filter."""
        msg = _none_available_message({}, "Saturday, August 1, 2026", "FULL", None, None)
        assert "No free Open Play sessions found" in msg
        assert "membership window" not in msg
        _assert_message_is_user_facing(msg)


class TestApplyDateFilterErrorPath:
    def test_a_missing_sidebar_radio_does_not_raise(self, capsys):
        import pickleball_booker as pb
        page = MagicMock()
        page.get_by_text = MagicMock(side_effect=RuntimeError("no such element"))
        pb._apply_date_filter(page, 0)   # must not propagate
        assert "Today" in capsys.readouterr().err

    def test_no_filter_is_applied_without_a_day_offset(self):
        import pickleball_booker as pb
        page = MagicMock()
        pb._apply_date_filter(page, None)
        page.get_by_text.assert_not_called()
