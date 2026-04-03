---
name: pickleball-booker
description: Automates court reservations at Pickleball Haven Lake Forest (site ID 13464) via CourtReserve.
---

# Pickleball Booker

A standalone tool for booking FREE AM Open Play sessions at Pickleball Haven Lake Forest.

## Capabilities
- **Check Availability:** Scans for free open play sessions for today or tomorrow.
- **Auto-Book:** Automatically registers for a session if one is available and free.
- **Target Time:** Prioritizes sessions closest to a specific time (e.g., 8:00 AM).

## Usage
The agent calls this via bash. The agent is responsible for calculating the specific YYYY-MM-DD date if the user provides a relative day (e.g., "next Friday").

```bash
# Book for a specific date (up to 7 days out)
python3 pickleball_booker.py --date "2026-04-03" --target-time "9:00 AM"

# Dry-run to check availability without booking
python3 pickleball_booker.py --date "2026-04-03" --dry-run
```

## Setup (User)
- Requires `playwright` (`pip install playwright` + `playwright install chromium`).
- Requires credentials in `.env` or macOS Keychain:
    - `COURTRESERVE_EMAIL`
    - `COURTRESERVE_PASS`
