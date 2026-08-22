# Erase everything — design

A single button on the website that removes this shop's data: sales, shifts,
stock, workers, expenses, the lot. The owner asked for a confirmation, then a
five-second window in which they can change their mind, and then everything
gone — "not in the DB, not in the web, not in the Telegram bot".

This document works out what that means against the schema and the two services
as they actually are. It is a plan, not a backlog: nothing here is ranked, and
the ordering is the order an engineer would read it in.

---

## 0. The one sentence that has to be said first

**Every past conversation between the bot and a worker cannot be deleted by this
button, and no amount of engineering will change that.** A Telegram bot may only
delete messages younger than 48 hours, it needs the numeric `message_id` of each
one, and this codebase has never recorded a single message id. What can be done
is set out in §5: revoke every worker's access so the bot stops answering them,
post one final notice, and tell the owner — in Armenian, on the page, before they
press anything — that the chat history itself has to be deleted by each worker
from their own phone. Telegram's own "Delete chat → also delete for …" does that
completely and with no time limit, which the Bot API cannot.

Everything else the owner asked for is achievable.

---

## 1. What the button does, step by step

The flow, from the owner's side:

1. **`GET /settings`** — a new page (there is no settings page today; see §10).
   At the bottom, inside a `<details class="card">` block titled
   «Վտանգավոր գործողություններ», sits the button. Above it, permanently visible,
   the plain-Armenian statement of what will and will not be erased — including
   the Telegram sentence from §0.

2. **Dry run.** «Ցույց տուր, թե ինչ կջնջվի» posts to
   `POST /settings/erase/preview` and swaps in a table of row counts per table
   for this owner. No arming, no state, nothing destroyed. This is the number
   the owner checks against their own sense of the business — 4,102 receipts and
   61,340 location pings is a recognisable shop; 3 receipts is a sign they are
   logged in as the wrong account.

3. **Export.** «Ներբեռնել ամեն ինչ (JSON)» streams the whole owner's data as
   NDJSON. Optional but offered first and described as "do this before you
   erase". See §8 for the size problem.

4. **The gates.** The erase form itself carries three fields, all checked on the
   server:
   - the shop's own name, typed exactly (§8);
   - a fresh six-digit code the bot has just sent to the owner's Telegram (§8);
   - the CSRF token, as every other POST on this site already carries.

5. **`POST /settings/erase/arm`** — validates the typed name and the code, and
   returns an *arming token*: a random string with a 60-second life, stored
   server-side against the session. It validates nothing that the final POST
   will not re-validate; its only job is to make the final POST unreplayable and
   to be the thing the countdown holds.

6. **The five seconds.** The response from `arm` is a fragment showing
   «Ջնջվում է 5… 4… 3…» and one button: «Չեղարկել». At zero the fragment posts
   the arming token to `POST /settings/erase`. Cancel clears the timer, discards
   the token, and swaps the fragment back to the button. See §7 — this is the
   weakest of the protections and it is deliberately the last one.

7. **`POST /settings/erase`** — re-checks CSRF, the session, the arming token
   and its age, refuses if any store session is open (§9), logs the intent to
   stdout, deletes (§3, §4), logs the outcome with row counts to stdout, then
   best-effort sends the final Telegram notice to the workers whose ids it
   captured before the wipe (§5).

8. **Redirect 303 to `/stores`**, which is now an empty page reading «Դեռ խանութ
   չկա» — the same state a brand new account is in. The owner is still logged
   in.

The three POSTs use `Depends(require_csrf)` exactly like every other
state-changing route in `web/app/routes/pages.py`. `require_csrf` already
depends on `current_user`, so it authenticates and CSRF-checks in one dependency;
there is no role concept in this app beyond `users.is_active`, and every route is
already scoped by `user.id`, so "erase everything" naturally and only means
"erase everything belonging to `owner_id = user.id`".

---

## 2. Whose data — the multi-tenant question

**The button erases one owner's data. It never issues a `TRUNCATE`, never a
`DROP`, never a `DELETE` without `WHERE owner_id = $1`.**

`web/migrations/001_init.sql` states the rule at the top of the file: every owned
table carries `owner_id` and `UNIQUE (id, owner_id)`, and children reference
parents by the composite `(id, owner_id)` key. Sixteen tables carry `owner_id`.
Four more (`users`, `auth_sessions`, `login_codes`, `login_links`) are keyed by
`user_id` instead. One (`schema_migrations`) belongs to the deployment, not to
anybody.

Today there is one owner in production. That is not a reason to write a wipe that
assumes it. A `TRUNCATE` written today because "there is only one shop" is a
loaded gun pointed at the second shop the owner opens next year under a second
account, and at every future test database. The scoped version costs nothing
extra to write and is provably safe — §11 specifies the test that proves it.

If a second owner ever exists: their rows are untouched, their workers keep
working, their bot conversations continue, and the erasing owner's `users` row
still exists so the two accounts stay distinguishable. Nothing about the wipe
depends on being the only tenant.

**Corollary:** this is not a "delete the database" feature. If the owner ever
wants the *database* gone, that is a Neon action (§6), not a button.

---

## 3. Every table, and what happens to it

Twenty-two tables exist. Seventeen are wiped, five are kept.

| # | Table | Scoped by | Action | Why |
|---|---|---|---|---|
| 1 | `location_pings` | `owner_id` | **Wiped** | Where each worker's phone was, minute by minute. The most personal data in the system and the largest table. |
| 2 | `sale_items` | `owner_id` | **Wiped** | The lines of every receipt. |
| 3 | `cash_movements` | `owner_id` | **Wiped** | The money ledger. Everything the reports show is a SUM over this. |
| 4 | `audit_events` | `owner_id` | **Wiped** | The history of corrections. Note it is wiped *by* the erase and therefore cannot be the erase's own audit trail — see §8. |
| 5 | `till_counts` | `owner_id` | **Wiped** | Hand counts of the drawer. |
| 6 | `stock_adjustments` | `owner_id` | **Wiped** | Who changed which count, and when. |
| 7 | `write_offs` | `owner_id` | **Wiped** | Breakage. |
| 8 | `transfers` | `owner_id` | **Wiped** | Stock moved between the owner's own shops. |
| 9 | `money_transfers` | `owner_id` | **Wiped** | Cash carried between the owner's own shops. Delete before `store_sessions`: it references both the session the money left and the one it landed in. |
| 10 | `expenses` | `owner_id` | **Wiped** | Rent, advertising, restocking. |
| 11 | `sales` | `owner_id` | **Wiped** | The receipts. This is the table the owner means when they say "no info about past sales". |
| 12 | `work_sessions` | `owner_id` | **Wiped** | Every shift: who worked, from where, for how much. |
| 13 | `store_sessions` | `owner_id` | **Wiped** | Every trading period. |
| 14 | `items` | `owner_id` | **Wiped** | The catalogue, with cost and sell prices — commercially the most sensitive rows here. |
| 15 | `workers` | `owner_id` | **Wiped** | Names, Telegram ids, handles, salaries, bonus rules. Deleting these rows *is* the revocation described in §5: with no row, the bot's `/me` answers `unknown_worker`. |
| 16 | `expense_categories` | `owner_id` | **Wiped** | The owner's own category names. |
| 17 | `stores` | `owner_id` | **Wiped** | Shop names, addresses, coordinates, radius, day-start hour, `till_balance`. A factory reset, not a data purge: after this the account looks exactly like a new one. |
| 18 | `users` | — | **Kept**, one row | The owner's own account, `telegram_username`, `telegram_id`, `telegram_bound_at`, `display_name`, `language`. Without it they cannot log back in and the app is a brick. Nothing about this row is business data; it is the key to the front door. |
| 19 | `auth_sessions` | `user_id` | **Partially wiped** | Every session for this user is deleted *except the one performing the erase*. Reuse `users_repo.delete_sessions_for_user`, then re-create the current one — or add a `delete_sessions_for_user_except(user_id, token_hash)`. Rationale: a laptop left logged in somewhere else should not survive a factory reset, but signing the owner out mid-action is a needless cliff. |
| 20 | `login_codes` | `user_id` | **Wiped** for this user | Short-lived hashes; nothing is lost and a stale code should not outlive the reset. |
| 21 | `login_links` | `user_id` | **Wiped** for this user | Same. Any outstanding `manage.py user login-link` URL stops working, which is correct. |
| 22 | `schema_migrations` | — | **Untouched** | Created inline by `web/migrate.py`, not by any `.sql` file, so it is invisible to a `grep CREATE TABLE migrations/`. Deleting it makes the next deploy re-run every migration against a populated schema and fail. Never touch it. |

Two things the table does not list because they are not rows: the `distance_m()`
SQL function from `001_init.sql`, and every index and constraint. The schema
itself is not data and stays exactly as it is.

### The owner's own Telegram binding

Kept, deliberately. Login is Telegram-only since `006_telegram_only_login.sql` —
there is no password column any more, no self-service reset, and the only escape
hatch is `manage.py user login-link`, which needs a shell with database access.
Clearing `users.telegram_id` would leave the owner unable to receive a login code
and would demand somebody run `railway run --service web python manage.py …` to
get them back in. The binding is the front door key, not shop data, and it stays.

### If the owner really wants the account gone too

That is a different action and should stay a different action: `manage.py user
deactivate --telegram @x`, or a `manage.py owner erase --telegram @x --yes`
command that does the same wipe from a shell and then deletes the `users` row.
A button on a website that logs itself out permanently is a button nobody can
undo a mistake with.

---

## 4. Delete order, and why not the cascade

### The cascade does not work

The obvious implementation is one statement:

```sql
DELETE FROM users WHERE id = $1;   -- DO NOT DO THIS
```

Fourteen tables carry `owner_id … REFERENCES users (id) ON DELETE CASCADE`, so
this looks like it would clear everything. It has three problems and any one of
them is disqualifying.

**It deletes the login.** §3 — the account has to survive.

**Four foreign keys are `ON DELETE RESTRICT`, and `RESTRICT` cannot be
deferred.** These are the ones:

| FK | File | Effect |
|---|---|---|
| `store_sessions.opened_by_worker_id → workers` | `001_init.sql:179` | Blocks deleting a worker who ever opened a shop. |
| `sales.voided_by_worker_id → workers` | `001_init.sql:262` | Blocks deleting a worker who ever voided a receipt. |
| `sale_items.item_id → items` | `001_init.sql:285` | Blocks deleting a product that was ever sold. |
| `write_offs.item_id → items` | `017_write_offs.sql:33` | Blocks deleting a product that was ever written off. |

`RESTRICT` is checked immediately when the referenced row is deleted; `NO ACTION`
is the one that defers to end of statement. Inside a single cascading `DELETE`,
whether the referencing rows happen to have been removed by an earlier queued
referential trigger is a function of trigger firing order, which is not something
to build a destructive feature on. It may pass on an empty-ish shop and fail on a
real one. Note that `web/tests/test_till_concurrency.py` already has a
`_forget(owner_id)` helper doing exactly this `DELETE FROM users` — it works
there only because that test's fixture never creates a sale.

**Eight foreign keys are `ON DELETE SET NULL`, which orphans rather than
deletes.** `write_offs` and `till_counts` are the two that behave unusually and
are worth stating explicitly, because they are the tables that would silently
survive a partial cascade:

- `write_offs` references `items` with **RESTRICT** but references `worker_id`,
  `work_session_id` and `store_session_id` with **SET NULL** (`017`). So deleting
  store sessions does not delete write-offs — it blanks their link and leaves
  rows saying "someone wrote off 4 of something, cost 12,000" floating with no
  shift attached. And deleting items is *blocked* by them.
- `till_counts` references `stores` with CASCADE but `store_session_id`,
  `work_session_id` and `worker_id` all with **SET NULL** (`022`). Deleting store
  sessions orphans every drawer count instead of removing it.
- Same SET-NULL shape on `stock_adjustments` (`020`), `transfers`'
  `requested_by_worker_id` / `decided_by_worker_id` (`021`), `money_transfers`'
  `sent_by_worker_id` / `decided_by_worker_id` (`031`), `expenses.store_id`
  and `expenses.category_id` (`008`), `sales.superseded_by_sale_id` (`009`), and
  `audit_events.user_id` / `reverted_by` (`010`).

The conclusion: **one explicit `DELETE FROM … WHERE owner_id = $1` per table, in
dependency order.** It is seventeen lines, every one of them readable, and it does
not depend on trigger ordering, on cascade semantics, or on nobody adding a new
FK next year.

### The order

Leaves first, roots last:

```
 1. location_pings
 2. sale_items
 3. cash_movements
 4. audit_events
 5. till_counts
 6. stock_adjustments
 7. write_offs
 8. transfers
 9. money_transfers
10. expenses
11. sales
12. work_sessions
13. store_sessions
14. items
15. workers
16. expense_categories
17. stores
```

Checking the two RESTRICTs that bite: `sales` (11) is deleted before `workers`
(15), so `sales.voided_by_worker_id` is clear. `store_sessions` (13) is deleted
before `workers` (15), so `opened_by_worker_id` is clear. `sale_items` (2) and
`write_offs` (7) are both gone before `items` (14). Every SET-NULL parent is
deleted after its children, so no orphan is ever produced.

Each statement returns its row count from the `DELETE n` command tag — collect
them, they are what gets logged (§8) and what the test in §11 asserts on.

Every step is `WHERE owner_id = $1` and therefore **idempotent**: re-running the
whole thing on an already-erased owner deletes zero rows and succeeds. That
matters for §9.

### Sequences

**Do not restart any sequence.** `TRUNCATE … RESTART IDENTITY` is the pattern in
`web/tests/test_concurrency.py`, and it is right there because that fixture owns
the whole database. Here it would be wrong twice over: `RESTART IDENTITY` cannot
be scoped to an owner, and resetting `sales_id_seq` to 1 while a second owner
still holds `sales.id = 1` makes their next insert fail on the primary key.

Ids restarting does not matter to anybody. Nothing in the UI shows a raw id as a
meaningful number, ids are not URLs anybody has bookmarked (all owner-scoped
routes 404 for rows that no longer exist), and after the wipe the owner's own new
data simply starts at whatever the sequence had reached. The only cosmetic
consequence — the first new receipt being `#40219` — is invisible, because the
receipt id is never printed.

---

## 5. The Telegram side, honestly

### What the API actually allows

`bot/requirements.txt` pins `python-telegram-bot[job-queue]==21.9`. Verified
against the installed source in `bot/.venv/Lib/site-packages/telegram/`:

- `Bot.delete_message(chat_id, message_id)` exists.
- `Bot.delete_messages(chat_id, message_ids)` exists (added in PTB 20.8).
  `telegram.constants.BulkRequestLimit.MAX_LIMIT = 100` — a hundred ids per call.
- The docstring on `delete_message` lists the limits verbatim: *"A message can
  only be deleted if it was sent less than 48 hours ago"*, and *"Bots can delete
  incoming messages in private chats"*.

That second line is worth getting right rather than repeating the folklore. In a
**private chat** — which is the only kind this bot ever has — the bot may delete
both its own outgoing messages **and** the worker's incoming ones. Authorship is
not the barrier. **The 48-hour age limit is the barrier**, and it applies to
everything.

### What this codebase stores

Nothing. Grepping `bot/app/` for `message_id` returns zero hits; the only state
the handlers keep is `context.user_data[...]` for flows in progress (`sell_item`,
`co_qty`, `rs_page`, and so on). No table has a message-id column. The web
service's `app/services/telegram.py` has `send_message` and nothing else — it
never records what it sent.

So today the bot could not delete a single past message even inside the 48-hour
window, because it does not know a single message id.

### The consequence

**"Every conversation of the bot with workers" cannot be removed.** Not for old
messages, not for recent ones, not for any of them. This must be on the page in
Armenian, above the button, before the owner presses it — not discovered
afterwards.

### The closest thing that can be done

Four parts, in this order:

1. **Revoke access — free, and already implied by the wipe.** Deleting the
   `workers` rows removes both `telegram_id` and `telegram_username`. The next
   thing any worker does in the bot hits `GET /api/bot/v1/me`, which raises
   `BotError("unknown_worker")` at `web/app/routes/bot_api.py:91`, and the bot
   prints the server's Armenian sentence verbatim: «Դուք գրանցված չեք
   համակարգում։ Դիմեք ղեկավարին։» From that moment the bot answers nothing
   useful to anybody. No extra code.

2. **One final notice.** Collect every live `workers.telegram_id` for this owner
   *before* the deletes, then after the transaction commits, send each of them
   one message with the existing `app/services/telegram.py::send_message`. Send
   after the commit, never before — telling a worker the shop's data is gone and
   then having the transaction roll back is the one ordering that lies. Failures
   are logged and ignored; a blocked bot must not turn a successful erase into a
   500. Wording, roughly:

   > 🗑 Այս խանութի տվյալները ջնջվել են։ Բոտն այլևս չի պատասխանի ձեր հարցումներին։
   > Ձեր կողմից նամակագրությունը ջնջելու համար՝ բացեք այս զրույցը → «Ջնջել
   > զրույցը» → նշեք «Ջնջել նաև … համար»։

3. **Tell the owner what is left to do.** The one instruction that genuinely
   finishes the job: each worker opens the chat with the bot, chooses *Delete
   chat*, and ticks *also delete for …*. In a private chat Telegram's own client
   deletes the history for both sides with **no 48-hour limit** — it is a user
   action, not a Bot API call, which is exactly why the bot cannot do it. The
   owner should do the same in their own chat with the bot, where their login
   codes are.

4. **Optional, and only useful going forward: start recording message ids.** A
   small `bot_messages (owner_id, chat_id, message_id, sent_at)` table written by
   a wrapper around every send and every incoming update would let a future erase
   call `delete_messages` in batches of 100 for everything under 48 hours old. It
   is a real improvement and it is honestly small — but it is a new table, a new
   write on the hot path of every bot interaction, and it cannot touch anything
   sent before it ships. Worth doing only if the owner expects to use this button
   more than once. It is not part of the first version.

### What the bot keeps outside Postgres

- **Conversation state**: `context.user_data` and `context.chat_data`, in the
  `Application`'s in-memory dicts. Grepping `bot/app/` for `persistence`,
  `PicklePersistence`, `bot_data` and `chat_data` returns **zero hits** — no
  persistence is configured, so nothing is on disk and nothing survives a
  restart.
- **Job queue**: `python-telegram-bot[job-queue]` brings APScheduler in, used
  only for the `conversation_timeout` on each `ConversationHandler`. In memory,
  dies with the process.
- **Configuration**: `bot/app/config.py` holds three secrets and two timeouts.
  As the README says, the bot has no store list, no coordinates, no radius, no
  names — the server geofences. There is nothing there to erase.
- **Transport**: long polling, not a webhook —
  `run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)` in
  `bot/app/__main__.py`, one replica by necessity.

**So: restart the bot service after the erase.** A worker mid-write-up holds a
half-finished basket in that process's memory; a restart drops it, and
`drop_pending_updates=True` additionally discards anything Telegram queued while
the process was down. On Railway this is a service restart, done by hand from the
dashboard. Not restarting is not dangerous — every one of those in-flight actions
would fail with `unknown_worker` on submit anyway — but the state is genuinely
still in RAM until you do, and the plan should not pretend otherwise. The web
service does not need restarting: it holds no per-owner state beyond the
connection pool.

---

## 6. What survives regardless

An empty live database is not the same as the data ceasing to exist. The owner
should be told this before pressing, not after.

**Neon point-in-time history.** The README's deployment section has the two Neon
quirks the code handles but not this one: Neon retains a WAL history window per
project and can restore or branch to any moment inside it. The window is a
project setting and depends on the plan — one day by default on the free tier,
longer on paid ones. Until that window rolls past the moment of the erase, **the
data is fully restorable by anybody with Neon console access**, this button
notwithstanding. For a real erasure the owner has to, in the Neon console:
reduce the history-retention setting so the window expires past the erase
timestamp, delete any branch created from a restore point (branches persist
independently of the retention window), and, if legal erasure is the point rather
than a factory reset, raise a support request asking for confirmation that
physical copies are gone. None of that can be done from this application.

**Railway.** No Postgres is hosted there — the data lives in Neon — so Railway
holds no database backup. It does hold service logs and build logs for a
plan-dependent window of days, and §8 deliberately writes the erase record into
those logs. Those log lines contain counts and ids only, never business data, but
`log.exception` tracebacks elsewhere in the app can carry query text. Nothing
logs a login code (`services/login.py` logs "code sent to user %s", never the
code) and the bot suppresses `httpx` INFO logging specifically because the URL
carries the bot token.

**Telegram's servers.** Every message ever exchanged with every worker, until
each of them deletes their own chat (§5). Telegram's retention is Telegram's
business and no API here reaches it.

**Anything already out of the system.** Any JSON export downloaded from §1 step
3, any screenshot, any figure copied into a message. The export is offered
precisely because the owner will usually want one — and it is then a copy of
everything, sitting in their Downloads folder, outside the reach of this button.

---

## 7. The five-second window

### Recommendation: client-side countdown

The confirm fragment renders a counter, the POST fires at zero, and «Չեղարկել»
clears a `setTimeout` and swaps the fragment back. Roughly fifteen lines of
inline JavaScript in the fragment, plus an arming token that expires in 60
seconds so the POST cannot be replayed later from a stale tab.

Why this one:

- **There is no job runner.** `web/app/main.py` has exactly one background task,
  a hand-rolled `_housekeeping()` loop that sleeps `HOUSEKEEPING_INTERVAL_S =
  3600` and then auto-closes stale sessions and purges expired auth sessions.
  There is no APScheduler, no Celery, no arq, no jobs table. A server-side timer
  means inventing all of that infrastructure for a five-second delay.
- **A restart mid-window has no good answer.** Railway redeploys, Neon suspends
  the compute after ~5 minutes idle on the free tier. If the process dies during
  the window, a server-side job either fires on the next boot — erasing the shop
  minutes or hours after the owner walked away from a tab they thought they had
  closed — or is dropped, which is a five-second timer that silently did nothing.
  Neither is defensible.
- **It matches the codebase.** Confirmations here are `window.confirm` via
  `onsubmit="return confirm(…)"` (a dozen places) or `hx-confirm` (two places).
  There is no modal partial, no `<dialog>`, no HX-Redirect anywhere. A small
  inline countdown in an HTMX-swapped fragment is the smallest step from what
  already exists.

The honest costs, stated rather than hidden: closing the tab cancels the erase
(which is fine — the owner did not lose data, they just have to press it again),
and once the request is in flight nothing can stop it (which is also fine — the
request takes well under a second to reach the server, and the transaction is the
point of no return in either design).

### The alternative, and when it would be right

A server-side armed job: a row recording "armed at T by user U with token X", a
task that fires at T+5s, and cancel deleting the row. It survives a closed tab,
it is auditable, and it is the correct design **if the cancel has to work from a
different device** — "I started it on the laptop and stopped it from my phone".
For a five-second window that scenario does not exist; nobody unlocks a phone in
five seconds. It would need a migration, a task, and a documented answer for a
restart mid-window, all to buy a property nobody can use.

If the owner ever asks for a *longer* delay — twenty-four hours, say, which is a
genuinely good idea for a feature like this — then the server-side design becomes
the right one and this note is where to start.

### And the thing to say out loud

**Five seconds is a UX nicety, not a safety mechanism.** It catches the misclick
and nothing else. It does not catch the wrong account, the borrowed laptop, the
misunderstanding about what "everything" includes, or the regret that arrives on
Tuesday. Those are §8, and §8 is where the effort belongs. If a reviewer has to
choose between the countdown and any single gate in §8, they should drop the
countdown.

---

## 8. The real safety gates

### Type the shop's name

Not «Այո» on a dialog. The form requires a text field matching exactly, compared
on the server after `strip()` and case-sensitively:

- one store → its `stores.name`, shown on the page in a `<code>` block;
- several stores → the page names one of them explicitly ("type the name of
  «Կենտրոն»") and requires that one;
- no stores at all → the owner's own handle, `users.telegram_username`, since
  there is no shop name to type.

Server-side, always. A client-only check is a JavaScript-disabled bypass, and
this is the wrong feature to have one of.

### Re-enter the login flow

A borrowed unlocked laptop should not be able to do this. Before arming, the
owner requests a fresh six-digit code over Telegram and types it — the same
mechanism as `/login`, which already exists in `app/services/login.py`
(`request_code` / `verify_code`), is already rate-limited by
`settings.login_codes_per_hour`, and already burns after
`settings.max_code_attempts` wrong guesses.

**One schema conflict to resolve.** `005_login_by_telegram.sql` creates
`one_live_login_code_per_user` — a partial unique index allowing exactly one
unconsumed code per user. Reusing `login_codes` for a step-up would therefore
retire any live login code, and vice versa. The fix is one migration:

```sql
-- 0NN_erase_step_up_codes.sql  (next free number; 029 is already claimed by
-- the in-flight reporting-index work, so this is 030 unless that lands first)
ALTER TABLE login_codes ADD COLUMN purpose text NOT NULL DEFAULT 'login'
    CHECK (purpose IN ('login', 'erase'));
DROP INDEX one_live_login_code_per_user;
CREATE UNIQUE INDEX one_live_login_code_per_user_purpose
    ON login_codes (user_id, purpose) WHERE consumed_at IS NULL;
```

and namespacing the HMAC in `app/security.py::hash_code` by purpose
(`f"{purpose}:{code}"`) so an erase code can never be replayed as a login code.
This is the only migration the feature needs.

### Export first

A `GET /settings/export` streaming NDJSON — one JSON object per row, one section
per table, via `StreamingResponse` and an asyncpg cursor. Not a single
`json.dumps` of everything: the pool sets `command_timeout=15.0` in
`web/app/db.py`, and both the timeout and the memory are real limits on the
largest tables.

Sizing, from the shape of the schema rather than a guess at the business:
`sales`, `sale_items` and `cash_movements` are of the order of a few hundred
bytes per row and a few tens of thousands of rows per year for a busy counter —
call it single-digit megabytes per year, which is nothing.
**`location_pings` is the outlier.** It takes one row per live-location reading
while every shift is open (`012_live_location.sql`); at a reading every few
seconds across a ten-hour shift that is thousands of rows per worker per day, and
it will dominate both the export and the delete by an order of magnitude or two.
Recommendation: exclude `location_pings` from the export by default with an
explicit "include the location history too" checkbox, and always delete it in
batches (below). The export offer should print the dry-run row counts beside it
so the owner knows what they are about to download.

### Dry run

`POST /settings/erase/preview` — one query, one row per table:

```sql
SELECT 'sales' AS t, count(*) FROM sales WHERE owner_id = $1
UNION ALL SELECT 'sale_items', count(*) FROM sale_items WHERE owner_id = $1
UNION ALL …
```

Sixteen branches, all index-supported, cheap. It changes nothing and can be run
as often as the owner likes. It is also the thing the final log line is checked
against, and the thing the test in §11 compares to the actual deleted counts.

### CSRF and rate limiting

CSRF is solved: `Depends(require_csrf)` on all three POSTs, exactly as every
other destructive route in `pages.py` already does. `require_csrf` checks
same-origin via `Origin`/`Referer` and constant-time-compares the
`X-CSRF-Token` header or `csrf_token` form field against `user.csrf_token`.

Rate limiting comes free from the step-up code: `login_codes_per_hour` caps how
often a code can be requested, and `max_code_attempts` burns the code on repeated
wrong guesses. The 60-second arming-token life caps replay. No new limiter is
needed.

### Log it where the erase cannot reach

`audit_events` is table #4 on the delete list. It cannot be the record of its own
deletion. The record goes to **stdout**, which on Railway means the service log,
which this application cannot write to and cannot delete:

```python
log.warning("ERASE ARMED owner=%s user=%s ip=%s stores=%s", …)
log.warning("ERASE DONE owner=%s user=%s rows=%r elapsed_ms=%s", owner_id, user.id, counts, ms)
```

Counts and ids only — never a product name, never an amount, never a worker's
name. The line exists to answer "did this happen, when, and how much went" for
somebody reading the logs a week later, not to preserve what was erased.
Retention is Railway's, and it is days, not forever (§6).

---

## 9. Failure modes

### Half a wipe

**One transaction, for fifteen of the sixteen tables.** `web/app/db.py` provides
`async with db.transaction() as conn:`, and the deletes go inside it with
`conn.execute(...)` — not `db.execute(...)`, which would leave the transaction.
Sixteen statements against one owner's rows is a small unit of work for Postgres;
all-or-nothing is achievable and is the right guarantee.

**`location_pings` is the exception, and is purged first, outside the
transaction, in batches.** The reason is `command_timeout=15.0` on the pool: a
single `DELETE` of a million-plus ping rows can exceed it and raise
`asyncpg.QueryCanceledError`, taking the whole transaction with it. Two options,
either acceptable:

- delete in batches of, say, 50,000 with `DELETE FROM location_pings WHERE ctid
  IN (SELECT ctid FROM location_pings WHERE owner_id = $1 LIMIT 50000)` in a
  loop before opening the transaction; or
- pass an explicit per-call `timeout=` to `conn.execute` for that one statement,
  overriding the pool default.

The batch loop is preferable because `location_pings` is the only table nothing
else references — deleting half of them breaks no invariant and produces no
orphan. A partly-purged ping table is a table with fewer pings in it, and the
next run finishes the job.

**If the transaction fails anyway** — the connection drops, Neon suspends, the
timeout fires — Postgres rolls it back and the shop is exactly as it was. The
route returns the standard `error.html` and the owner can try again. Because
every statement is `WHERE owner_id = $1`, the operation is idempotent: re-running
after any failure, partial or total, converges on the same end state. Say so on
the page: «Կրկին սեղմեք» is a complete recovery procedure.

**What is not transactional:** the Telegram notices (§5, sent after commit,
best-effort, failures logged) and the bot process restart (manual). Neither can
leave the database inconsistent.

### A worker's shift disappearing mid-service

If a store session is open when the erase runs, a cashier is standing at a
counter. Their shift row vanishes; their next tap gets `unknown_worker` and the
Armenian «Դուք գրանցված չեք համակարգում» — which, at that instant, is confusing
rather than informative. Worse, any sale they entered in the minutes before is
gone with no record that it was ever made, and the cash in the drawer no longer
corresponds to anything.

**The erase refuses while any store session is open.** The check:

```sql
SELECT count(*) FROM store_sessions WHERE owner_id = $1 AND closed_at IS NULL
```

If it is not zero, `POST /settings/erase` raises
`AppError("validation_error", "Նախ փակեք բոլոր բաց աշխատաշրջանները։")` and the
page lists which shops are open with links to them.

**It does not force-close.** Force-closing runs the whole close path — pays
salaries out of the till, writes `cash_movements` rows, snapshots
`cash_at_close`, settles bonuses — and every one of those rows is about to be
deleted anyway. It would be the application making money decisions on the
owner's behalf at the exact moment they have declared they no longer care about
the books, and the worker would be paid or not paid by a side effect of a delete
button. That is not a decision this feature should take.

**But refusing must not be a dead end.** A stale open session — a worker who went
home without closing — would otherwise block the button forever. The settings
page therefore shows each open session with the existing owner-side close control
(`POST /stores/{id}/close`, already implemented, already reachable from
`_store_status.html`), so closing them is one deliberate click each and then the
erase unlocks. Two explicit actions, not one action with a hidden second half.

Note that `_housekeeping()` also auto-closes sessions older than
`settings.auto_close_hours` (16), so a truly abandoned session resolves itself
within a day without anybody doing anything.

### Erase while the owner has another tab open

Harmless. The other tab's next HTMX poll — `/partials/footer` every 10s from
`base.html`, `/partials/stores/{id}/status` every 10s from `store_detail.html` —
renders empty totals or 404s into `error.html`. Their session cookie still works
(§3, table row 18). No special handling needed.

---

## 10. Implementation checklist

### New migration

- `web/migrations/0NN_erase_step_up_codes.sql` — the `login_codes.purpose` column
  and the reworked partial unique index (§8). This is the only schema change the
  feature needs. Append-only, no down-migration, per the convention in
  `web/migrate.py`.

### New files

- `web/app/repo/erase.py`
  - `counts(owner_id) -> dict[str, int]` — the dry run.
  - `purge_location_pings(owner_id, batch=50_000) -> int` — the batched pre-pass.
  - `wipe(conn, owner_id) -> dict[str, int]` — the fifteen ordered deletes,
    returning row counts. Takes `conn` first, following the existing convention
    for transactional repo helpers (`write_offs_repo.delete_for_session(conn,
    owner_id, …)` and friends, used by `delete_report` in `pages.py`).
- `web/app/services/erase.py`
  - `preview(owner_id)`, `open_sessions(owner_id)`, `arm(user, typed_name, code)`,
    `run(user, token)`. Owns the guards, the ordering, the logging and the
    post-commit notices. Nothing outside this module deletes across tables.
- `web/app/templates/settings.html` — the page. The danger block copies the shape
  of the existing `<details class="card">` + `.danger-zone` + `button.danger` at
  `store_detail.html:76-145`.
- `web/app/templates/_erase_preview.html` — the dry-run table fragment.
- `web/app/templates/_erase_countdown.html` — the five-second fragment, with the
  inline `setTimeout` and the cancel button.
- `web/tests/test_erase.py` — see below.

### Edited files

- `web/app/routes/pages.py` — `GET /settings`, `POST /settings/erase/preview`,
  `POST /settings/erase/code`, `POST /settings/erase/arm`, `POST /settings/erase`,
  `GET /settings/export`. All the POSTs take `Depends(require_csrf)`; the final
  one returns `RedirectResponse("/stores", status_code=303)`, matching the
  existing destructive routes (there is no `HX-Redirect` precedent in this
  codebase — do not introduce one here).
- `web/app/templates/base.html` — a «Կարգավորումներ» nav entry, and
  `active="settings"`.
- `web/app/services/login.py` — `request_code`/`verify_code` grow a `purpose`
  parameter defaulting to `"login"`.
- `web/app/repo/users.py` — `purpose` threaded through `replace_login_code`,
  `live_login_code`, `codes_issued_since`; plus
  `delete_sessions_for_user_except(user_id, token_hash)`.
- `web/app/security.py` — `hash_code(code, purpose="login")`, namespacing the
  HMAC.
- `web/app/i18n.py` — English translations for the new Armenian strings, per the
  Armenian-as-key convention.
- `README.md` — a section documenting the button, and the §6 retention reality.
  (While there: the "Managing accounts" section still documents
  `manage.py user add --email` and `set-password`, both removed by
  `006_telegram_only_login.sql`. Not this feature's job, but a reader of that
  section will be misled about how login works.)

### Optionally

- `web/manage.py` — an `owner erase --telegram @x --yes` subcommand calling the
  same service, for the case where the website is unreachable or the account
  itself is going. Follows the existing `async def cmd_*(args) -> int` +
  `set_defaults(func=…)` pattern.

### Not touched

- `bot/` — nothing. The revocation falls out of deleting the `workers` rows, and
  the notice is sent by the web service through `app/services/telegram.py`. The
  only bot-side work is the optional message-id recorder in §5.4, which is a
  separate piece of work with its own table and its own decision.

### Tests

`web/tests/conftest.py` gives each test one connection inside a transaction that
is always rolled back, with `db.bind(connection)` routing the app through it —
so all of these run without committing anything.

1. **`test_second_owner_survives`** — the one that matters most. Build two owners
   with a full fixture each (stores, workers, sessions, sales, sale items, cash
   movements, write-offs, adjustments, till counts, transfers, expenses, audit
   events, pings). Erase owner A. Assert every one of the sixteen tables has zero
   rows for A **and** its original non-zero count for B. If this test does not
   exist, the feature does not ship.
2. **`test_every_owned_table_is_emptied`** — enumerate the tables from the
   database, not from a list in the test:
   ```sql
   SELECT table_name FROM information_schema.columns
    WHERE column_name = 'owner_id' AND table_schema = 'public'
   ```
   then assert `count(*) WHERE owner_id = A` is zero for each. This is the test
   that catches migration 030 adding a table that nobody added to `erase.py` —
   a hardcoded list in the test would pass while the wipe silently leaked.
3. **`test_login_survives`** — after the erase the `users` row still exists with
   its `telegram_id` and `telegram_username`, the acting `auth_sessions` row is
   still valid, other sessions for that user are gone, and `GET /stores` returns
   200.
4. **`test_idempotent`** — running the erase twice succeeds, and the second run
   reports all-zero counts.
5. **`test_refuses_with_open_store_session`** — with a session open, the POST
   raises `validation_error` and **every row is still there**. Assert the counts,
   not just the status code.
6. **`test_wrong_shop_name_deletes_nothing`** — and `test_missing_code_deletes_
   nothing`, `test_stale_arming_token_deletes_nothing`, and
   `test_no_csrf_deletes_nothing`. Each asserts unchanged row counts. The point
   of a gate is that it holds, not that it returns 4xx.
7. **`test_sequences_are_not_restarted`** — note the max `sales.id` before the
   erase, erase, insert a new sale, assert the new id is greater. This is the
   test that would fail if somebody "tidied up" by adding `RESTART IDENTITY`.
8. **`test_preview_matches_what_is_deleted`** — the dry-run counts equal the
   `DELETE n` counts the wipe reports, table by table.
9. **`test_schema_migrations_untouched`** — the row count in `schema_migrations`
   is the same before and after.
10. **`test_erase_by_owner_a_cannot_be_triggered_for_owner_b`** — post the erase
    as owner A with owner B's ids in every field that takes one. Nothing of B's
    moves. (Every route is scoped by `user.id`, so this should be structurally
    impossible — the test is there so it stays impossible.)

`web/tests/test_tenancy.py` is the existing home for isolation tests and the
right model to copy; `web/tests/factories.py` already has `make_owner`.

---

## 11. Summary of what the owner gets, in their terms

- **A button, with a confirmation, a typed shop name, a fresh Telegram code, and
  a five-second cancellable countdown** — all four, not just the countdown.
- **Everything about the business gone from the database**: sales, receipts,
  shifts, workers, products and prices, expenses, transfers, drawer counts,
  breakage, corrections, location history, and the shops themselves. Sixteen
  tables, one transaction, all or nothing.
- **The login still works.** They can sign back in with Telegram the same as
  always, and land on an empty app.
- **The bot stops answering every worker immediately.** Deleting the workers is
  the revocation; there is nothing left for the bot to recognise.
- **The bot's past conversations are not deleted, and cannot be.** The bot sends
  each worker one final notice with the instruction for deleting the chat from
  their own phone, which does work completely. That instruction is the honest
  answer to that part of the request.
- **The data still exists in Neon's history for as long as the retention window
  runs.** A separate action in the Neon console, described on the page, is what
  makes that go away.
