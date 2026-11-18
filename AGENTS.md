# AGENTS.md

Telegram bot (aiogram 3, long-polling) that sells/creates VPN subscriptions on a
Marzban-style panel. SQLite, no ORM migration tool. Celery worker + beat run
status sync, reminders and onboarding nudges.

## Commands
- Run the bot: `./.venv/bin/python main.py`
- Tests: `./.venv/bin/python -m unittest discover -s tests` (from the repo root).
  One file: `./.venv/bin/python -m unittest tests.test_unused_nudge`
- Worker / beat (need Redis): `./.venv/bin/celery -A app.celery_app worker -l info`
  and `./.venv/bin/celery -A app.celery_app beat -l info`.

Always use `.venv/bin/python`; the system `python3` has no `celery`, so importing
`tasks/` there fails. The README says `venv`, but the committed env is `.venv`.

## Config & environment
- `config/settings.py` loads `.env` via python-dotenv, walking up from the repo
  root. In this checkout `.env` lives one level above the repo root, and there is
  no `.env.example` in the tree despite the README referencing it.
- `config/plans.json` / `config/coupons.json` are config-as-code (reprice without
  code changes). Plans carry a `tier` (`vip` / `unlimited`) and optionally a
  `provider`; VIP defaults to `vip_panel`, while Unlimited uses `dante_api`.
  Read them via `settings.get_plan(id)` and
  `settings.coupon_price(plan, coupon)`.

## Architecture notes (not obvious from filenames)
- Router order in `main.py` matters: `admin_review` is included before
  `docs_support` so admin replies to support requests (matched by the
  `ReplyToOrder` filter against `orders.admin_message_id`) aren't swallowed by
  support forwarding.
- Subscription integrations live behind `services/subscriptions/`: handlers and
  Celery tasks resolve the plan/order provider instead of importing a panel
  client directly. Adding or replacing a provider means adding an adapter and a
  registry entry.
- The provider is the source of truth for status/usage/expiry. `orders` stores
  the generic `provider`, `provider_user_id`, `provider_username`, and
  `subscription_url` fields. Legacy VIP orders still use `panel_username`; do
  not decide "used/expired" from the DB — read the provider.
- Unlimited is auto-provisioned by the DANTE API on admin approval (`quota_gb=0`,
  ISO date expiry, no `configs` field). The API's generated `email` is the
  unique panel identifier stored as `provider_username`; the API `name` is the
  buyer-requested display name. A new purchase always creates a new API user;
  only the explicit renewal flow extends the selected user and supersedes the
  previous order. VIP stays on the Marzban-compatible panel.
- VIP panel client is `panel_api.py` (Marzban schema: `POST /api/admin/token`,
  create/fetch/modify users). It uses `aiohttp` and caches the token ~55 min.
- Payment screens must use `utils.CARD_NUMBER` / `utils.payment_card_line()`; the
  raw digit string renders reversed inside RTL messages.

## Conventions / gotchas
- Never call `message.edit_text(...)` / `edit_caption(...)` directly inside
  `handlers/`. Use `utils.safe_edit_text` / `utils.safe_edit_caption` (they swallow
  Telegram's "message is not modified"). `tests/test_safe_edit.py` scans `handlers/`
  and fails on raw calls.
- Schema changes need two edits: the column in `models.py` **and** an idempotent
  `ALTER TABLE` in `database._ensure_order_columns`. `Base.metadata.create_all`
  does not alter existing tables and there is no Alembic.
- Clock mismatch: `orders.created_at` is stamped by SQLite `CURRENT_TIMESTAMP`
  (UTC), while app code sets `expire_at` with local `datetime.now()`. Compare
  `created_at` against naive UTC, not local time.
- Tests are not isolated: they load the real `.env` and write throwaway rows into
  the real dev DB (`data/bot.db`, gitignored) using telegram ids like `99900x`,
  cleaning up via `db.delete_test_orders()`. Some tests require `ADMIN_USER_IDS` /
  `ADMIN_CHAT_ID` to be set. Panel calls are mocked (no network needed); don't run
  suites in parallel.
- No CI and no pytest config — unittest only.
- Commit messages follow `Type(scope): subject` (e.g. `Fix(handlers): ...`).

## Deploy
- Remote `vps` is a bare repo on the production server (branch `main`). Deploy =
  push, then on the server `git pull` and restart the bot plus
  `vpnbot-celery-worker` and `vpnbot-celery-beat`. Restart beat after editing
  `app/celery_app.py` schedules. `deploy/` ships the worker and beat systemd
  units; the bot unit is only described in the README.
