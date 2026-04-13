"""Unit tests for pickleball booker — membership tier logic and browser config."""

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
