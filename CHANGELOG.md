# Changelog

All notable changes to the Pickleball Haven Booker will be documented in this file.

## [0.1.0.1] - 2026-07-31

### Fixed
- Card scan matched any text containing "OPEN PLAY", which also matches "Beginner Open Play". On 2026-07-31 a 9:00 AM request booked the Beginner session instead of the regular one because it sorted closer to the target time. Cards containing "BEGINNER" are now excluded outright.

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
