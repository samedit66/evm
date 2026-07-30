UV_RUN := uv run --frozen

.PHONY: sync format lint test check ci install-hooks pre-commit

sync:
	uv sync --all-groups

format:
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

lint:
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .

test:
	$(UV_RUN) pytest -m "not network and not toolchain"

check: lint test

ci:
	uv sync --locked --all-groups
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .
	$(UV_RUN) pytest -m "not network and not toolchain"

install-hooks:
	$(UV_RUN) pre-commit install

pre-commit:
	$(UV_RUN) pre-commit run --all-files
