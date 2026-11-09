import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    # recorder_mock must come before anything that creates `hass`: the integration
    # depends on recorder, so every HA test needs it.
    yield


# Shared with test_review_fixes.py.
from .test_init import setup  # noqa: E402, F401
