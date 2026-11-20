.PHONY: test build up down import import-baseline smoke poc validate

test:
	GOPROXY=off go test ./internal/core
	PYTHONPATH=worker python -m unittest discover -s worker/tests -v

build:
	go build -o bin/acquisition-api ./cmd/server

up:
	docker compose up --build -d

down:
	docker compose down

import:
	python tools/import_sources.py --file data/source_registry.sample.json

import-baseline:
	python tools/import_sources.py --file data/source_registry.sample.json

validate:
	GOPROXY=off go run ./cmd/validate-registry -input data/source_registry.sample.json

smoke:
	python tools/smoke.py

poc:
	PYTHONPATH=worker python tools/poc_compare.py --config data/poc_targets.example.json --out results/poc.jsonl
