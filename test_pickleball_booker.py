"""Unit tests for pickleball booker — membership tier logic and browser config."""

import json
import os
import re
import pytest
from unittest.mock import patch, MagicMock

# Import the module functions and constants we need to test.
# _load_env() runs at import time, so we set dummy creds to prevent errors.
os.environ.setdefault("COURTRESERVE_EMAIL", "test@example.com")
os.environ.setdefault("COURTRESERVE_PASS", "testpass")

from pickleball_booker import (
    _parse_start_time,
    _is_within_tier_window,
    _tier_window_label,
    _scan_and_book,
    TIER_RULES,
    VALID_TIERS,
    TierWindow,
    book_pickleball_session,
)


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
    """Test that book_pickleball_session validates MEMBERSHIP_TYPE before launching a browser."""

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "MORNING"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_invalid_tier_returns_error(self, mock_load):
        result = book_pickleball_session(dry_run=True)
        assert result["status"] == "error"
        assert "MORNING" in result["message"]
        assert "invalid" in result["message"].lower()

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "AM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_valid_am_not_rejected(self, mock_load):
        # Give it a date far enough out that it won't launch the browser (>7 days)
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        # Should fail with "more than 7 days out", NOT "invalid tier"
        assert "invalid" not in result.get("message", "").lower()

    @patch.dict(os.environ, {}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_missing_defaults_to_am(self, mock_load):
        os.environ.pop("MEMBERSHIP_TYPE", None)
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "invalid" not in result.get("message", "").lower()

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": " pm "}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_whitespace_trimmed(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "invalid" not in result.get("message", "").lower()

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "Pm"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_case_insensitive(self, mock_load):
        result = book_pickleball_session(dry_run=True, target_date_str="2099-01-01")
        assert "invalid" not in result.get("message", "").lower()


# ── Pre-scan target-time validation ───────────────────────────────────────────

class TestPreScanValidation:
    """Test that target-time / tier conflicts are caught before launching the browser."""

    @patch.dict(os.environ, {"MEMBERSHIP_TYPE": "PM"}, clear=False)
    @patch("pickleball_booker._load_env")
    def test_pm_tier_morning_target_error(self, mock_load):
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = book_pickleball_session(dry_run=True, target_time="9:00 AM", target_date_str=tomorrow)
        assert result["status"] == "error"
        assert "outside your tier window" in result["message"]

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
    """Verify that _scan_and_book uses card-level DOM scoping, not unbounded parent walk."""

    def test_source_has_card_class_check(self):
        import inspect
        source = inspect.getsource(_scan_and_book)
        assert "card" in source.lower() and "className" in source, \
            "_scan_and_book must check CSS class for card-level container to prevent cross-card text leaking"

    def test_source_does_not_use_unbounded_walk(self):
        """The old bug: walking up DOM until any parent has 'OPEN PLAY' grabs sibling cards."""
        import inspect
        source = inspect.getsource(_scan_and_book)
        # The JS should check className before returning, not just innerText
        js_blocks = re.findall(r"btn\.evaluate\(.*?\)\)", source, re.DOTALL)
        assert len(js_blocks) > 0, "Expected at least one btn.evaluate() call"
        js = js_blocks[0]
        # The JS should reference className or classList, not just walk up blindly
        assert "className" in js or "classList" in js, \
            "DOM traversal must check element class to stop at the card boundary"


# ── Beginner Open Play exclusion ─────────────────────────────────────────────

# Root cause of the 2026-07-31 mis-booking: "Beginner Open Play" cards also
# contain the substring "OPEN PLAY", so they satisfied the card filter and
# competed on time-proximity like any regular Open Play card. Requested a
# 9:00 AM regular session; the Beginner 9:00 AM card was closer to the
# target time than the regular one (which started at 9:15 that day), so it
# won the sort and got booked instead.

def _make_card_button(card_text: str):
    btn = MagicMock()
    btn.evaluate = MagicMock(return_value=card_text)
    btn.inner_text = MagicMock(return_value="Register")
    return btn


class TestBeginnerSessionExclusion:
    def test_beginner_card_not_booked_over_regular(self):
        beginner_btn = _make_card_button(
            "Beginner Open Play\n"
            "Beginner Open Play/Skills 9AM-12PM\n"
            "Fri, Jul 31st\n"
            "9a - 12p\n"
            "FREE"
        )
        regular_btn = _make_card_button(
            "Open Play\n"
            "Open Play/Challenge Courts 9:15AM-12PM\n"
            "Fri, Jul 31st\n"
            "9:15a - 12p\n"
            "FREE"
        )

        page = MagicMock()
        page.inner_text = MagicMock(return_value="Sessions for Fri, Jul 31st are listed below.")
        page.locator.return_value.all.return_value = [beginner_btn, regular_btn]

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
