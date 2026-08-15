.PHONY: install infra run test lint typecheck check llama llama-cpu

install:
	python -m pip install -e ".[dev]"

infra:
	docker compose up -d postgres redis

# Cloud agent on llama.cpp. Detects CUDA and falls back to CPU; the first run
# downloads and converts the model, which takes a while.
llama:
	bash scripts/llama_up.sh

llama-cpu:
	LLAMA_FORCE=cpu bash scripts/llama_up.sh

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest

lint:
	ruff check app tests

typecheck:
	mypy

check: lint typecheck test
