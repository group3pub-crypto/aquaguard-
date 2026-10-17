.DEFAULT_GOAL := help
SHELL := /bin/bash

# STACK selects the compose file set. `prod` (default) = docker-compose.yml + .prod.yml.
# `dev` = docker-compose.yml + .dev.yml (loopback ports, auto-reload, simulator profile).
# Nothing is picked up implicitly: there is no docker-compose.override.yml.
STACK ?= prod
COMPOSE_BIN ?= docker compose
COMPOSE ?= $(COMPOSE_BIN) -f docker-compose.yml -f docker-compose.$(STACK).yml

# Interpreters. In CI everything shares one interpreter; locally point each at its venv, e.g.
#   make test PY_BACKEND=/path/venv-backend/bin/python PY_ML=/path/venv-ml/bin/python
PYTHON ?= python3
PY_BACKEND ?= $(PYTHON)
PY_ML ?= $(PYTHON)
PY_EDGE ?= $(PYTHON)
PY_BOT ?= $(PYTHON)
PY_INFRA ?= $(PYTHON)
RUFF ?= ruff
PYPKGS := backend ml edge bot infra libs
BACKEND_PYTEST_ARGS ?= --cov=app --cov-report=term-missing

ADMIN_USER ?= admin
LOADTEST_MQTT_HOST ?= 127.0.0.1
LOADTEST_MQTT_PORT ?= 1883
LOADTEST_METERS ?= 500
LOADTEST_DURATION ?= 30

# value of NAME in ./.env (no sourcing: values may contain spaces)
envval = $$(sed -n 's/^$(1)=//p' .env | tail -n 1)

.PHONY: help env check-env install lint format typecheck test test-backend test-ml test-edge \
	test-dashboard test-bot test-infra train up down logs ps config migrate bootstrap-admin \
	provision-sensor dev-up dev-down sim-up backup backup-up restore-test loadtest \
	pin-digests clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# --------------------------------------------------------------------------- configuration
env: ## Create .env with fresh random secrets (never overwrites an existing .env)
	@sh infra/scripts/make-env.sh

check-env: ## Validate .env: placeholders, weak secrets, required settings (fails closed)
	@$(PY_INFRA) infra/scripts/check_env.py $(if $(filter dev,$(STACK)),--dev,)

install: ## Install Python and Node dependencies for local development
	$(PYTHON) -m pip install -e libs/aquaguard_features
	$(PYTHON) -m pip install -e "backend[dev]" -e "ml[dev]" -e "edge[dev]" -e "bot[dev]" pytest-cov mypy
	cd dashboard && npm ci
	$(PYTHON) -m pip install pre-commit && pre-commit install

# --------------------------------------------------------------------------- quality
lint: ## Ruff check + format check for every Python package, shellcheck (if present), dashboard lint
	$(RUFF) check $(PYPKGS)
	$(RUFF) format --check $(PYPKGS)
	@if command -v shellcheck >/dev/null; then shellcheck -S warning -s sh infra/scripts/*.sh infra/backup/backup.sh infra/backup/restore.sh infra/postgres/10-roles.sh infra/mosquitto/*.sh && shellcheck -S warning -s bash infra/backup/restore-test.sh; else echo "shellcheck not installed: skipped"; fi
	cd dashboard && npm run lint

format: ## Auto-format and fix
	$(RUFF) check --fix $(PYPKGS)
	$(RUFF) format $(PYPKGS)

typecheck: ## mypy (backend, bot; advisory) + tsc (gating)
	-cd backend && $(PY_BACKEND) -m mypy app
	-cd bot && $(PY_BOT) -m mypy bot
	cd dashboard && npx tsc --noEmit

test: test-backend test-ml test-edge test-dashboard test-bot test-infra ## Run every test suite

test-backend: ## Backend pytest (BACKEND_PYTEST_ARGS adds coverage by default)
	cd backend && $(PY_BACKEND) -m pytest $(BACKEND_PYTEST_ARGS)

test-ml: ## ML pytest
	$(PY_ML) -m pytest ml/tests

test-edge: ## Edge simulator pytest
	$(PY_EDGE) -m pytest edge/tests

test-dashboard: ## Dashboard tests
	cd dashboard && npm test

test-bot: ## Bot pytest
	cd bot && $(PY_BOT) -m pytest

test-infra: ## Deployment checks: env/compose consistency and compose file validity
	$(PY_INFRA) infra/scripts/check_env.py --lint
	$(PY_INFRA) infra/scripts/check_compose.py

train: ## Train detector + forecaster on SYNTHETIC data (writes ml/models + ml/reports)
	$(PY_ML) -m ml.scripts.train_detector
	$(PY_ML) -m ml.scripts.train_forecaster

# --------------------------------------------------------------------------- run
up: env check-env ## Production stack: Caddy TLS proxy, backend, dashboard, postgres, mosquitto
	$(COMPOSE) up -d --build
	@echo ""; echo "https://$(call envval,PUBLIC_DOMAIN)  (first user: make bootstrap-admin)"

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) --profile '*' down

logs: ## Tail logs (SERVICE=backend to filter)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

ps: ## Show service state and health
	$(COMPOSE) ps

config: ## Render the effective compose configuration (validates the files and .env)
	$(COMPOSE) config

migrate: ## Apply database migrations now (one-shot owner-role service; also runs on `up`)
	$(COMPOSE) run --rm db-migrate

bootstrap-admin: ## Create an admin user (ADMIN_USER=admin); the password is prompted on the TTY
	$(COMPOSE) exec backend python -m app.cli create-admin --username "$(ADMIN_USER)"

provision-sensor: ## Register a sensor and print its broker credentials: SOCIETY=<slug> UNIT=<n>
	@[ -n "$(SOCIETY)" ] && [ -n "$(UNIT)" ] || { echo "usage: make provision-sensor SOCIETY=<slug> UNIT=<unit>" >&2; exit 2; }
	@out=$$($(COMPOSE) exec -T backend python -m app.cli provision-sensor --society "$(SOCIETY)" --unit "$(UNIT)" 2>&1) \
	  || { echo "$$out" >&2; exit 1; }
	@secret=$(call envval,MQTT_DEVICE_SECRET); [ $${#secret} -ge 32 ] || { echo "MQTT_DEVICE_SECRET missing/short in .env" >&2; exit 1; }; \
	echo "sensor registered in the database (flat sensor_id $(SOCIETY).$(UNIT))"; \
	echo ""; echo "Broker username : $(SOCIETY)/$(UNIT)"; \
	echo "Broker password : $$(MQTT_DEVICE_SECRET=$$secret sh infra/mosquitto/device_password.sh '$(SOCIETY)/$(UNIT)')"; \
	echo ""; echo "1. add $(SOCIETY)/$(UNIT) to MQTT_DEVICES in .env"; \
	echo "2. $(COMPOSE) up -d mosquitto     # re-renders the broker password file and ACL"; \
	echo "3. flash the device (TLS, port 8883); the password above is derived from MQTT_DEVICE_SECRET," ; \
	echo "   not the one-time secret printed by the backend CLI (that scheme is for a hand-managed passwd file)"

dev-up: STACK := dev
dev-up: ## Development stack (loopback ports, plain HTTP, auto-reload) - NEVER for production
	@sh infra/scripts/make-env.sh
	@$(PY_INFRA) infra/scripts/check_env.py --dev
	$(COMPOSE) up -d --build
	@echo "dashboard http://localhost:3000   API http://localhost:8000/docs"

dev-down: STACK := dev
dev-down: ## Stop the development stack
	$(COMPOSE) --profile '*' down

sim-up: STACK := dev
sim-up: ## DEVELOPMENT/TEST TOOL, never production: start the synthetic sensor simulator
	$(COMPOSE) --profile sim up -d --build simulator

# --------------------------------------------------------------------------- data safety
backup: ## Take one encrypted database backup now (needs BACKUP_AGE_RECIPIENT)
	$(COMPOSE) --profile backup run --rm backup once

backup-up: ## Start the nightly backup container
	$(COMPOSE) --profile backup up -d --build backup

restore-test: ## Backup -> restore drill on a throw-away native PostgreSQL cluster (no Docker needed)
	PYTHON=$(PY_BACKEND) bash infra/backup/restore-test.sh

loadtest: ## Publisher-side MQTT load test against the DEV stack's loopback broker (make dev-up)
	MQTT_HOST=$(LOADTEST_MQTT_HOST) MQTT_PORT=$(LOADTEST_MQTT_PORT) \
	MQTT_USERNAME="$(call envval,MQTT_USERNAME)" MQTT_PASSWORD="$(call envval,MQTT_PASSWORD)" \
	$(PY_INFRA) infra/loadtest/load_test.py --meters $(LOADTEST_METERS) --duration $(LOADTEST_DURATION)

pin-digests: ## Print sha256 digests of the base images (needs network + buildx)
	@sh infra/scripts/pin-digests.sh

clean: ## Remove containers AND VOLUMES (data loss!) and local caches: make clean CONFIRM=yes
	@[ "$(CONFIRM)" = yes ] || { echo "this deletes all volumes (database, models, certificates); re-run with CONFIRM=yes" >&2; exit 1; }
	$(COMPOSE) --profile '*' down -v --remove-orphans
	find . -name __pycache__ -not -path './dashboard/node_modules/*' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
