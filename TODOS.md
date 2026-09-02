# TODOS

## Testing

### Integration test infrastructure for pickleball booker
**Priority:** P2
**What:** Set up Playwright test infrastructure with mocked CourtReserve responses so `_scan_and_book()` filtering can be tested per-tier without hitting the real site.
**Why:** Unit tests cover pure logic, but can't verify Playwright selectors and page interactions work with filtered results per membership tier.
**Pros:** Catches regressions when CourtReserve changes their HTML structure.
**Cons:** Significant effort to mock CourtReserve page structure. Mocks can drift from reality.
**Context:** No integration test infrastructure exists today. This is a community tool, not a production service.
**Depends on:** Nothing — the blocking dependency (multi-membership unit tests) shipped in 0.1.0.0 on 2026-04-04. This is now unblocked.
**Added:** 2026-04-04 via /plan-eng-review
**Reviewed:** 2026-08-03 — still open, dependency cleared. The 0.1.0.1 "Beginner Open Play" regression is exactly the class of bug this would have caught.
**Reviewed:** 2026-09-02 — still open. Both remaining coverage gaps in 0.1.0.3 are browser JS (`_COLLECT_CARDS_JS`, `_CHECKBOX_JS`) that only this harness could exercise. The 0.1.0.3 DOM-walk bug — a Clinic card inheriting a sibling Open Play card's text — is unreachable from unit tests and would have been caught here.

### No CI runs the test suite
**Priority:** P2
**What:** Add a GitHub Actions workflow running `pytest` on push and pull_request.
**Why:** The repo is public and has 189 tests that run in under a second, but nothing runs them except a developer remembering to. A contributor's PR gets no signal at all.
**Pros:** Cheap (seconds per run), and the suite is already fast and hermetic — no network, no browser.
**Cons:** None material. Needs `pip install -r requirements.txt` plus playwright only if integration tests land later.
**Context:** Flagged by the pre-landing review during the 0.1.0.3 ship. Not added in that release to keep a bugfix PR scoped.
**Added:** 2026-09-02 via /ship

## Booker internals

### Date-filter click is duplicated between the scan and the reconfirmation probe
**Priority:** P3
**What:** `_apply_date_filter()` and the inline Today/Tomorrow clicking in `book_pickleball_session()` do the same job in two places.
**Why:** They can drift. A drift between the main scan's view and the probe's view is exactly the 0.1.0.3 bug where the probe read an unfiltered list and reported a completed booking as `uncertain`.
**Pros:** One code path means the probe can't silently diverge from the scan again.
**Cons:** The main flow's copy carries extra `wait_for_selector` handling the probe doesn't need, and unifying them edits the live booking path. Deferred out of 0.1.0.3 for that reason — the drift risk was judged smaller than the risk of refactoring the booking path without integration tests.
**Context:** Flagged by the maintainability specialist during the 0.1.0.3 ship.
**Added:** 2026-09-02 via /ship
**Depends on:** Integration test infrastructure (above) would make this safe to do.

## Completed

_Nothing recorded yet. Items move here with a `**Completed:** vX.Y.Z (YYYY-MM-DD)` line._
