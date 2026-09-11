#!/usr/bin/env python3
"""Bulk-insert synthetic transfers for benchmarking.

Inserts COUNT synthetic Transfer rows via COPY (fast path). The token row is
created if missing. Re-running with a different COUNT generates fresh tx
hashes (a time-based nonce prefixes the hash), so existing data is never
overwritten or duplicated.

Usage:
    python scripts/seed.py --count 1000000
    python scripts/seed.py --count 100000 --db-url postgresql+psycopg://user:pass@host:5432/db
"""

import argparse
import os
import random
import time
from datetime import UTC, datetime
from io import StringIO

from sqlalchemy import create_engine, text

DEFAULT_DB_URL = os.environ.get(
    "DB_URL", "postgresql+psycopg://chainledger:chainledger@localhost:5433/chainledger"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1_000_000, help="rows to insert")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--token", default="0x" + "a" * 40, help="token address (created if missing)"
    )
    parser.add_argument("--token-symbol", default="SEED")
    parser.add_argument("--token-decimals", type=int, default=18)
    args = parser.parse_args()

    engine = create_engine(args.db_url)
    started = time.monotonic()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tokens (address, symbol, name, decimals) "
                "VALUES (:a, :s, :s, :d) ON CONFLICT (address) DO NOTHING"
            ),
            {"a": args.token, "s": args.token_symbol, "d": args.token_decimals},
        )

    # Block range below typical testnet heights so seeded rows sort after
    # real data in block-number DESC order (5 transfers per block).
    base_block = 100_000_000
    nonce = format(int(time.time()), "016x")
    rng = random.Random(42)
    start_ts = 1_700_000_000

    buf = StringIO()
    for i in range(args.count):
        tx_hash = f"{nonce}{i:048x}"
        block = base_block + i // 5
        log_index = i % 5
        from_address = format(rng.getrandbits(160), "040x")
        to_address = format(rng.getrandbits(160), "040x")
        value = rng.getrandbits(200)
        block_time = datetime.fromtimestamp(start_ts + block, tz=UTC).isoformat()
        buf.write(
            f"{tx_hash}\t{log_index}\t{block}\t{block_time}\t"
            f"{args.token}\t0x{from_address}\t0x{to_address}\t{value}\n"
        )

    buf.seek(0)
    with engine.raw_connection() as raw:
        cursor = raw.cursor()
        cursor.copy_expert(
            "COPY transfers (tx_hash, log_index, block_number, block_time, "
            "token_address, from_address, to_address, value) FROM STDIN "
            "WITH (FORMAT csv, DELIMITER E'\\t')",
            buf,
        )
        raw.commit()

    elapsed = time.monotonic() - started
    print(f"seeded {args.count:,} transfers in {elapsed:.2f}s "
          f"({args.count / elapsed:,.0f} rows/sec)")
    print(f"token: {args.token}  blocks: {base_block}..{base_block + args.count // 5}")


if __name__ == "__main__":
    main()
