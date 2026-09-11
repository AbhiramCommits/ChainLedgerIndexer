.PHONY: up down migrate test lint run-api run-indexer

up:
	@test -f .env || cp .env.example .env
	docker compose up -d --build

down:
	docker compose down

migrate:
	docker compose run --rm --no-deps api alembic upgrade head

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy chainledger

run-api:
	uv run uvicorn chainledger.api:app --reload --port 8000

run-indexer:
	uv run python -m chainledger.indexer
