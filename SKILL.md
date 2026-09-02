---
name: pickleball-booker
description: Automates court reservations at Pickleball Haven Lake Forest (site ID 13464) via CourtReserve. Supports AM, PM, and Full Day membership tiers.
---

# Pickleball Booker

A standalone tool for booking FREE Open Play sessions at Pickleball Haven Lake Forest.
Supports **AM**, **PM**, and **Full Day** membership tiers.

## Membership Tiers

The booker filters sessions by the tier recorded in `preferences.json` (`"membership_tier"`). `MEMBERSHIP_TYPE` in `.env` is only a fallback for a checkout that has no `preferences.json`, and `preferences.json` wins whenever both are present.

| Tier | Time Window |
|------|------------|
| **AM** | Before 2:30 PM |
| **PM** | 2:30 PM onward |
| **FULL** | All day, no restriction |

**There is no default tier.** With neither source set the booker returns `error` asking for one, rather than assuming AM — a silent AM default made a FULL member's every evening request come back as "no sessions found for your AM membership".

**First-run check:** If the booker reports no configured tier, ask the user: "What membership tier do you have at Pickleball Haven — AM, PM, or Full Day?" Then add it as `"membership_tier"` in `preferences.json`.

## Capabilities
- **Check Availability:** Scans for free open play sessions for today or tomorrow.
- **Auto-Book:** Automatically registers for a session if one is available and free.
- **Target Time:** Prioritizes sessions closest to a specific time (e.g., 8:00 AM).
- **Tier-Aware:** Only shows sessions within the user's membership time window.

## Usage
The agent calls this via bash. The agent is responsible for calculating the specific YYYY-MM-DD date if the user provides a relative day (e.g., "next Friday", "Monday", "tomorrow").

```bash
# Book for a specific date (up to 7 days out)
python3 pickleball_booker.py --date "2026-04-06" --target-time "9:00 AM"

# Dry-run to check availability without booking
python3 pickleball_booker.py --date "2026-04-06" --dry-run
```

## Booking Orchestration (agent rules — read every time)

Before invoking the booker, ALWAYS read `preferences.json` in this skill's directory. It is the single source of truth for the owner's standing rules (tier, default slots, auto-confirm posture). If the file is missing, ask the owner which tier they're on rather than assuming one — this file is `.gitignore`d, so a fresh checkout won't have it, and guessing the tier is how the agent ends up refusing slots the owner can actually book.

### Imperative skip-confirmation rule (mandatory)
If the user's message contains an imperative booking phrase — `"book me"`, `"book it"`, `"book the ..."`, `"reserve me ..."` — and a target slot can be resolved deterministically from the time-of-day rules below, **skip the "would you like me to book?" confirmation step and book directly**. The imperative IS the confirmation. Asking again is the May-1 confirmation-loop bug (user repeats the same imperative, agent re-dry-runs, nothing ever books). Only ask for confirmation when the date or time is genuinely ambiguous (e.g., "book me for next Monday" with no time, or a requested time the tier in `preferences.json` doesn't cover).

### Default date when only a time is given (mandatory)
If the user gives a target time but no date — e.g. "book me for the 9 am session", "book the 6 pm slot" — default to **TODAY** in local time, never tomorrow. Only roll to tomorrow if today's slot has already finished and the user is clearly thinking ahead. Confirming the date is fine when it's genuinely ambiguous ("Friday's 9 am"); inventing a future date silently is the 2026-06-29 failure mode (user said "book me for the 9 am session", agent booked Jun 30 dry-run without asking, user had to correct it).

### Time-of-day → slot mapping (for "now" / "right now" / no time given)
When the user says "now"/"right now"/"today" with no explicit time, pass the **current local time** as `--target-time`, formatted `H:MM AM/PM`. The booker picks the session closest to it. **Never use a fixed default like 9 AM irrespective of the current time** — that was a prior failure mode.

Do not encode tier windows or slot boundaries here. The tier lives in `preferences.json` and the booker enforces it: a time outside the tier's window comes back as `error` with the window named, and `--target-time` only books a session within 45 minutes of the request. Open Play is drop-in, so a slot already in progress is still worth booking — prefer the one that has started over making the owner wait for the next.

If every slot for today has already finished, say so and offer tomorrow. Do not silently roll the date.

### Never claim success without a terminal status, AND always report the terminal status (mandatory)
Terminal statuses returned by the booker: `booked`, `uncertain`, `already_booked`, `none_available`, `dry_run`, `login_failed`, `error`. Anything else — `"Process still running"`, `"(no new output)"`, `"Command still running"` — is **not** a terminal status. Poll again. **Never** synthesize a "Done! ✅" message from a non-terminal output. This is the April-23 false-success bug.

When a terminal status IS reached, **you must reply to the user with that status before ending the turn** — never go silent. Empty replies after a successful tool call are the May-19 silent-failure bug: user said "Yes", booker returned `uncertain`, agent's next turn was an empty message, user got no answer at all. The user must always learn whether their booking went through.

Per-status reply guidance:
- `booked`: "🎾 Booked you into the [time slot] session on [date]." Use the actual slot from the booker, not the requested target.
- `uncertain`: relay the booker's own `message` field — it distinguishes the cases. When it says the form didn't submit, the session is most likely NOT booked; say so plainly rather than softening it. Otherwise: "Registration completed but CourtReserve didn't surface a confirmation I could recognize. Please check your CourtReserve account to verify." Offer to re-dry-run in a few minutes to test.
- `already_booked`: "You're already registered for this session."
- `login_failed`: "I couldn't get into your CourtReserve account — the login was rejected. Nothing was booked. Your saved credentials may need updating." Do not retry automatically; repeated attempts can lock the account.
- `none_available`: "No free sessions match — [reason from message field]."
- `error`: surface the error message verbatim.

### Duplicate-message handling
If the user re-sends the same booking command within 60 seconds, do NOT treat it as a new booking request. Possibilities:
1. The user thinks the first one didn't go through. → Reply: "🎾 Still working on the first one — hold on a few seconds."
2. The user is impatiently confirming after the agent asked for confirmation. → Treat as "Yes, proceed" and book.
Distinguish by whether the booker is still running (case 1) or you're waiting on user confirmation (case 2).

### Date confirmation (kept for non-imperative cases)
For ambiguous dates only (e.g., "book me for next Friday"), tell the user the full resolved date you're about to book and wait for confirmation. Example: "Booking for Friday, April 12, 2026 at 9:00 AM — is that right?" For unambiguous imperatives with resolved dates ("book me right now"), the imperative-skip rule above takes precedence.

## Response Status Codes
- `booked` — Confirmed success. CourtReserve showed a booking confirmation page.
- `uncertain` — Registration steps were completed (buttons clicked) but no confirmation message appeared. Tell the user to manually verify in CourtReserve. Do NOT report this as a successful booking.
- `dry_run` — Availability found; no booking attempted. `sessions` is a list of all available slots (e.g. `[{"time": "7a–9a"}, {"time": "9a–12p"}]`). Show all of them to the user so they can choose which to book.
- `already_booked` — Already registered for this session.
- `none_available` — No free Open Play sessions match. The `message` field names the filter that actually rejected them — requested time, fee, closed registration, or tier window — so relay it as written rather than describing it as a tier problem.
- `login_failed` — CourtReserve rejected the login, or the session didn't survive the hop to the events list. Nothing was booked. Surface the `message` and stop; don't retry in a loop.
- `error` — Script failed; check the `message` field. Common errors:
    - Invalid `MEMBERSHIP_TYPE` (not AM, PM, or FULL)
    - Target time outside the user's tier window (e.g., PM member requesting 9:00 AM)

## Known Limitations
- Only **Open Play** sessions are supported regardless of tier. Reserved courts, clinics, **Beginner Open Play**, **Special Events** and cards marked **"NO OPEN PLAY"** (tournament days) are not bookable — all of them are excluded from the scan even though their card text also contains the substring "Open Play".
- The PM tier floor (2:30 PM) is assumed from the AM ceiling. Verify with Pickleball Haven if PM sessions start at a different time.

## Setup (User)
- Requires `playwright` (`pip install playwright` + `playwright install chromium`).
- Requires credentials in `.env` or macOS Keychain:
    - `COURTRESERVE_EMAIL`
    - `COURTRESERVE_PASS`
    - `SELF_RATED_LEVEL` (booking only — `--dry-run` does not need it)
    - `MEMBERSHIP_TYPE` (AM, PM, or FULL) — fallback only; `preferences.json` is the source of truth and there is no default
- See `.env.example` for a template.
