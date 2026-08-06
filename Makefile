PYTHON ?= python3

.PHONY: help install-dev lint test test-core test-ingestion test-compat test-node test-macos compile build check clean

help:
	@echo "install-dev     Install editable development dependencies"
	@echo "lint            Check Python style and formatting"
	@echo "test            Run all Python and Node test suites"
	@echo "build           Build and validate wheel/sdist artifacts"
	@echo "check           Run the complete release validation"

install-dev:
	$(PYTHON) -m pip install -e ".[mcp,ingestion,dev]"

lint:
	$(PYTHON) -m ruff check src/rag_favorite tests scripts

test-core:
	PYTHONPATH=src $(PYTHON) -m pytest -q tests services/rag-app/tests

test-ingestion:
	cd ingestion && PYTHONPATH=../src $(PYTHON) -m pytest -q tests

test-compat:
	PYTHONPATH=services/rag-mcp:src $(PYTHON) -m unittest discover -s services/rag-mcp/tests
	PYTHONPATH=services/cooking-rag:src $(PYTHON) -m unittest discover -s services/cooking-rag/tests

test-node:
	node --test ingestion/openclaw-plugin/*.test.js

test-macos:
	bash -n packaging/macos/install.command
	bash -n packaging/macos/uninstall.command

test: test-core test-ingestion test-compat test-node test-macos

compile:
	$(PYTHON) -m compileall -q src ingestion/backend services scripts

build:
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

check: lint test compile build

clean:
	$(PYTHON) -c "import shutil; [shutil.rmtree(path, ignore_errors=True) for path in ('build', 'dist', 'src/rag_favorite.egg-info')]"
