# Pickleball Haven Booker

A standalone OpenClaw skill to automate court reservations at Pickleball Haven Lake Forest via CourtReserve.

## Installation

1.  **Clone or Copy** this folder into your OpenClaw workspace:
    `~/.openclaw/workspace/skills/pickleball-booker/`

2.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    playwright install chromium
    ```

3.  **Configure Credentials:**
    Copy `.env.example` to `.env` and fill in your values:
    ```bash
    cp .env.example .env
    # Edit .env with your CourtReserve email, password, and membership tier
    ```
    *Alternatively, use macOS Keychain with the service name `openclaw-pickleball-booker`.*

## Membership Configuration

Set your membership tier in `.env` via the `MEMBERSHIP_TYPE` variable:

| Tier | Value | Time Window | Notes |
|------|-------|------------|-------|
| AM Membership | `MEMBERSHIP_TYPE=AM` | Before 2:30 PM | **Default** if not set |
| PM Membership | `MEMBERSHIP_TYPE=PM` | 2:30 PM onward | |
| Full Day | `MEMBERSHIP_TYPE=FULL` | All day | No time restriction |

**All tiers share these constraints:**

| Constraint | Value | Why |
|---|---|---|
| Session type | **Open Play only** | Reserved courts and clinics are not supported |
| Cost | **Free only** | Paid sessions are skipped |
| Booking window | **Up to 7 days out** | CourtReserve site limit for this club |
| Target-time tolerance | **+/- 45 minutes** | When `--target-time` is set, only books sessions within 45 min of the requested time |

## Usage

### From the CLI
- **Check today's availability (Dry Run):**
  `python3 pickleball_booker.py --dry-run`
- **Book for tomorrow at 8:00 AM:**
  `python3 pickleball_booker.py --date "2026-04-03" --target-time "8:00 AM"`

### Via the OpenClaw Agent
Ask your agent:
- "Check if there are any pickleball sessions tomorrow morning."
- "Book me a court for 9 AM today at Pickleball Haven."

## How to Share

1.  **GitHub:** Push this folder to a new repo. Others can install it with `openclaw skills install <your-repo-url>`.
2.  **Zip:** Zip this entire folder (excluding `.env`) and send it to a friend.

---
**Security Note:** Never share your `.env` file. It contains your personal login credentials.
