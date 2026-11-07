"""Config flow: Samsung Account login by pasting the ms-app:// callback URL.

Samsung's login ends by redirecting the browser to an ms-app://... URL (the
Windows Samsung Health app's callback), which no browser can open. The user copies
that URL from the browser's developer tools and pastes it here. After a successful
sign-in a progress step downloads the history, so the dialog never looks stuck.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import SamsungHealthApiError, SamsungHealthAuthError, complete_login, start_login
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_STORAGE_KEY,
    DEFAULT_COUNTRY,
    DEFAULT_LOCALE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SCAN_INTERVAL_OPTIONS,
)
from .coordinator import entry_paths, fetch_history, prefetched_store

_LOGGER = logging.getLogger(__name__)
CONF_REDIRECT_URL = "redirect_url"


def interval_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=str(m), label=f"{m} min") for m in SCAN_INTERVAL_OPTIONS],
            mode=SelectSelectorMode.LIST,
        )
    )


def account_unique_id(user_id: str) -> str:
    """Stable, non-reversible id for the Samsung account."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:32]


class SamsungHealthConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._storage_key: str | None = None
        self._login_url: str | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._scan_interval = DEFAULT_SCAN_INTERVAL

    async def _async_new_login_url(self) -> None:
        paths = entry_paths(self.hass, self._storage_key)
        self._login_url = await self.hass.async_add_executor_job(
            start_login, paths, DEFAULT_COUNTRY, DEFAULT_LOCALE
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._storage_key = uuid.uuid4().hex
        return await self.async_step_login()

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if CONF_SCAN_INTERVAL in user_input:
                self._scan_interval = int(user_input[CONF_SCAN_INTERVAL])
            paths = entry_paths(self.hass, self._storage_key)
            try:
                user_id = await self.hass.async_add_executor_job(
                    complete_login, paths, user_input[CONF_REDIRECT_URL]
                )
            except SamsungHealthAuthError as exc:
                # Expired pending login or a URL that isn't the callback: start over.
                _LOGGER.debug("Samsung login rejected: %s", exc)
                errors["base"] = "invalid_auth"
                self._login_url = None
            except SamsungHealthApiError as exc:
                _LOGGER.debug("Samsung login failed: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(account_unique_id(user_id))
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(self._get_reauth_entry())
                self._abort_if_unique_id_configured()
                return await self.async_step_first_sync()

        if self._login_url is None:
            try:
                await self._async_new_login_url()
            except SamsungHealthApiError as exc:
                _LOGGER.debug("Could not start Samsung login: %s", exc)
                return self.async_abort(reason="cannot_connect")

        schema: dict[Any, Any] = {vol.Required(CONF_REDIRECT_URL): str}
        if self.source != "reauth":
            # Same choice as under Configure, so it can be set right away.
            schema[vol.Required(CONF_SCAN_INTERVAL, default=str(self._scan_interval))] = interval_selector()
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(schema),
            description_placeholders={"login_url": self._login_url},
            errors=errors,
        )

    async def async_step_first_sync(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Signed in: download the history now, with a progress dialog that says so.

        The first download takes 10-60 s. Without this step the sign-in dialog just
        spins until it's done, which looks like the sign-in hanging.
        """
        if self._sync_task is None:
            self._sync_task = self.hass.async_create_task(self._async_first_sync())
        if not self._sync_task.done():
            return self.async_show_progress(
                step_id="first_sync", progress_action="first_sync", progress_task=self._sync_task
            )
        return self.async_show_progress_done(next_step_id="finish")

    async def _async_first_sync(self) -> None:
        paths = entry_paths(self.hass, self._storage_key)
        try:
            prefetched = await self.hass.async_add_executor_job(fetch_history, paths)
        except SamsungHealthApiError as exc:
            # Not fatal: the entry's own first refresh retries it.
            _LOGGER.warning("First Samsung Health download failed, will retry after setup: %s", exc)
            return
        prefetched_store(self.hass)[self._storage_key] = prefetched

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title="Samsung Health",
            data={CONF_STORAGE_KEY: self._storage_key},
            options={CONF_SCAN_INTERVAL: self._scan_interval},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SamsungHealthOptionsFlow:
        return SamsungHealthOptionsFlow()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._storage_key = entry_data[CONF_STORAGE_KEY]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_login()


class SamsungHealthOptionsFlow(OptionsFlow):
    """Polling interval. Every poll downloads the full history, so keep it modest."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=str(current)): interval_selector()}),
        )
