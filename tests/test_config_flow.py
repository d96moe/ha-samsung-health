from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_samsung_health.api import SamsungHealthAuthError, SamsungHealthTransientError
from custom_components.ha_samsung_health.config_flow import account_unique_id
from custom_components.ha_samsung_health.const import CONF_STORAGE_KEY, DOMAIN

FLOW = "custom_components.ha_samsung_health.config_flow"
CALLBACK = "ms-app://s-1-15-2-1?code=a&state=b&auth_server_url=c&retValue=d"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.ha_samsung_health.async_setup_entry", return_value=True):
        yield


async def test_user_flow_creates_entry(hass):
    with patch(f"{FLOW}.start_login", return_value="https://account.samsung.com/login") as start, \
         patch(f"{FLOW}.complete_login", return_value="user-1"):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "login"
        assert result["description_placeholders"]["login_url"] == "https://account.samsung.com/login"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"redirect_url": CALLBACK})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_STORAGE_KEY]) == 32
    assert result["result"].unique_id == account_unique_id("user-1")
    assert start.call_count == 1


async def test_invalid_callback_shows_error_and_new_link(hass):
    with patch(f"{FLOW}.start_login", side_effect=["https://x/1", "https://x/2"]) as start, \
         patch(f"{FLOW}.complete_login", side_effect=SamsungHealthAuthError("expired")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"redirect_url": "nope"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert result["description_placeholders"]["login_url"] == "https://x/2"
    assert start.call_count == 2


async def test_network_error_keeps_link(hass):
    with patch(f"{FLOW}.start_login", return_value="https://x/1") as start, \
         patch(f"{FLOW}.complete_login", side_effect=SamsungHealthTransientError("down")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"redirect_url": CALLBACK})
    assert result["errors"] == {"base": "cannot_connect"}
    assert start.call_count == 1


async def test_cannot_start_login_aborts(hass):
    with patch(f"{FLOW}.start_login", side_effect=SamsungHealthTransientError("down")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=account_unique_id("user-1"), data={CONF_STORAGE_KEY: "k" * 32})
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(("user_id", "reason"), [("user-1", "reauth_successful"), ("other", "wrong_account")])
async def test_reauth(hass, user_id, reason):
    entry = _entry(hass)
    with patch(f"{FLOW}.start_login", return_value="https://x/1"), \
         patch(f"{FLOW}.complete_login", return_value=user_id) as complete:
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "login"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"redirect_url": CALLBACK})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    # reauth reuses the entry's own storage directory
    assert complete.call_args.args[0].directory.name == "k" * 32
