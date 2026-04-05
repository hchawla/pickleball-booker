---
name: pickleball-booker
description: Automates court reservations at Pickleball Haven Lake Forest (site ID 13464) via CourtReserve. Supports AM, PM, and Full Day membership tiers.
---

# Pickleball Booker

A standalone tool for booking FREE Open Play sessions at Pickleball Haven Lake Forest.
Supports **AM**, **PM**, and **Full Day** membership tiers.

## Membership Tiers

The booker filters sessions based on the user's membership type (set via `MEMBERSHIP_TYPE` in `.env`):

| Tier | Time Window | Default? |
|------|------------|----------|
| **AM** | Before 2:30 PM | Yes (if not set) |
| **PM** | 2:30 PM onward | No |
| **FULL** | All day, no restriction | No |

**First-run check:** If `MEMBERSHIP_TYPE` is not set in `.env`, ask the user: "What membership tier do you have at Pickleball Haven — AM, PM, or Full Day?" Then help them add it to their `.env` file.

## Capabilities
- **Check Availability:** Scans for free open play sessions for today or tomorrow.
- **Auto-Book:** Automatically registers for a session if one is available and free.
- **Target Time:** Prioritizes sessions closest to a specific time (e.g., 8:00 AM).
- **Tier-Aware:** Only shows sessions within the user's membership time window.

## Usage
The agent calls this via bash. The agent is responsible for calculating the specific YYYY-MM-DD date if the user provides a relative day (e.g., "next Friday", "Monday", "tomorrow").

**IMPORTANT — Date confirmation:** Before booking (not dry-run), always tell the user the full resolved date and time you're about to book (e.g., "Booking for Monday, April 6, 2026 at 9:00 AM — is that right?") and wait for confirmation. This prevents silent wrong-date bookings.

```bash
# Book for a specific date (up to 7 days out)
python3 pickleball_booker.py --date "2026-04-06" --target-time "9:00 AM"

# Dry-run to check availability without booking
python3 pickleball_booker.py --date "2026-04-06" --dry-run
```

## Response Status Codes
- `booked` — Confirmed success. CourtReserve showed a booking confirmation page.
- `uncertain` — Registration steps were completed (buttons clicked) but no confirmation message appeared. Tell the user to manually verify in CourtReserve. Do NOT report this as a successful booking.
- `dry_run` — Availability found; no booking attempted. `sessions` is a list of all available slots (e.g. `[{"time": "7a–9a"}, {"time": "9a–12p"}]`). Show all of them to the user so they can choose which to book.
- `already_booked` — Already registered for this session.
- `none_available` — No free Open Play sessions match the criteria for this membership tier. The `message` field includes the tier and time window.
- `error` — Script failed; check the `message` field. Common errors:
    - Invalid `MEMBERSHIP_TYPE` (not AM, PM, or FULL)
    - Target time outside the user's tier window (e.g., PM member requesting 9:00 AM)

## Known Limitations
- Only **Open Play** sessions are supported regardless of tier. Reserved courts and clinics are not bookable.
- The PM tier floor (2:30 PM) is assumed from the AM ceiling. Verify with Pickleball Haven if PM sessions start at a different time.

## Setup (User)
- Requires `playwright` (`pip install playwright` + `playwright install chromium`).
- Requires credentials in `.env` or macOS Keychain:
    - `COURTRESERVE_EMAIL`
    - `COURTRESERVE_PASS`
    - `MEMBERSHIP_TYPE` (AM, PM, or FULL — defaults to AM)
- See `.env.example` for a template.
