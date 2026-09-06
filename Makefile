# Every command in this project, in one place. Linux/macOS/CI; Windows users
# have scripts/setup.ps1 and the plain `uv run ...` lines in the README.
#
#   make setup   once
#   make check   before every commit
#   make run     the two services
#   make demo    the four-minute walkthrough

.DEFAULT_GOAL := help
.PHONY: help setup data lint fmt test check erp ui demo evals evals-live docker docker-evals k8s clean

ERP_URL ?= http://127.0.0.1:8000
UI_PORT ?= 8001

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## Create the environment from uv.lock and copy .env
	uv sync --locked
	@test -f .env || (cp .env.example .env && echo "created .env -- add your provider key")

data:  ## Regenerate the 200-scenario dataset (byte-identical to what is committed)
	uv run python -m generator.cli

lint:  ## ruff check + format check, exactly as CI runs them
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Apply the formatter and the safe fixes
	uv run ruff check --fix .
	uv run ruff format .

test:  ## The full suite
	uv run pytest -q

check: lint test  ## What CI runs. Run this before every commit.

erp:  ## Start the mock ERP on :8000
	uv run uvicorn mock_erp.app:app --reload --port 8000

ui:  ## Start the review UI on :8001 (needs the ERP)
	REVIEW_ERP_BASE_URL=$(ERP_URL) uv run uvicorn review_ui.app:app --reload --port $(UI_PORT)

demo:  ## The four-minute walkthrough (needs both services; no API key required)
	uv run python scripts/demo.py

evals:  ## Score all 200 scenarios with the rule baseline; gates on safety
	AGENT_ERP_BASE_URL=$(ERP_URL)/sap/opu/odata/sap/ZPROC_SRV \
	  uv run proc-evals --split all --mode baseline

evals-live:  ## Score the 10-case golden split against a real model and record cassettes
	AGENT_ERP_BASE_URL=$(ERP_URL)/sap/opu/odata/sap/ZPROC_SRV \
	  uv run proc-evals --split golden --mode record

docker:  ## Build and start both services in containers
	docker compose up --build

docker-evals:  ## Run the eval suite as a one-shot container
	docker compose run --rm evals

k8s:  ## Apply the Kubernetes manifests
	kubectl apply -k deploy/k8s

clean:  ## Remove caches, local state and generated reports
	rm -rf .pytest_cache .ruff_cache evals/reports traces
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.sqlite3*' -not -path './.venv/*' -delete
