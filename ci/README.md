# Testing and CI

## Running the tests

You need Linux, macOS or WSL (see the note on Windows below) and a Python version
supported by the current Home Assistant release (CI uses 3.14).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check --select F,E,W,B,I,UP --ignore E501 custom_components tests
.venv/bin/python -m pytest tests --timeout 30
```

The suite must pass with **zero skipped tests**: a skip is a false green.

## CI

`ci/Jenkinsfile` is the maintainer's Jenkins pipeline. It does exactly the steps above
in a fresh venv (lint, the full suite with a JUnit report, then a gate that fails the
build if any test was skipped), for the branch given in its `BRANCH` parameter. It
expects an agent with Python 3.14 at `/usr/local/bin/python3.14`; you don't need
Jenkins to work on the integration.

## Notes for contributors

- **Windows.** `pytest-homeassistant-custom-component` imports
  `homeassistant.runner`, which needs `fcntl`, so the Home Assistant tests don't run
  on Windows; use WSL. The pure tests do run on Windows, with the plugin turned off:
  `.venv\Scripts\python -m pytest tests/test_parsers.py tests/test_api.py -p no:homeassistant`.
- **Every test uses the recorder.** The integration depends on `recorder`, so
  `tests/conftest.py` requests `recorder_mock` before anything creates `hass`.
  The other order fails with `assert not hass_fixture_setup`.
- **The clock is frozen in many tests** (`freezer`). `asyncio.sleep(x)` with x > 0
  then never returns, so a test that waits for a background task yields with
  `asyncio.sleep(0)` instead (see `_spin` in `tests/test_llm_api.py`). `--timeout 30`
  makes a hang show up at once, not after minutes.
- **LLM API tests call the tools directly**, with the schema-validated arguments,
  not through `APIInstance.async_call_tool`: that imports the conversation
  integration, which needs `hassil`, not installed in the test venv. They need
  Home Assistant 2026.10 or newer (`probatio` schemas).
- **No real health data in the repository.** The tests use synthetic documents
  (`tests/helpers.py`).
