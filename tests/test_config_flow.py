import asyncio
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_samsung_health.api import SamsungHealthAuthError, SamsungHealthTransientError
from custom_components.ha_samsung_health.config_flow import account_unique_id
from custom_components.ha_samsung_health.const import CONF_STORAGE_KEY, DOMAIN
from custom_components.ha_samsung_health.coordinator import Prefetched, entry_paths, prefetched_store

FLOW = "custom_components.ha_samsung_health.config_flow"
CALLBACK = "ms-app://s-1-15-2-1?code=a&state=b&auth_server_url=c&retValue=d"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.ha_samsung_health.async_setup_entry", return_value=True), \
         patch("custom_components.ha_samsung_health.async_unload_entry", return_value=True):
        yield


PREFETCHED = Prefetched({"m": []}, {"m": 1}, {})


async def _sign_in(hass, history, form=None, complete=None):
    """Run the flow through sign-in and the history download; returns the final result."""
    with patch(f"{FLOW}.start_login", return_value="https://account.samsung.com/login") as start, \
         patch(f"{FLOW}.complete_login", **(complete or {"return_value": "user-1"})), \
         patch(f"{FLOW}.fetch_history", **history) as fetch:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "login"
        assert result["description_placeholders"]["login_url"] == "https://account.samsung.com/login"
        gate = asyncio.Event()
        real = hass.async_add_executor_job

        async def slow_executor(func, *args):
            if func is fetch:
                await gate.wait()
            return await real(func, *args)

        with patch.object(hass, "async_add_executor_job", slow_executor):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"redirect_url": CALLBACK, **(form or {})}
            )
            # Signed in: a progress dialog says so while the history downloads.
            assert result["type"] is FlowResultType.SHOW_PROGRESS
            assert result["step_id"] == "first_sync"
            assert result["progress_action"] == "first_sync"
            gate.set()
            await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert start.call_count == 1
    return result


async def test_user_flow_downloads_history_then_creates_entry(hass):
    result = await _sign_in(hass, {"return_value": PREFETCHED})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    key = result["data"][CONF_STORAGE_KEY]
    assert len(key) == 32
    assert result["result"].unique_id == account_unique_id("user-1")
    assert result["result"].options == {"scan_interval_minutes": 60}  # default
    # handed to the entry's first refresh, so it isn't downloaded twice
    assert prefetched_store(hass)[key] is PREFETCHED


async def test_update_interval_chosen_at_setup(hass):
    result = await _sign_in(hass, {"return_value": PREFETCHED}, {"scan_interval_minutes": "15"})
    assert result["result"].options == {"scan_interval_minutes": 15}


async def test_failed_first_download_still_creates_entry(hass):
    result = await _sign_in(hass, {"side_effect": SamsungHealthTransientError("down")})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert prefetched_store(hass) == {}


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


async def test_options_flow_sets_interval(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval_minutes": "120"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"scan_interval_minutes": 120}


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=account_unique_id("user-1"), data={CONF_STORAGE_KEY: "k" * 32})
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(("user_id", "reason"), [("user-1", "reauth_successful"), ("other", "wrong_account")])
async def test_reauth(hass, user_id, reason):
    entry = _entry(hass)
    entry_dir = entry_paths(hass, "k" * 32).directory
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "master.json").write_text("old login of user-1")

    def login(paths, url):  # what the library does: write the new login into the directory
        paths.directory.mkdir(parents=True, exist_ok=True)
        (paths.directory / "master.json").write_text(f"new login of {user_id}")
        return user_id

    with patch(f"{FLOW}.start_login", return_value="https://x/1"), \
         patch(f"{FLOW}.complete_login", side_effect=login) as complete:
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "login"
        assert "scan_interval_minutes" not in result["data_schema"].schema  # only at setup
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"redirect_url": CALLBACK})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    # The login is staged elsewhere; only the right account ever reaches the entry's directory.
    assert complete.call_args.args[0].directory != entry_dir
    expected = "new login of user-1" if reason == "reauth_successful" else "old login of user-1"
    assert (entry_dir / "master.json").read_text() == expected
    leftovers = [p.name for p in entry_dir.parent.iterdir()
                 if p.name.startswith("reauth-") or p.name.endswith(".old")]
    assert leftovers == []  # no staging left behind


async def test_abandoned_sign_in_leaves_no_files(hass):
    created = []

    def start(paths, country, locale):
        paths.directory.mkdir(parents=True, exist_ok=True)
        (paths.directory / "pending.json").write_text("{}")
        created.append(paths.directory)
        return "https://x/1"

    with patch(f"{FLOW}.start_login", side_effect=start):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert created[0].exists()
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert not created[0].exists()


async def test_unexpected_error_in_first_download_still_creates_entry(hass, caplog):
    result = await _sign_in(hass, {"side_effect": RuntimeError("library bug")})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "First Samsung Health download failed" in caplog.text  # logged, not an unretrieved task error


async def test_finished_sign_in_keeps_its_login(hass):
    def login(paths, url):
        paths.directory.mkdir(parents=True, exist_ok=True)
        (paths.directory / "master.json").write_text("{}")
        return "user-1"

    result = await _sign_in(hass, {"return_value": PREFETCHED}, complete={"side_effect": login})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (entry_paths(hass, result["data"][CONF_STORAGE_KEY]).directory / "master.json").exists()
