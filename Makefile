# One entry point for humans and CI. Everything runs in the pinned toolchain.
PY      ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
export PXR_PLUGINPATH_NAME := $(CURDIR)/schema/marine

.PHONY: help setup build validate test api viewer viewer-dev docker clean

help:
	@echo "make setup      create .venv, install pinned Python + Node deps"
	@echo "make build      run the whole pipeline (generate, ingest, compose, convert, validate)"
	@echo "make validate   run validators on the current outputs"
	@echo "make test       pytest suite (round trips, validators, API)"
	@echo "make api        serve API + viewer on http://localhost:8000"
	@echo "make viewer     build the static viewer into viewer/dist"
	@echo "make viewer-dev Vite dev server for the viewer (proxies the API)"
	@echo "make docker     build and run with Docker Compose"

setup:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"
	cd build && npm ci || npm install
	cd viewer && npm ci || npm install

build:
	$(BIN)/python -m marinekit.build

validate:
	$(BIN)/python -m marinekit.validate

test:
	$(BIN)/python -m pytest -q

api: viewer
	$(BIN)/uvicorn api.app:app --host 0.0.0.0 --port 8000

viewer:
	cd viewer && npm run build

viewer-dev:
	cd viewer && npm run dev

docker:
	docker compose up --build

clean:
	rm -rf build/out viewer/dist viewer/public/data
