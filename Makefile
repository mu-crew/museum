# Lint, format, type-check and test. The tools run through uvx from pinned
# PyPI wheels, so local runs and CI use the same binaries and nothing is
# installed globally. Shell style comes from .editorconfig, Python style and
# the 3.9 target from pyproject.toml. The tests are stdlib unittest; PYTHON
# picks the interpreter (CI also runs them on 3.9, macOS's system python).
SHELLCHECK := uvx --from shellcheck-py==0.11.0.1 shellcheck
SHFMT      := uvx --from shfmt-py==4.2.0 shfmt
RUFF       := uvx --from ruff==0.16.9 ruff
TY         := uvx --from ty==0.0.84 ty
PYTHON     ?= python3
SHELL_SCRIPTS := install.sh .githooks/pre-commit

.PHONY: check lint fmt test hooks

check: lint test
	$(SHFMT) -d $(SHELL_SCRIPTS)
	$(RUFF) format --check

lint:
	$(SHELLCHECK) $(SHELL_SCRIPTS)
	$(RUFF) check
	$(TY) check

test:
	cd test && $(PYTHON) -m unittest

fmt:
	$(SHFMT) -w $(SHELL_SCRIPTS)
	$(RUFF) format
	$(RUFF) check --fix

hooks:
	git config core.hooksPath .githooks
