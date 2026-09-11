# chainledger

Real-time Ethereum ERC-20 `Transfer` event indexer with a REST API.

## Stack

- Python 3.11, FastAPI + SQLAlchemy 2.0 (psycopg)
- web3.py for RPC access, structlog + tenacity
- Postgres 16 via docker-compose, Alembic migrations
- Foundry `anvil` local testnet — zero external API keys required

## Quickstart

```bash
cp .env.example .env     # optional; `make up` does this for you
make up                  # starts postgres, anvil, api, indexer
make migrate             # run alembic upgrade head
curl localhost:8000/health
```

## Development

```bash
uv sync
make test                 # full suite (unit + integration; integration skips without anvil on PATH)
make test-cov             # unit suite with coverage report
make lint                 # ruff + mypy
make run-api              # uvicorn with reload (set DB_URL/RPC_URL for localhost)
make run-indexer          # indexer entrypoint
```

### Tests

The suite uses testcontainers to run a disposable Postgres 16 (migrated with
Alembic once per session, tables truncated between tests) and a `FakeWeb3`
double serving canned payloads from `tests/fixtures/*.json` — no external
services or API keys required, Docker is the only dependency.

- Unit tests target >80% coverage on `chainledger.indexer` and
  `chainledger.api` (currently ~97%).
- `tests/test_anvil_integration.py` (marked `integration`) starts a real
  `anvil`, deploys a minimal ERC-20 via web3.py, fires 3 transfers, runs one
  indexer cycle, and asserts the API returns all 3. It is skipped when
  `anvil` is not on `PATH` (CI installs it via foundry-toolchain).

Run just the integration test locally:

```bash
curl -L https://foundry.paradigm.xyz | bash && foundryup
make test
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, unit tests with coverage
gates, and the anvil integration test on push and PR.

For running the API outside docker, point it at the dockerized services:

```bash
DB_URL=postgresql+psycopg://chainledger:chainledger@localhost:5433/chainledger \
RPC_URL=http://localhost:8545 \
uv run uvicorn chainledger.api:app --reload
```

## Configuration

All settings are read from the environment (see `.env.example`):

| Variable               | Default                       | Description                                  |
| ---------------------- | ----------------------------- | -------------------------------------------- |
| `RPC_URL`              | `http://anvil:8545`           | Ethereum JSON-RPC endpoint                   |
| `DB_URL`               | see `.env.example`            | SQLAlchemy database URL                      |
| `CHAIN_ID`             | `31337`                       | Chain ID (anvil default)                     |
| `TOKENS`               | ``                            | Comma-separated ERC-20 addresses             |
| `START_BLOCK`          | `0`                           | First block to index                         |
| `CONFIRMATIONS`        | `5`                           | Blocks to wait before indexing               |
| `POLL_INTERVAL_SECONDS`| `3`                           | Poll cycle interval                          |
| `BATCH_SIZE`           | `500`                         | Max blocks per batch                         |

## Indexer

`python -m chainledger.indexer` polls for ERC-20 `Transfer` logs:

- **Cursors** — per-token `last_indexed_block` stored in `indexer_cursors`;
  advanced in the same transaction as the transfer inserts, so a crash can
  never skip data
- **Reorg buffer** — only blocks below `latest - CONFIRMATIONS` are indexed
- **Idempotency** — inserts use `ON CONFLICT (tx_hash, log_index) DO NOTHING`;
  re-indexing an already covered range is a safe no-op
- **Range bisection** — `eth_getLogs` "more than N results" errors split the
  block range in half and retry each half recursively
- **Retries** — all RPC calls go through tenacity (exponential backoff, max 5
  attempts) for transport failures, 429s, and timeouts
- **Decoding** — ERC-721 `Transfer` logs share topic0 with ERC-20 but carry 4
  topics instead of 3; they are skipped and counted in `skipped_logs`
- **Values** — raw uint256 kept as Python `int` end to end, stored as
  `NUMERIC(78,0)`; never converted to float

Batch progress is logged with structlog: `token`, `from_block`, `to_block`,
`logs_found`, `rows_inserted`, `duration_ms`.

### Local end-to-end

```bash
make up
# deploy a token on anvil (anvil account 0 private key)
docker compose cp contracts/TestToken.sol foundry:/tmp/TestToken.sol
docker compose exec foundry sh -c 'mkdir -p /tmp/fw && cd /tmp/fw && \
  forge create --rpc-url http://127.0.0.1:8545 /tmp/TestToken.sol:TestToken \
  --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 --broadcast'
# set TOKENS=<address> and CONFIRMATIONS=0 in .env, then:
docker compose up -d --force-recreate indexer
docker compose exec foundry cast send <address> "transfer(address,uint256)" \
  0x70997970C51812dc3A010C7d01b50e0d17dc79C8 1000000000000000000000 \
  --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
  --rpc-url http://127.0.0.1:8545
```

## API

REST API on `localhost:8000`. All amounts are serialized as decimal strings:
`value` is the raw uint256, `value_decimal` is formatted with the token's
decimals (never floats, so JS clients lose no precision).

| Endpoint | Description |
| --- | --- |
| `GET /health` | status, chain head, latest DB block and `blocks_behind` per token |
| `GET /tokens` | indexed tokens with symbol/decimals and transfer count |
| `GET /transfers` | filters: `token`, `from_address`, `to_address`, `address` (matches from OR to), `from_block`, `to_block`, `start_time`, `end_time`; cursor pagination via `limit` (default 50, max 500) and `cursor` |
| `GET /transfers/{tx_hash}` | all transfers in a transaction |
| `GET /addresses/{address}/balance-delta?token=...` | net inflow minus outflow computed in SQL |

- Ordering is `block_number DESC, log_index DESC`; `next_cursor` is an opaque
  base64 keyset cursor. Pagination uses keyset cursors rather than OFFSET:
  OFFSET cost degrades linearly with page depth, while the cursor rides the
  `(block_number DESC, log_index DESC)` index at constant cost per page.
- Address parameters are validated with `Web3.is_address` and normalized to
  lowercase; invalid values (and `from_block > to_block`) return 422.

**`balance-delta` caveat**: this is the net flow over the *indexed block
range* only — the sum of transfers the indexer has seen. It is NOT the
canonical on-chain balance (which would also include the indexed range's
starting balance and any events the indexer has not covered, e.g. mints
outside the configured start block).

## Backfill

Re-index a historical range, reusing the live indexer's `fetch_range` and
`upsert_transfers` paths:

```bash
python -m chainledger.backfill --token 0x... --from-block 1000 --to-block 5000 --workers 4
```

- Splits the range into chunks across a worker thread pool, with a progress
  bar and a final summary (blocks scanned, logs decoded, rows inserted, rows
  already present, skipped logs).
- Writes are `ON CONFLICT DO NOTHING`, so a second run over the same range
  inserts 0 rows; the summary asserts this explicitly when the range was
  already covered (`idempotency: PASSED`).
- `--gap-scan` finds block ranges below the indexer cursor with no coverage
  record (in `indexed_ranges`) and re-indexes only those. It needs
  `--from-block`/`--to-block` neither.

## Data model

- `tokens` — ERC-20 metadata (address PK, symbol, decimals, name)
- `transfers` — Transfer events; unique on `(tx_hash, log_index)` for idempotent
  writes; `value` stored as `NUMERIC(78,0)` (raw uint256, never float)
- `indexer_cursors` — per-token `last_indexed_block` for resume/resync
- `indexed_ranges` — coverage records per (token, block range), written in the
  same transaction as the transfers; used by backfill `--gap-scan`

All addresses and tx hashes are normalized to lowercase.
