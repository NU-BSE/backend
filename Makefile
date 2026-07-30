.PHONY: install infra migrate run test lint

install:
	python -m pip install -e ".[dev]"

infra:
	docker compose up -d postgres redis

migrate:
	alembic upgrade head

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest

lint:
	ruff check app tests scripts
