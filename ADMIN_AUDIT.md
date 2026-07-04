# Admin and Quota Audit

Date: 2026-07-04

## Scope

Reviewed the current Telegram flight-search project with focus on:

- seeing which Telegram users use search;
- limiting expensive search runs, for example 2 per user per day;
- allowing extra searches only after owner approval;
- deciding whether this should be a Telegram admin panel or a separate web admin.

No web UI/admin surface exists in the repository today, so the practical first version should be Telegram-based admin commands.

## Current State

The project is a Python Telegram bot backed by SQLite.

Existing useful foundations:

- `users` table stores Telegram `user_id`, `chat_id`, `status`, and `created_at`.
- `user_searches` stores saved search configs.
- `search_snapshots` and `route_snapshots` store actual result history.
- `route_subscriptions` stores selected route alerts.
- Tests cover solver, cache behavior, route snapshots, subscriptions, discovery, and monitoring.

Main search creation flow:

- `/start` registers users in `users`.
- `/new_search` starts an 11-step wizard.
- The final wizard step saves a row in `user_searches`.
- Immediately after saving, the bot launches `run_search_and_update_baseline(...)`.

Expensive execution path:

- `run_single_search_and_send(...)` resolves destinations, discovers candidate edges, queries Travelpayouts, runs solver, saves snapshots, and sends the report.
- `/refresh_search` can force a fresh recalculation and bypass cache.
- `/check_route` can manually query a direct segment.

## Key Findings

1. There is no admin authorization model.

The config has credentials and DB path only. There is no `ADMIN_USER_IDS`, no owner check helper, and no admin commands. Any future admin feature needs a strict Telegram user-id allowlist before exposing user data or approval actions.

2. There is no search-run quota or audit log.

The project stores saved search configs and result snapshots, but not "a user spent one quota unit at this time." A daily limit cannot be implemented correctly from `user_searches` alone because one saved search can be refreshed multiple times, and a user can run expensive commands without creating a new search.

3. Quota must protect execution, not only search creation.

If the limit is checked only before `save_user_search`, users can still consume API capacity via `/refresh_search` or direct checks. The guard should sit before every expensive entry point:

- new search immediate run;
- refresh saved search;
- direct route/segment checks if those should be limited;
- future "buy now/fresh" commands.

4. Current schema is close to ready.

SQLite migrations are already done in `init_db()` using additive columns and new tables. Adding `search_runs`, `approval_requests`, and admin fields can follow the same pattern without introducing a migration framework yet.

5. Telegram admin MVP is the best first step.

A separate web panel would require HTTP server, auth/session model, CSRF protection, hosting, and UI work. For this project stage, Telegram commands are faster and safer:

- `/admin` summary;
- `/admin_users`;
- `/admin_requests`;
- `/approve <request_id>`;
- `/deny <request_id>`;
- `/set_limit <user_id> <daily_limit>`.

## Recommended Data Model

Add config:

- `ADMIN_USER_IDS`: comma-separated Telegram ids.
- `DEFAULT_DAILY_SEARCH_LIMIT`: default `2`.

Extend `users`:

- `daily_search_limit INTEGER DEFAULT 2`
- `is_admin INTEGER DEFAULT 0`
- `blocked_reason TEXT`
- `last_seen_at TEXT`

Add `search_runs`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `user_id INTEGER NOT NULL`
- `chat_id INTEGER NOT NULL`
- `search_id INTEGER`
- `run_type TEXT NOT NULL` such as `new_search`, `refresh_search`, `check_route`, `monitor`
- `status TEXT NOT NULL` such as `started`, `completed`, `failed`, `denied_quota`
- `created_at TEXT DEFAULT CURRENT_TIMESTAMP`
- `finished_at TEXT`
- `metadata_json TEXT DEFAULT '{}'`

Add `approval_requests`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `user_id INTEGER NOT NULL`
- `chat_id INTEGER NOT NULL`
- `search_id INTEGER`
- `request_type TEXT NOT NULL` such as `extra_search`
- `status TEXT NOT NULL DEFAULT 'pending'`
- `requested_payload_json TEXT DEFAULT '{}'`
- `admin_user_id INTEGER`
- `admin_note TEXT`
- `created_at TEXT DEFAULT CURRENT_TIMESTAMP`
- `decided_at TEXT`

Optional later:

- `quota_grants` for one-off extra credits or temporary subscriptions.
- `admin_audit_log` for every admin action.

## Recommended Flow

Normal user:

1. User finishes `/new_search`.
2. Bot calls `can_start_search(user_id, run_type)`.
3. If today's count is below limit, bot creates `search_runs` row and starts the search.
4. If the limit is exceeded, bot creates `approval_requests` row and tells the user: "Daily limit is used. Request sent to admin."

Admin:

1. Admin receives a Telegram notification with request details.
2. Admin presses inline button or runs `/approve <id>`.
3. Bot marks request approved and either:
   - starts the pending search immediately, or
   - grants one extra credit and asks the user to retry.

Recommended MVP behavior: start the pending search immediately after approval, because it feels better to the user and proves the approval flow works end to end.

## Implementation Plan

Phase 1: Telegram admin and quotas

- Add config parsing for admin ids and default daily limit.
- Add DB tables and helper functions:
  - register/update user activity;
  - count today's billable runs;
  - create/update run records;
  - create/list/approve/deny approval requests;
  - list users with usage stats.
- Add `is_admin(user_id)` helper.
- Add quota guard before new search execution and `/refresh_search`.
- Add admin commands and inline approval buttons.
- Extend tests for quota and approval helpers.

Phase 2: Better visibility

- Add `/admin_user <user_id>` with searches, snapshots, active subscriptions, last activity, quota used today.
- Add `/admin_searches` with recent runs and failures.
- Add daily usage summary.

Phase 3: Web admin if needed

- Add a small FastAPI admin app only after Telegram admin proves the model.
- Reuse the same DB helpers.
- Protect with a real auth layer, not a hidden URL.

## Product Notes

The user-facing copy should be clear and calm:

- When under limit: no extra friction.
- When over limit: "Лимит на сегодня использован. Я отправил заявку админу."
- When approved: "Заявка одобрена, запускаю поиск."
- When denied: "Админ пока не одобрил дополнительный поиск."

Do not expose other users' data to non-admin users. Admin commands must fail closed.

## Verification

Current test command passed:

```bash
python3 test_solver.py
```

Result: `Test finished successfully!`

## Implementation Status

Implemented after this audit:

- Telegram admin MVP with `/admin`, `/admin_users`, `/admin_requests`, `/set_limit`, `/approve`, and `/deny`.
- Inline approve/deny buttons in admin notifications.
- `ADMIN_USER_IDS` and `DEFAULT_DAILY_SEARCH_LIMIT` config.
- User daily limits and last-seen tracking.
- `search_runs` quota/audit table.
- `approval_requests` table.
- Quota guard before new search execution, `/refresh_search`, and `/check_route`.
- Approved requests automatically start the stored pending action.
- Tests for quota and approval DB helpers.

To enable the admin panel, set:

```bash
ADMIN_USER_IDS=123456789
DEFAULT_DAILY_SEARCH_LIMIT=2
```
