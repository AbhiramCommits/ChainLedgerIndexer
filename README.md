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
make test                 # pytest (unit tests)
make lint                 # ruff + mypy
make run-api              # uvicorn with reload (set DB_URL/RPC_URL for localhost)
make run-indexer          # indexer entrypoint
```

Database integration tests (`tests/test_integration.py`) run when `TEST_DB_URL`
is set, e.g. against the dockerized Postgres:

```bash
docker compose exec postgres createdb -U chainledger chainledger_test
TEST_DB_URL=postgresql+psycopg://chainledger:chainledger@localhost:5433/chainledger_test uv run pytest
```

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

## Data model

- `tokens` — ERC-20 metadata (address PK, symbol, decimals, name)
- `transfers` — Transfer events; unique on `(tx_hash, log_index)` for idempotent
  writes; `value` stored as `NUMERIC(78,0)` (raw uint256, never float)
- `indexer_cursors` — per-token `last_indexed_block` for resume/resync

All addresses and tx hashes are normalized to lowercase.
