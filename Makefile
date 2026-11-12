.PHONY: check test lint compile link

check: compile lint test ## Run compile, lint and unit tests

compile: ## Byte-compile the package
	python3 -m compileall -q herdr_zshz

test: ## Run the unit test suite
	uv run python -m unittest discover -s tests -v

lint: ## Lint with ruff
	uv run --with ruff ruff check .

link: ## Link the plugin into herdr
	herdr plugin link .
