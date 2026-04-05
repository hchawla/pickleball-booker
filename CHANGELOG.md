# Changelog

All notable changes to the Pickleball Haven Booker will be documented in this file.

## [0.1.0.0] - 2026-04-04

### Added
- Multi-membership support: AM, PM, and Full Day tiers. Set `MEMBERSHIP_TYPE` in `.env`.
- Pre-scan validation catches tier/time conflicts before launching the browser.
- Tier-aware error messages include your membership window when no sessions match.
- `.env.example` template with all configuration fields documented.
- 37 unit tests covering all tier filtering logic, validation, and edge cases.

### Changed
- `_load_env()` now always parses `.env` for non-credential config, even when credentials come from Keychain.
- `_is_before_cutoff()` replaced with `_is_within_tier_window()` supporting all three tiers.
- FULL tier skips time filtering entirely instead of using a fake all-day window.
- Invalid `MEMBERSHIP_TYPE` returns a clear error instead of silently defaulting to AM.
- SKILL.md and README.md updated for multi-tier documentation.
