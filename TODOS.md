# TODOS

## Integration test infrastructure for pickleball booker
**What:** Set up Playwright test infrastructure with mocked CourtReserve responses so `_scan_and_book()` filtering can be tested per-tier without hitting the real site.
**Why:** Unit tests cover pure logic, but can't verify Playwright selectors and page interactions work with filtered results per membership tier.
**Pros:** Catches regressions when CourtReserve changes their HTML structure.
**Cons:** Significant effort to mock CourtReserve page structure. Mocks can drift from reality.
**Context:** No test infrastructure exists today. This is a community tool, not a production service. Unit tests ship first in the multi-membership PR.
**Depends on:** Multi-membership unit tests shipping first.
**Added:** 2026-04-04 via /plan-eng-review
