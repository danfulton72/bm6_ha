"""Config flow for the BM6 battery monitor."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import protocol as p
from .const import (
    CONF_AUTO_HISTORY,
    CONF_HISTORY_INTERVAL,
    CONF_KEEP_CONNECTED,
    CONF_POLL_INTERVAL,
    CONF_SOC_CURVE,
    DEFAULT_AUTO_HISTORY,
    DEFAULT_HISTORY_INTERVAL,
    DEFAULT_KEEP_CONNECTED,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .coordinator import parse_curve


def _is_bm6(info: BluetoothServiceInfoBleak) -> bool:
    name = (info.name or "").strip()
    if name in p.DEVICE_NAMES:
        return True
    if p.SERVICE_UUID in info.service_uuids:
        return True
    try:
        return p.parse_advert(info.manufacturer_data, info.address) is not None
    except Exception:  # noqa: BLE001
        return False


def _title(info: BluetoothServiceInfoBleak) -> str:
    name = (info.name or "BM6").strip() or "BM6"
    return f"{name} {info.address[-5:].replace(':', '')}"


class BM6ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BM6."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        if not _is_bm6(discovery_info):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        title = _title(self._discovery)
        if user_input is not None:
            return self.async_create_entry(
                title=title, data={CONF_ADDRESS: self._discovery.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": title}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            info = self._discovered[address]
            return self.async_create_entry(title=_title(info), data={CONF_ADDRESS: address})

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or info.address in self._discovered:
                continue
            if _is_bm6(info):
                self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{_title(info)} ({addr})"
                            for addr, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> BM6OptionsFlow:
        return BM6OptionsFlow()


class BM6OptionsFlow(OptionsFlow):
    """Connection, history and SoC-curve options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        if user_input is not None:
            curve = (user_input.get(CONF_SOC_CURVE) or "").strip()
            try:
                parse_curve(curve)
            except ValueError:
                errors[CONF_SOC_CURVE] = "invalid_curve"
            if not errors:
                new = {**opts, **user_input, CONF_SOC_CURVE: curve}
                return self.async_create_entry(data=new)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=3600,
                        step=10,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_KEEP_CONNECTED,
                    default=opts.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED),
                ): bool,
                vol.Required(
                    CONF_AUTO_HISTORY,
                    default=opts.get(CONF_AUTO_HISTORY, DEFAULT_AUTO_HISTORY),
                ): bool,
                vol.Required(
                    CONF_HISTORY_INTERVAL,
                    default=opts.get(CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=1440,
                        step=10,
                        unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_SOC_CURVE,
                    description={"suggested_value": opts.get(CONF_SOC_CURVE, "")},
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
