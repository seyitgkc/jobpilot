PY ?= .venv/bin/python

.PHONY: test corpus-check

test:
	$(PY) -m pytest -q

corpus-check:
	$(PY) -m jobpilot corpus-check
