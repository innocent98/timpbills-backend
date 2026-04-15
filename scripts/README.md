# Backend scripts

Utility scripts runnable via `poetry run python scripts/<name>.py` (local) or
`make <target>` (docker-compose).

## `seed_dev_user.py`

Creates / updates a known dev user with pre-verified phone and set PIN so the
Flutter app's E2E tests can bypass registration and jump to login.

Run via:

    make seed                                   # inside docker-compose
    poetry run python scripts/seed_dev_user.py  # local

Credentials seeded:

| field    | value              |
| -------- | ------------------ |
| phone    | +2348000000001     |
| email    | dev@timpbills.test |
| password | Test1234!          |
| PIN      | 1357               |
| KYC      | tier_1             |

The script is idempotent — re-running updates the existing row rather than
creating a duplicate. Never use this script in production.
