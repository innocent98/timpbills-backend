# Makefile for timpbills-backend

.PHONY: help up down logs shell test migrate seed reset-db clean install dev-install run test-cov lint format migrate-create

help:
	@echo "Timpbills backend — Docker commands"
	@echo ""
	@echo "  make up         Start api + db + redis (detached)"
	@echo "  make down       Stop all services"
	@echo "  make logs       Tail api logs (Ctrl-C to exit)"
	@echo "  make shell      Shell into api container"
	@echo "  make test       Run pytest inside api container"
	@echo "  make migrate    Apply alembic migrations"
	@echo "  make seed       Run dev seed script (creates test user)"
	@echo "  make reset-db   DESTRUCTIVE: drop + recreate db volume"
	@echo "  make clean      Remove containers + volumes (DESTRUCTIVE)"
	@echo ""
	@echo "Local dev (no Docker):"
	@echo "  make install       Install production dependencies"
	@echo "  make dev-install   Install all dependencies including dev"
	@echo "  make run           Run development server locally"
	@echo "  make test-cov      Run tests with coverage locally"
	@echo "  make lint          Run linting"
	@echo "  make format        Format code"

up:
	docker compose up -d

restart:
	docker compose restart

ps:
	docker ps

down:
	docker compose down

logs:
	docker compose logs -f api

shell:
	docker compose exec api bash

test:
	docker compose exec api poetry run pytest -v

migrate:
	docker compose exec api poetry run alembic upgrade head

seed:
	docker compose exec api poetry run python scripts/seed_dev_user.py

reset-db:
	docker compose down -v
	docker compose up -d db
	@echo "Waiting for db..."
	@sleep 5
	docker compose up -d

clean:
	docker compose down -v
	docker compose rm -f

# Local dev targets (no Docker)
install:
	poetry install --only main

dev-install:
	poetry install
	poetry run pre-commit install

run:
	poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test-cov:
	poetry run pytest --cov=app --cov-report=html --cov-report=term

lint:
	poetry run black --check app tests
	poetry run isort --check-only app tests
	poetry run ruff check app tests
	poetry run mypy app

format:
	poetry run black app tests
	poetry run isort app tests
	poetry run ruff check --fix app tests

migrate-create:
	@read -p "Enter migration message: " msg; \
	poetry run alembic revision --autogenerate -m "$$msg"
