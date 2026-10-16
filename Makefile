.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE ?= docker compose
SECRET_VARS := SECRET_KEY POSTGRES_PASSWORD MQTT_PASSWORD DEMO_ADMIN_PASSWORD DEMO_RESIDENT_PASSWORD BOT_API_PASSWORD

.PHONY: help env install lint format typecheck test test-backend test-ml test-edge test-dashboard test-bot train up down logs seed demo loadtest clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

env: ## Create .env from .env.example with fresh random secrets (if absent)
	@if [ -f .env ]; then echo ".env exists, leaving it untouched"; else \
	  cp .env.example .env; \
	  for v in $(SECRET_VARS); do \
	    n=16; [ $$v = SECRET_KEY ] && n=32; \
	    sed -i.bak "s|^$$v=.*|$$v=$$(openssl rand -hex $$n)|" .env; \
	  done; \
	  sed -i.bak "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://aquaguard:$$(grep ^POSTGRES_PASSWORD= .env | cut -d= -f2)@localhost:5432/aquaguard|" .env; \
	  rm -f .env.bak; chmod 600 .env; echo "created .env with generated secrets"; fi

install: ## Install Python and Node dependencies for local development
	@for d in backend ml edge bot; do \
	  if [ -f $$d/requirements.txt ]; then pip install -r $$d/requirements.txt; \
	  elif [ -f $$d/pyproject.toml ]; then pip install -e "$$d[dev]" || pip install -e $$d; fi; \
	done
	@[ -d dashboard ] && cd dashboard && npm ci || true
	pip install pre-commit ruff && pre-commit install || true

lint: ## Ruff + dashboard lint
	ruff check backend ml edge bot infra
	ruff format --check backend ml edge bot infra
	cd dashboard && npm run lint

format: ## Auto-format and fix
	ruff check --fix backend ml edge bot infra
	ruff format backend ml edge bot infra

typecheck: ## mypy (backend, bot) + tsc
	mypy backend/app || true
	cd bot && mypy bot || true
	cd dashboard && npx tsc --noEmit

test: test-backend test-ml test-edge test-dashboard test-bot ## Run every test suite

test-backend: ## Backend pytest with coverage
	cd backend && pytest --cov=app --cov-report=term-missing

test-ml: ## ML pytest
	pytest ml/tests

test-edge: ## Edge pytest
	pytest edge/tests

test-dashboard: ## Dashboard tests
	cd dashboard && npm test

test-bot: ## Bot pytest
	cd bot && pytest

train: ## Generate synthetic data and train detector + forecaster (writes ml/models, ml/reports)
	python ml/scripts/generate_data.py
	python ml/scripts/train_detector.py
	python ml/scripts/train_forecaster.py

up: env ## Start the core stack (postgres, mosquitto, backend, dashboard)
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) --profile demo --profile bot down

logs: ## Tail logs (SERVICE=backend to filter)
	$(COMPOSE) --profile demo --profile bot logs -f --tail=100 $(SERVICE)

seed: ## Seed demo data inside the backend container
	$(COMPOSE) exec backend python -m app.seed

demo: env ## One command demo: build, start with simulator, seed, print URLs
	$(COMPOSE) --profile demo up -d --build
	@echo "waiting for backend..."; \
	for i in $$(seq 1 60); do curl -fs http://127.0.0.1:8000/health >/dev/null && break; sleep 2; done
	$(MAKE) seed
	@echo ""; echo "Dashboard: http://localhost:3000"; echo "API docs:  http://localhost:8000/docs"; \
	echo "Login: admin / DEMO_ADMIN_PASSWORD from .env"

loadtest: ## Publisher-side MQTT load test (needs MQTT_* env and aiomqtt)
	set -a; . ./.env; set +a; python infra/loadtest/load_test.py --meters 500 --duration 30

clean: ## Remove containers, volumes and local caches
	$(COMPOSE) --profile demo --profile bot down -v --remove-orphans
	find . -name __pycache__ -not -path './node_modules/*' -prune -exec rm -rf {} + 
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
