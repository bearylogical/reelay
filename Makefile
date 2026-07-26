PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
CONFIG ?= config.yaml
EXAMPLE ?= config_example.yaml
MIGRATE = $(PYTHON) -m reelay.config_migrate --config $(CONFIG) --example $(EXAMPLE)

.DEFAULT_GOAL := help
.PHONY: help install test lint run miniapp-dev config-init config-check config-diff config-migrate \
        up down logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- dev ---------------------------------------------------------------

install: ## Install the package and test deps into the current environment
	$(PYTHON) -m pip install -e ".[test]"

test: ## Run the test suite
	$(PYTHON) -m pytest -q

lint: ## Byte-compile every module (same check CI runs)
	$(PYTHON) -m compileall -q reelay

run: config-check ## Run the bot (refuses if config.yaml is missing required keys)
	$(PYTHON) -m reelay

miniapp-dev: ## Serve the Mini App UI at http://127.0.0.1:8081/miniapp/ (no Telegram, no token)
	$(PYTHON) -m reelay.miniapp_dev $(ARGS)

## --- config migrations -------------------------------------------------
##
## config_example.yaml is the schema of record. These targets keep a real
## config.yaml in step with it, so a new key lands before a restart discovers
## it rather than after.

config-init: ## Create config.yaml from the example (never overwrites)
	@test ! -f $(CONFIG) || { echo "$(CONFIG) already exists -- use 'make config-migrate'"; exit 1; }
	cp $(EXAMPLE) $(CONFIG)
	@echo "created $(CONFIG) -- fill in telegram.token at minimum"

config-check: ## Report keys config.yaml is missing (exit 1 if any block startup)
	@$(MIGRATE) check

config-diff: ## Show exactly what config-migrate would add
	@$(MIGRATE) diff

config-migrate: ## Merge missing keys (with their comments) into config.yaml, backing it up first
	@$(MIGRATE) apply

## --- docker ------------------------------------------------------------

up: config-check ## Start the stack
	docker compose up -d

down: ## Stop the stack
	docker compose down

logs: ## Tail the bot's logs
	docker compose logs -f reelay
