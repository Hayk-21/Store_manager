"""Owner-side CLI. Registration is closed by design, so accounts are made here.

    python manage.py user add --email you@example.com
    python manage.py user set-password --email you@example.com
    python manage.py user list
    python manage.py user deactivate --email someone@example.com

Against production, run it through Railway so it uses the deployed variables:

    railway run --service web python manage.py user add --email you@example.com

The password is read with getpass when --password is omitted, which keeps it out
of shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.db import db
from app.logging_conf import setup_logging
from app.repo import users as users_repo
from app.repo.workers import normalise_username
from app.security import hash_password, normalise_email, password_problem


def _read_password(supplied: str | None) -> str:
    if supplied:
        return supplied
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat:   ")
    if first != second:
        raise SystemExit("passwords did not match")
    return first


def _validated(password: str) -> str:
    problem = password_problem(password)
    if problem:
        raise SystemExit(f"rejected: {problem}")
    return password


async def cmd_user_add(args: argparse.Namespace) -> int:
    """Register an owner by Telegram handle.

    No password is set: signing in means typing the handle and a code the bot
    sends. Nothing can be delivered until they press /start on the bot once,
    which is what binds their account to a chat.
    """
    handle = normalise_username(args.telegram)
    if not handle:
        raise SystemExit(
            f"{args.telegram!r} is not a Telegram username "
            f"(4-32 characters, letters, digits and _)"
        )
    if await users_repo.by_telegram_username(handle):
        raise SystemExit(f"@{handle} is already registered")

    user_id = await users_repo.create_admin(handle, args.name, normalise_email(args.email)
                                            if args.email else None)
    print(f"created user {user_id}: @{handle}")
    print(f"  -> tell them to open the bot and press Start, then sign in as @{handle}")
    return 0


async def cmd_user_set_handle(args: argparse.Namespace) -> int:
    handle = normalise_username(args.telegram)
    if not handle:
        raise SystemExit(f"{args.telegram!r} is not a Telegram username")
    row = await users_repo.by_id(args.id)
    if row is None:
        raise SystemExit(f"no user with id {args.id}")
    await users_repo.set_telegram_username(row["id"], handle)
    print(f"user {row['id']} now signs in as @{handle} (they must press Start again)")
    return 0


async def cmd_user_set_password(args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    row = await users_repo.by_email(email)
    if row is None:
        raise SystemExit(f"no such user: {email}")
    password = _validated(_read_password(args.password))
    await users_repo.set_password(row["id"], hash_password(password))
    # Changing a password must not leave old sessions alive.
    await users_repo.delete_sessions_for_user(row["id"])
    print(f"password updated for {email}; all sessions signed out")
    return 0


async def cmd_user_list(_: argparse.Namespace) -> int:
    rows = await users_repo.list_all()
    if not rows:
        print("no users yet")
        return 0
    print(f"{'id':>4}  {'telegram':<20} {'bound':<7} {'active':<7} {'email':<28} last login")
    for r in rows:
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "—"
        handle = f"@{r['telegram_username']}" if r["telegram_username"] else "—"
        print(
            f"{r['id']:>4}  {handle:<20} "
            f"{'yes' if r['telegram_id'] else 'NO':<7} "
            f"{'yes' if r['is_active'] else 'no':<7} "
            f"{(r['email'] or '—'):<28} {last}"
        )
    print('\n"bound: NO" means they have not pressed Start on the bot yet, so no')
    print("login code can reach them.")
    return 0


async def cmd_user_deactivate(args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    row = await users_repo.by_email(email)
    if row is None:
        raise SystemExit(f"no such user: {email}")
    await users_repo.set_active(row["id"], not args.undo)
    await users_repo.delete_sessions_for_user(row["id"])
    print(f"{email} is now {'active' if args.undo else 'deactivated'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="group", required=True)

    user = sub.add_parser("user", help="manage website logins").add_subparsers(
        dest="action", required=True
    )

    add = user.add_parser("add", help="register an owner by Telegram handle")
    add.add_argument("--telegram", required=True, help="e.g. @justhayk")
    add.add_argument("--name", help="display name")
    add.add_argument("--email", help="optional label; not used to sign in")
    add.set_defaults(func=cmd_user_add)

    handle = user.add_parser("set-handle", help="change which Telegram handle signs in")
    handle.add_argument("--id", type=int, required=True)
    handle.add_argument("--telegram", required=True)
    handle.set_defaults(func=cmd_user_set_handle)

    setpw = user.add_parser("set-password", help="change a password")
    setpw.add_argument("--email", required=True)
    setpw.add_argument("--password", help="omit to be prompted (recommended)")
    setpw.set_defaults(func=cmd_user_set_password)

    listing = user.add_parser("list", help="show every login")
    listing.set_defaults(func=cmd_user_list)

    deact = user.add_parser("deactivate", help="block a login")
    deact.add_argument("--email", required=True)
    deact.add_argument("--undo", action="store_true", help="reactivate instead")
    deact.set_defaults(func=cmd_user_deactivate)

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
