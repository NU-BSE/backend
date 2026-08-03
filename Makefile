.PHONY: install infra run test lint typecheck check

install:
	python -m pip install -e ".[dev]"

infra:
	docker compose up -d postgres redis

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest

lint:
	ruff check app tests

typecheck:
	mypy

check: lint typecheck test
