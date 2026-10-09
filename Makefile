.PHONY: install dev api web test test-pg up down logs scale eval snapshot local-ai smoke migrate admin status reindex retry-failed backup restore acceptance psql

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements-dev.txt
	cd frontend && npm install

# Lite mode: SQLite + in-process cache/queue/worker. Needs no Docker.
api:
	. .venv/bin/activate && cd backend && uvicorn granthsetu.api:app --reload --port 8000

web:
	cd frontend && npm run dev

dev:
	$(MAKE) -j2 api web

test:
	. .venv/bin/activate && cd backend && pytest -q

smoke:
	. .venv/bin/activate && cd backend && python scripts/smoke_live.py

# Distributed mode: gateway + 2 API replicas + worker + Postgres primary/replica + Redis
up:
	docker compose up --build -d
	@echo "Open http://localhost:$${PORT:-8080}"

down:
	docker compose down

logs:
	docker compose logs -f --tail=100 api worker

scale:
	docker compose up -d --scale api=$${N:-3}

local-ai:
	docker compose --profile local-ai up -d ollama
	docker compose exec ollama ollama pull embeddinggemma

eval:
	curl -s -X POST localhost:$${PORT:-8080}/api/eval/run -H 'content-type: application/json' -d '{"use_llm":true}'

snapshot:
	. .venv/bin/activate && cd backend && python -m granthsetu.snapshot export ../data/snapshot.jsonl.gz

# ---- operations (docker compose) ---------------------------------------
migrate:
	docker compose run --rm migrate

admin:            # make admin EMAIL=you@college.edu   (prompts for the password)
	docker compose run --rm migrate python -m granthsetu.manage create-admin --email $(EMAIL)

status:
	docker compose run --rm migrate python -m granthsetu.manage status

reindex:
	docker compose run --rm migrate python -m granthsetu.manage reindex

retry-failed:
	docker compose run --rm migrate python -m granthsetu.manage retry-failed

psql:
	docker compose exec postgres-primary psql -U gs -d granthsetu

backup:           # database + private book files, into ./backups/<timestamp>/
	mkdir -p backups/$$(date +%Y%m%d-%H%M%S) && d=backups/$$(ls -t backups | head -1) && \
	docker compose exec -T postgres-primary pg_dump -U gs -d granthsetu -Fc > $$d/granthsetu.dump && \
	docker compose run --rm -v $$(pwd)/$$d:/backup --entrypoint sh migrate -c "tar -C /data -czf /backup/files.tgz files" && \
	echo "backup written to $$d"

restore:          # make restore DIR=backups/20261009-120000   (stops api/worker while restoring)
	docker compose stop api worker
	docker compose exec -T postgres-primary pg_restore -U gs -d granthsetu --clean --if-exists < $(DIR)/granthsetu.dump
	docker compose run --rm -v $$(pwd)/$(DIR):/backup --entrypoint sh migrate -c "tar -C /data -xzf /backup/files.tgz"
	docker compose start api worker
	$(MAKE) reindex

test-pg:          # tests against a real PostgreSQL (the database is wiped): make test-pg PG=postgresql://gs:pw@localhost:5432/gs_test
	. .venv/bin/activate && cd backend && PG_TEST_URL=$(PG) pytest -q

acceptance:       # workflows A-G over HTTP against a running stack; see docs/TESTING.md
	. .venv/bin/activate && cd backend && GS_URL=$${GS_URL:-http://localhost:8080} python scripts/acceptance.py setup
