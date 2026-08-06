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
    email = normalise_email(args.email)
    if await users_repo.by_email(email):
        raise SystemExit(f"{email} already exists; use set-password to change it")
    password = _validated(_read_password(args.password))
    user_id = await users_repo.create(email, hash_password(password), args.name)
    print(f"created user {user_id}: {email}")
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
    print(f"{'id':>4}  {'email':<32} {'active':<7} {'password':<9} last login")
    for r in rows:
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "—"
        print(
            f"{r['id']:>4}  {r['email']:<32} "
            f"{'yes' if r['is_active'] else 'no':<7} "
            f"{'set' if r['has_password'] else 'unset':<9} {last}"
        )
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

    add = user.add_parser("add", help="create a login")
    add.add_argument("--email", required=True)
    add.add_argument("--password", help="omit to be prompted (recommended)")
    add.add_argument("--name", help="display name")
    add.set_defaults(func=cmd_user_add)

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
