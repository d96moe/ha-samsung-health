# CI

## Jobs (Jenkins on LXC 200)

| Job | File | What it does |
|---|---|---|
| `ha-samsung-health` | `ci/Jenkinsfile` | Lint (ruff), the full pytest suite in a Python 3.14 venv on the Jenkins agent (`BRANCH` parameter), then a zero-skip gate. |

## Notes

- **Tests need Linux.** `pytest-homeassistant-custom-component` imports
  `homeassistant.runner`, which needs `fcntl`, so the HA tests do not run on
  Windows. The pure tests do run on Windows, with the plugin turned off:
  `.venv\Scripts\python -m pytest tests/test_parsers.py tests/test_api.py -p no:homeassistant`.
- **Every test uses the recorder.** The integration depends on `recorder`, so
  `tests/conftest.py` requests `recorder_mock` before anything creates `hass`.
  The other order fails with `assert not hass_fixture_setup`.
- **Zero-skip gate.** A skipped test is a false green, so the build fails if
  `test-results.xml` reports any skips.
- **The clock is frozen in many tests** (`freezer`). `asyncio.sleep(x)` with x > 0
  then never returns, so a test that waits for a background task yields with
  `asyncio.sleep(0)` instead (see `_spin` in `tests/test_llm_api.py`). Run with
  `--timeout 30` locally so a hang shows up at once, not after minutes.
- **LLM API tests call the tools directly**, with the schema-validated arguments,
  not through `APIInstance.async_call_tool`: that imports the conversation
  integration, which needs `hassil`, not installed in the test venv. They need
  Home Assistant 2026.10 or newer (`probatio` schemas).
- **No real health data in the repository.** The tests use synthetic documents
  (`tests/helpers.py`). Validation against real data and the Samsung Health app's
  values is a local, gitignored script (`spike/private/validate.py`).
