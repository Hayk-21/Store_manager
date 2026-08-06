"""End-to-end smoke test: the bot's own client against a running web service.

This is the one test that proves the two services agree, because it uses the
real ``Api`` class rather than a hand-written request. Run it after a deploy:

    API_BASE_URL=https://your-web.up.railway.app/api/bot/v1 \
    BOT_SHARED_SECRET=... TELEGRAM_ID=123456789 python smoke.py

It sells and then voids, so it leaves the till where it found it — but it does
open and close a real shift, so point it at a dev database, not at a shop that
is trading.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

from app.api import Api, ApiError, ApiUnavailable, new_idempotency_key

TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", "0"))

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
    if not TELEGRAM_ID:
        print("set TELEGRAM_ID to a worker's telegram id registered on the website")
        return 2

    api = Api()
    lat = float(os.getenv("SMOKE_LAT", "40.177200"))
    lng = float(os.getenv("SMOKE_LNG", "44.503200"))

    try:
        print("\n1. identity")
        me = await api.me(TELEGRAM_ID)
        check("the worker is registered", me["ok"] and "worker" in me)
        print(f"       worker: {me['worker']['name']}, "
              f"salary {me['worker']['salary_per_shift']}")

        if me.get("session"):
            print("       a shift is already open; closing it first")
            await api.end_shift(TELEGRAM_ID, new_idempotency_key())

        print("\n2. opening the store")
        key = new_idempotency_key()
        opened = await api.open_store(TELEGRAM_ID, lat, lng, 20, key)
        check("a shift opened", opened["ok"] and not opened["duplicate"])
        store = opened["session"]["store_name"]
        print(f"       store: {store}, {opened['session']['distance_m']} m away")

        replay = await api.open_store(TELEGRAM_ID, lat, lng, 20, key)
        check("replaying the same key is a duplicate, not a second shift",
              replay["duplicate"] is True
              and replay["session"]["id"] == opened["session"]["id"])

        print("\n3. stock")
        listing = await api.search_items(TELEGRAM_ID, "")
        check("the stock list came back", listing["ok"])
        sellable = [i for i in listing["items"] if i["count"] > 0]
        print(f"       {listing['total']} item(s), {len(sellable)} in stock")

        if not sellable:
            print("       no stock to sell; skipping the sale checks")
        else:
            item = sellable[0]
            print(f"\n4. selling one '{item['name']}'")
            sale_key = new_idempotency_key()
            sale = await api.sell(TELEGRAM_ID, item["id"], 1, "cash", sale_key)
            line = sale["sale"]["lines"][0]
            check("the sale was recorded", sale["ok"] and not sale["duplicate"])
            check("the count went down by exactly one",
                  line["remaining_count"] == item["count"] - 1,
                  f"{item['count']} -> {line['remaining_count']}")
            check("the money is a decimal string, never a float",
                  isinstance(sale["sale"]["total"], str))

            again = await api.sell(TELEGRAM_ID, item["id"], 1, "cash", sale_key)
            check("a retried sale does not sell twice",
                  again["duplicate"] is True and again["sale"]["id"] == sale["sale"]["id"])

            print("\n5. undoing it")
            voided = await api.void_last(TELEGRAM_ID, "smoke test")
            check("the void reported the same amount",
                  voided["voided"]["total"] == sale["sale"]["total"])
            after = await api.search_items(TELEGRAM_ID, item["name"])
            restored = next(i for i in after["items"] if i["id"] == item["id"])
            check("the stock came back", restored["count"] == item["count"],
                  f"{item['count']} -> {restored['count']}")

        print("\n6. refusals are refusals, not retries")
        try:
            await api.sell(TELEGRAM_ID, 999_999_999, 1, "cash", new_idempotency_key())
            check("an unknown item is refused", False, "it was accepted")
        except ApiError as exc:
            check("an unknown item is refused", exc.code == "unknown_item", exc.code)
            check("the refusal is printable Armenian", bool(exc.human().strip()))

        print("\n7. ending the shift")
        summary = (await api.end_shift(TELEGRAM_ID, new_idempotency_key()))["summary"]
        check("the shift ended", summary["ended_at"] is not None)
        check("the salary came out of the till", Decimal(summary["salary_deducted"]) >= 0)
        print(f"       salary {summary['salary_deducted']}, "
              f"store {'closed' if summary['store_closed'] else 'still open'}, "
              f"cash {summary['store_totals_after']['cash']}")

    except ApiUnavailable as exc:
        print(f"\nCould not reach the web service: {exc}")
        return 2
    except ApiError as exc:
        print(f"\nThe server refused: {exc.code} — {exc.message}")
        return 2
    finally:
        await api.aclose()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
