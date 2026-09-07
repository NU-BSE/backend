.PHONY: install infra run test lint typecheck check llama llama-cpu keys

install:
	python -m pip install -e ".[dev]"

# Device-licensing secrets. Refuses to overwrite: regenerating either one
# breaks licences and shards already in the field.
keys:
	python scripts/generate_license_keys.py

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
