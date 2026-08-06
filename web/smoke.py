"""Post-deploy sanity check against a running instance.

Deliberately tiny. The real testing is `pytest`, which can construct situations
you cannot reach by poking a live server. This only answers "did the deploy come
up and is it wired to the right database and secret".

    python smoke.py --base-url https://your-web.up.railway.app
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--bot-secret", default="", help="checks the bot API when given")
    args = parser.parse_args()

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=30, follow_redirects=False
    ) as client:
        health = await client.get("/health")
        check("/health is 200", health.status_code == 200, f"got {health.status_code}")
        check("the database is reachable", health.json().get("db") == "up", health.text)

        login = await client.get("/login")
        check("/login renders", login.status_code == 200)
        check("the page is Armenian", "Մուտք" in login.text)
        check("a CSRF cookie is issued", "vs_csrf" in login.headers.get("set-cookie", ""))

        anonymous = await client.get("/stores")
        check("signed-out visitors are redirected",
              anonymous.status_code == 303
              and anonymous.headers.get("location") == "/login")

        unauthenticated = await client.get("/api/bot/v1/me?telegram_id=1")
        check("the bot API refuses an unsigned request", unauthenticated.status_code == 401)

        if args.bot_secret:
            known = await client.get(
                "/api/bot/v1/me?telegram_id=1", headers={"X-Bot-Secret": args.bot_secret}
            )
            check("the shared secret is accepted",
                  known.status_code == 404
                  and known.json()["error"]["code"] == "unknown_worker",
                  f"got {known.status_code}: {known.text[:120]}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
