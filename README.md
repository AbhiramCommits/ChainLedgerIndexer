# chainledger

Real-time Ethereum ERC-20 `Transfer` event indexer with a REST API.

Current status: scaffold only. DB models, Alembic migration, docker-compose stack
(Postgres + API + indexer + Foundry Anvil testnet), and a `/health` endpoint.
Indexing logic is not implemented yet.

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
make test                 # pytest
make lint                 # ruff + mypy
make run-api              # uvicorn with reload (set DB_URL/RPC_URL for localhost)
make run-indexer          # indexer entrypoint
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

## Data model

- `tokens` — ERC-20 metadata (address PK, symbol, decimals, name)
- `transfers` — Transfer events; unique on `(tx_hash, log_index)` for idempotent
  writes; `value` stored as `NUMERIC(78,0)` (raw uint256, never float)
- `indexer_cursors` — per-token `last_indexed_block` for resume/resync

All addresses and tx hashes are normalized to lowercase.
