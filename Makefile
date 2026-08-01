UV_RUN := uv run --frozen

.PHONY: sync format lint test coverage check ci install-hooks pre-commit

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

coverage:
	$(UV_RUN) pytest -m "not network and not toolchain" \
		--cov=evm \
		--cov-report=term-missing:skip-covered --cov-fail-under=90

check: lint test

ci:
	uv sync --locked --all-groups
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .
	$(UV_RUN) pytest -m "not network and not toolchain" \
		--cov=evm \
		--cov-report=term-missing:skip-covered --cov-fail-under=90

install-hooks:
	$(UV_RUN) pre-commit install

pre-commit:
	$(UV_RUN) pre-commit run --all-files
