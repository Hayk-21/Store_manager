"""Owner-side CLI. Accounts are made here; there is no self-registration.

    python manage.py user add --telegram @justhayk --name Hayk
    python manage.py user list
    python manage.py user set-handle --id 1 --telegram @newhandle
    python manage.py user deactivate --telegram @someone
    python manage.py user login-link --telegram @justhayk

Against production, run it through Railway so it uses the deployed variables:

    railway run --service web python manage.py user list

Signing in means a Telegram handle and a code the bot delivers, so a new account
cannot be used until that person presses Start on the bot -- a bot may only
reply to somebody who has messaged it first. `user list` shows who is bound.

`login-link` is the way back in if Telegram is unavailable: it prints a URL that
signs you in once and then stops working. It needs database access, which is why
it lives here and not on a web form.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.db import db
from app.logging_conf import setup_logging
from app.repo import users as users_repo
from app.repo.workers import normalise_username
from app.security import generate_token, hash_token

LINK_TTL_MINUTES = 15


def _handle(raw: str) -> str:
    handle = normalise_username(raw)
    if not handle:
        raise SystemExit(
            f"{raw!r} is not a Telegram username "
            f"(4-32 characters: letters, digits and _)"
        )
    return handle


async def _find(raw: str):
    user = await users_repo.by_telegram_username(_handle(raw))
    if user is None:
        raise SystemExit(f"no account for {raw}")
    return user


async def cmd_user_add(args: argparse.Namespace) -> int:
    handle = _handle(args.telegram)
    if await users_repo.by_telegram_username(handle):
        raise SystemExit(f"@{handle} is already registered")
    user_id = await users_repo.create_admin(handle, args.name)
    print(f"created user {user_id}: @{handle}")
    print("  -> tell them to open the bot and press Start, then sign in with that handle")
    return 0


async def cmd_user_set_handle(args: argparse.Namespace) -> int:
    user = await users_repo.by_id(args.id)
    if user is None:
        raise SystemExit(f"no user with id {args.id}")
    handle = _handle(args.telegram)
    await users_repo.set_telegram_username(user["id"], handle)
    print(f"user {user['id']} now signs in as @{handle} (they must press Start again)")
    return 0


async def cmd_user_list(_: argparse.Namespace) -> int:
    rows = await users_repo.list_all()
    if not rows:
        print("no users yet")
        return 0
    print(f"{'id':>4}  {'telegram':<20} {'bound':<7} {'active':<7} {'name':<20} last login")
    for r in rows:
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "—"
        label = r["display_name"] or r["telegram_name"] or "—"
        print(
            f"{r['id']:>4}  {'@' + r['telegram_username']:<20} "
            f"{'yes' if r['telegram_id'] else 'NO':<7} "
            f"{'yes' if r['is_active'] else 'no':<7} {label:<20} {last}"
        )
    print('\n"bound: NO" means they have not pressed Start on the bot yet, so no')
    print("login code can reach them.")
    return 0


async def cmd_user_deactivate(args: argparse.Namespace) -> int:
    user = await _find(args.telegram)
    await users_repo.set_active(user["id"], args.undo)
    await users_repo.delete_sessions_for_user(user["id"])
    print(f"@{user['telegram_username']} is now "
          f"{'active' if args.undo else 'deactivated'}")
    return 0


async def cmd_user_login_link(args: argparse.Namespace) -> int:
    """Mint a single-use sign-in URL, for when Telegram is not an option."""
    user = await _find(args.telegram)
    token = generate_token()
    await users_repo.create_login_link(user["id"], hash_token(token), LINK_TTL_MINUTES)
    print(f"\n  {settings.base_url}/login/link/{token}\n")
    print(f"Works once, for {LINK_TTL_MINUTES} minutes. Do not paste it anywhere shared.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="group", required=True)

    user = sub.add_parser("user", help="manage owner accounts").add_subparsers(
        dest="action", required=True
    )

    add = user.add_parser("add", help="register an owner by Telegram handle")
    add.add_argument("--telegram", required=True, help="e.g. @justhayk")
    add.add_argument("--name", help="display name")
    add.set_defaults(func=cmd_user_add)

    handle = user.add_parser("set-handle", help="change which handle signs in")
    handle.add_argument("--id", type=int, required=True)
    handle.add_argument("--telegram", required=True)
    handle.set_defaults(func=cmd_user_set_handle)

    listing = user.add_parser("list", help="show every account")
    listing.set_defaults(func=cmd_user_list)

    deact = user.add_parser("deactivate", help="block an account")
    deact.add_argument("--telegram", required=True)
    deact.add_argument("--undo", action="store_true", help="reactivate instead")
    deact.set_defaults(func=cmd_user_deactivate)

    link = user.add_parser("login-link", help="one-time sign-in URL (Telegram unavailable)")
    link.add_argument("--telegram", required=True)
    link.set_defaults(func=cmd_user_login_link)

    return parser


async def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    await db.connect()
    try:
        return await args.func(args)
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
