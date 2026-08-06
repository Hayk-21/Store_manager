"""Load the owner's price list into a store.

    python seed_catalogue.py --list-stores
    python seed_catalogue.py --store-id 2
    python seed_catalogue.py --store-id 2 --stock 0

Prices are transcribed from the owner's own sheet. A wholesale price of None is
a dash on that sheet: the model is not sold wholesale, which is a different
thing from selling it for nothing.

Re-running is safe: an item that already exists has its prices updated and its
stock left alone, because the stock is whatever the shop counted last.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from decimal import Decimal

from app.db import db
from app.logging_conf import setup_logging

# name, cost, wholesale, retail
CATALOGUE = [
    ("Lucid 20,000",          1288, 2500,  4000),
    ("Chillax 6000",          1190, 1700,  3000),
    ("Leaf Bar 8000",         1000, 2000,  3000),
    ("Bubble Moon 10K",       1680, 2500,  3500),
    ("Vanter 30,000",         1925, 2700,  5000),
    ("Aokit 50,000",          2170, 3300,  6000),
    ("Hyppe 50,000",          2611, None,  7000),
    ("Dojo 40,000",           2709, None,  7000),
    ("Smok Man 13,000",       1680, 2500,  4000),
    ("Lost Mary 10,000",      2415, 3500,  5500),
    ("Elfbar Triplex 30,000", 4032, None,  9000),
    ("Haski 13,000",          1680, 2500,  3500),
    ("Waka 1800",              994, 2000,  2000),
    ("Aryimi 18,000",         1582, 2500,  4000),
    ("Vaprive 50,000",        4130, None, 10000),
    ("Geek Bar 10,000",       1680, 2500,  3500),
]


async def list_stores() -> int:
    rows = await db.fetch(
        """
        SELECT s.id, s.name, u.telegram_username AS owner,
               (SELECT count(*) FROM items i WHERE i.store_id = s.id AND i.is_active) AS items
          FROM stores s JOIN users u ON u.id = s.owner_id
         WHERE s.is_active ORDER BY s.id
        """
    )
    if not rows:
        print("no stores yet")
        return 1
    print(f"{'id':>4}  {'store':<28} {'owner':<16} items")
    for r in rows:
        print(f"{r['id']:>4}  {r['name']:<28} @{r['owner']:<15} {r['items']}")
    return 0


async def seed(store_id: int, stock_low: int, stock_high: int, seed_value: int) -> int:
    store = await db.fetchrow(
        "SELECT id, owner_id, name FROM stores WHERE id = $1 AND is_active", store_id
    )
    if store is None:
        print(f"no active store with id {store_id}")
        return 1

    # Deterministic, so re-running quotes the same numbers rather than inventing
    # a fresh set every time somebody tries the command.
    rng = random.Random(seed_value)

    added = updated = 0
    for name, cost, wholesale, retail in CATALOGUE:
        existing = await db.fetchrow(
            "SELECT id FROM items WHERE store_id = $1 AND lower(btrim(name)) = lower($2)",
            store_id, name,
        )
        if existing:
            await db.execute(
                """
                UPDATE items SET self_price = $2, sell_price = $3, wholesale_price = $4,
                                 is_active = true, updated_at = now()
                 WHERE id = $1
                """,
                existing["id"], Decimal(cost), Decimal(retail),
                Decimal(wholesale) if wholesale is not None else None,
            )
            updated += 1
            continue

        await db.execute(
            """
            INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price,
                               wholesale_price)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            store["owner_id"], store_id, name, rng.randint(stock_low, stock_high),
            Decimal(cost), Decimal(retail),
            Decimal(wholesale) if wholesale is not None else None,
        )
        added += 1

    totals = await db.fetchrow(
        """
        SELECT count(*) AS lines, coalesce(sum(count), 0) AS units,
               coalesce(sum(self_price * count), 0) AS cost,
               coalesce(sum(possible_profit), 0) AS profit
          FROM items WHERE store_id = $1 AND is_active
        """,
        store_id,
    )
    print(f"{store['name']}: {added} added, {updated} updated")
    print(f"  {totals['lines']} lines, {totals['units']} units")
    print(f"  stock at cost {totals['cost']:,.0f} ֏, possible profit {totals['profit']:,.0f} ֏")
    return 0


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-stores", action="store_true")
    parser.add_argument("--store-id", type=int)
    parser.add_argument("--stock", type=int, help="fixed quantity for every item")
    parser.add_argument("--stock-low", type=int, default=3)
    parser.add_argument("--stock-high", type=int, default=25)
    parser.add_argument("--seed", type=int, default=7, help="makes the quantities repeatable")
    args = parser.parse_args()

    await db.connect()
    try:
        if args.list_stores or args.store_id is None:
            return await list_stores()
        low = high = args.stock if args.stock is not None else None
        if low is None:
            low, high = args.stock_low, args.stock_high
        return await seed(args.store_id, low, high, args.seed)
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
