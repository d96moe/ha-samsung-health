"""Config flow: Samsung Account login by pasting the ms-app:// callback URL.

Samsung's login ends by redirecting the browser to an ms-app://... URL (the
Windows Samsung Health app's callback), which no browser can open. The user copies
that URL from the address bar (or browser dev tools) and pastes it here.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import SamsungHealthApiError, SamsungHealthAuthError, complete_login, start_login
from .const import CONF_STORAGE_KEY, DEFAULT_COUNTRY, DEFAULT_LOCALE, DOMAIN
from .coordinator import entry_paths

_LOGGER = logging.getLogger(__name__)
CONF_REDIRECT_URL = "redirect_url"


def account_unique_id(user_id: str) -> str:
    """Stable, non-reversible id for the Samsung account."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:32]


class SamsungHealthConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._storage_key: str | None = None
        self._login_url: str | None = None

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
                return self.async_create_entry(
                    title="Samsung Health", data={CONF_STORAGE_KEY: self._storage_key}
                )

        if self._login_url is None:
            try:
                await self._async_new_login_url()
            except SamsungHealthApiError as exc:
                _LOGGER.debug("Could not start Samsung login: %s", exc)
                return self.async_abort(reason="cannot_connect")

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema({vol.Required(CONF_REDIRECT_URL): str}),
            description_placeholders={"login_url": self._login_url},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._storage_key = entry_data[CONF_STORAGE_KEY]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_login()
