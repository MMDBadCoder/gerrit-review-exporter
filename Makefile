PYTHON ?= python3

.PHONY: test test-real version

test:
	$(PYTHON) -m unittest discover -s tests -v

test-real:
	PYTHON="$(PYTHON)" bash tests/run_real_gerrit.sh

version:
	$(PYTHON) gerrit_rv.py --version
