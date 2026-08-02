PY ?= .venv/bin/python

.PHONY: test corpus-check plugin

test:
	$(PY) -m pytest -q

corpus-check:
	$(PY) -m jobpilot corpus-check

plugin:
	$(PY) scripts/build_plugin.py
