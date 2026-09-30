# Lint and format the shell scripts. The tools run through uvx from pinned
# PyPI wheels, so local runs and CI use the same binaries and nothing is
# installed globally. Style comes from .editorconfig.
SHELLCHECK := uvx --from shellcheck-py==0.11.0.1 shellcheck
SHFMT      := uvx --from shfmt-py==4.2.0 shfmt
SCRIPTS    := bin/museum-sync bin/museum-search install.sh .githooks/pre-commit

.PHONY: check lint fmt hooks

check: lint
	$(SHFMT) -d $(SCRIPTS)

lint:
	$(SHELLCHECK) $(SCRIPTS)

fmt:
	$(SHFMT) -w $(SCRIPTS)

hooks:
	git config core.hooksPath .githooks
