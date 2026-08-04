# TODOS

## Integration test infrastructure for pickleball booker
**What:** Set up Playwright test infrastructure with mocked CourtReserve responses so `_scan_and_book()` filtering can be tested per-tier without hitting the real site.
**Why:** Unit tests cover pure logic, but can't verify Playwright selectors and page interactions work with filtered results per membership tier.
**Pros:** Catches regressions when CourtReserve changes their HTML structure.
**Cons:** Significant effort to mock CourtReserve page structure. Mocks can drift from reality.
**Context:** No integration test infrastructure exists today. This is a community tool, not a production service.
**Depends on:** Nothing — the blocking dependency (multi-membership unit tests) shipped in 0.1.0.0 on 2026-04-04. This is now unblocked.
**Added:** 2026-04-04 via /plan-eng-review
**Reviewed:** 2026-08-03 — still open, dependency cleared. The 0.1.0.1 "Beginner Open Play" regression is exactly the class of bug this would have caught.
