# `python -E` makes the interpreter ignore PYTHONPATH, which /opt/ros/jazzy/setup.bash
# (sourced from ~/.bashrc) points at ROS's site-packages. Without -E, pytest autoloads
# ROS's `launch_testing` plugin and dies on its missing `yaml` dependency.
PY := .venv/bin/python -E

.PHONY: venv install test test-live sync-rules clean

venv:
	uv venv --python 3.12 .venv

install:
	uv pip install --python .venv/bin/python -e '.[dev]'

test:
	$(PY) -m pytest -q

test-live:
	$(PY) -m pytest -m live -v

# The extension needs its own copy of rules.json (a packed .zip cannot follow a
# symlink). A test asserts the two stay byte-identical.
sync-rules:
	cp rules.json extension/rules.json

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info
