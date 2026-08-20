.DEFAULT_GOAL := help
.PHONY: help up verify test demo mcp shell down clean native-setup native-verify

help:  ## Show this help
	@echo "Fresh clone to a working system:"
	@echo "    make up"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---- Docker (the documented path) ------------------------------------------

up:  ## Start everything and run the full verification
	docker compose up --build

test:  ## Run the test suite in a container (no API key needed)
	docker compose run --rm app test

demo:  ## Walk one pile through the gate, interactively
	docker compose run --rm app python -m app.cli run meridian --thread demo
	@echo ""
	@echo "Now settle the items and resume:"
	@echo "  docker compose run --rm app python -m app.cli approve demo 0 --value '41%'"
	@echo "  docker compose run --rm app python -m app.cli reject  demo 3"
	@echo "  docker compose run --rm app python -m app.cli resume  demo"

mcp:  ## Run the MCP server (stdio) for an agent to connect to
	docker compose run --rm app mcp

shell:  ## Interactive shell inside the app container
	docker compose run --rm app shell

down:  ## Stop containers
	docker compose down

clean:  ## Stop containers and delete the database volume
	docker compose down -v

## ---- Native (no Docker) ----------------------------------------------------

native-setup:  ## Create a venv and install dependencies
	python3.11 -m venv .venv || python3 -m venv .venv
	./.venv/bin/pip install -q --upgrade pip
	./.venv/bin/pip install -q -r requirements-dev.txt
	./.venv/bin/python scripts/make_corpora.py
	@echo ""
	@echo "Point DATABASE_URL at a Postgres with pgvector, then: make native-verify"

native-verify:  ## Run the verification against a native Postgres
	./scripts/verify_all.sh
