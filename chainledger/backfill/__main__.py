import argparse
import sys

from tqdm import tqdm
from web3 import Web3

from chainledger.backfill.core import Backfiller, ChunkResult
from chainledger.config import get_settings


def _parse_token(value: str) -> str:
    token = value.lower()
    if not Web3.is_address(token):
        print(f"error: invalid token address: {value}", file=sys.stderr)
        raise SystemExit(2)
    return token


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chainledger.backfill", description="Backfill ERC-20 Transfer events"
    )
    parser.add_argument("--token", required=True, help="ERC-20 token address")
    parser.add_argument("--from-block", type=int, help="first block to index (inclusive)")
    parser.add_argument("--to-block", type=int, help="last block to index (inclusive)")
    parser.add_argument("--workers", type=int, default=4, help="worker thread count")
    parser.add_argument(
        "--gap-scan",
        action="store_true",
        help="find block ranges below the indexer cursor with no coverage record "
        "and re-index only those (--from-block/--to-block are ignored)",
    )
    return parser


def _print_summary(
    token: str, results: list[ChunkResult], blocks_scanned: int, idempotency_passed: bool
) -> None:
    logs_decoded = sum(r.rows_inserted + r.rows_already for r in results)
    rows_inserted = sum(r.rows_inserted for r in results)
    rows_already = sum(r.rows_already for r in results)
    skipped_logs = sum(r.skipped_logs for r in results)
    print("Backfill summary")
    print(f"  token:                {token}")
    print(f"  blocks scanned:       {blocks_scanned}")
    print(f"  logs decoded:         {logs_decoded}")
    print(f"  rows inserted:        {rows_inserted}")
    print(f"  rows already present: {rows_already}")
    print(f"  skipped logs:         {skipped_logs}")
    if idempotency_passed:
        # The whole range was covered before this run; ON CONFLICT DO NOTHING
        # guarantees a second pass writes nothing.
        assert rows_inserted == 0, "idempotency violation: covered range inserted rows"
        print(
            f"  idempotency:          PASSED (range already covered, {rows_already} rows skipped)"
        )
    else:
        print("  idempotency:          n/a (range was not previously covered)")


def main() -> None:
    args = _build_parser().parse_args()
    token = _parse_token(args.token)
    settings = get_settings()
    backfiller = Backfiller(settings, token, workers=args.workers)

    if args.gap_scan:
        ranges = backfiller.find_gap_ranges()
        if not ranges:
            print(f"gap scan: no gaps found for {token}")
            return
        print(f"gap scan: found {len(ranges)} uncovered range(s) for {token}")
        for f, t in ranges:
            print(f"  {f} -> {t} ({t - f + 1} blocks)")
    else:
        if args.from_block is None or args.to_block is None:
            print(
                "error: --from-block and --to-block are required (or use --gap-scan)",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if args.from_block > args.to_block:
            print("error: --from-block must be <= --to-block", file=sys.stderr)
            raise SystemExit(2)
        ranges = [(args.from_block, args.to_block)]

    idempotency_passed = all(backfiller.range_fully_covered(f, t) for f, t in ranges)
    blocks_scanned = sum(t - f + 1 for f, t in ranges)

    results: list[ChunkResult] = []
    with tqdm(total=blocks_scanned, unit="blocks", desc="backfill") as pbar:
        for result in backfiller.run(ranges):
            results.append(result)
            pbar.update(result.blocks)

    _print_summary(token, results, blocks_scanned, idempotency_passed)


if __name__ == "__main__":
    main()
