"""Represent the Freebox router and its devices and sensors."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
from typing import Any

from freebox_api import Freepybox
from freebox_api.api.wifi import Wifi
from freebox_api.exceptions import HttpRequestError, InsufficientPermissionsError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import slugify

from .const import (
    API_VERSION,
    APP_DESC,
    CONNECTION_SENSORS,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    CONF_USE_HOME
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)


async def get_api(hass: HomeAssistant, host: str) -> Freepybox:
    """Get the Freebox API."""
    freebox_path = hass.helpers.storage.Store(STORAGE_VERSION, STORAGE_KEY).path

    if not os.path.exists(freebox_path):
        await hass.async_add_executor_job(os.makedirs, freebox_path)

    token_file = Path(f"{freebox_path}/{slugify(host)}.conf")

    return Freepybox(APP_DESC, token_file, API_VERSION)

async def reset_api(hass: HomeAssistantType, host: str):
    """Delete the config file to be able to restart a new pairing process."""
    freebox_path = hass.helpers.storage.Store(STORAGE_VERSION, STORAGE_KEY).path
    token_file = Path(f"{freebox_path}/{slugify(host)}.conf")
    token_file.unlink(True)
class FreeboxRouter:
    """Representation of a Freebox router."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize a Freebox router."""
        self.hass = hass
        self._entry = entry
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        #self._use_home = entry.options.get(CONF_USE_HOME, False)
        self._use_home = entry.options.get(CONF_USE_HOME, entry.data.get(CONF_USE_HOME, False))

        self._api: Freepybox = None
        self.name = None
        self.mac = None
        self._sw_v = None
        self._attrs = {}

        self.devices: dict[str, dict[str, Any]] = {}
        self.disks: dict[int, dict[str, Any]] = {}
        self.sensors_temperature: dict[str, int] = {}
        self.sensors_connection: dict[str, float] = {}
        self.call_list: list[dict[str, Any]] = []
        self.home_devices: dict[str, Any] = {}

        self._unsub_dispatcher = None
        self._option_listener = None
        self.listeners = []
        self._warning_once = False

    async def setup(self) -> None:
        """Set up a Freebox router."""
        self._api = await get_api(self.hass, self._host)

        try:
            await self._api.open(self._host, self._port)
        except HttpRequestError:
            _LOGGER.exception("Failed to connect to Freebox")
            return ConfigEntryNotReady

        # System
        fbx_config = await self._api.system.get_config()
        self.mac = fbx_config["mac"]
        self.name = fbx_config["model_info"]["pretty_name"]
        self._sw_v = fbx_config["firmware_version"]

        # Devices & sensors
        await self.update_all()
        self._unsub_dispatcher = async_track_time_interval(
            self.hass, self.update_all, SCAN_INTERVAL
        )

    async def update_all(self, now: datetime | None = None) -> None:
        """Update all Freebox platforms."""
        await self.update_device_trackers()
        await self.update_sensors()
        if self._use_home:
            await self.update_home_devices()

    async def update_device_trackers(self) -> None:
        """Update Freebox devices."""
        new_device = False
        fbx_devices: [dict[str, Any]] = await self._api.lan.get_hosts_list()

        # Adds the Freebox itself
        fbx_devices.append(
            {
                "primary_name": self.name,
                "l2ident": {"id": self.mac},
                "vendor_name": "Freebox SAS",
                "host_type": "router",
                "active": True,
                "attrs": self._attrs,
            }
        )

        for fbx_device in fbx_devices:
            device_mac = fbx_device["l2ident"]["id"]

            if self.devices.get(device_mac) is None:
                new_device = True

            self.devices[device_mac] = fbx_device

        async_dispatcher_send(self.hass, self.signal_device_update)

        if new_device:
            async_dispatcher_send(self.hass, self.signal_device_new)

    async def update_sensors(self) -> None:
        """Update Freebox sensors."""
        # System sensors
        syst_datas: dict[str, Any] = await self._api.system.get_config()

        # According to the doc `syst_datas["sensors"]` is temperature sensors in celsius degree.
        # Name and id of sensors may vary under Freebox devices.
        for sensor in syst_datas["sensors"]:
            if "value" in sensor:
                self.sensors_temperature[sensor["name"]] = sensor["value"]

        # Connection sensors
        connection_datas: dict[str, Any] = await self._api.connection.get_status()
        for sensor_key in CONNECTION_SENSORS:
            self.sensors_connection[sensor_key] = connection_datas[sensor_key]

        self._attrs = {
            "IPv4": connection_datas.get("ipv4"),
            "IPv6": connection_datas.get("ipv6"),
            "connection_type": connection_datas["media"],
            "uptime": datetime.fromtimestamp(
                round(datetime.now().timestamp()) - syst_datas["uptime_val"]
            ),
            "firmware_version": self._sw_v,
            "serial": syst_datas["serial"],
        }

        self.call_list = await self._api.call.get_calls_log()

        await self._update_disks_sensors()

        async_dispatcher_send(self.hass, self.signal_sensor_update)

    async def _update_disks_sensors(self) -> None:
        """Update Freebox disks."""
        # None at first request
        fbx_disks: [dict[str, Any]] = await self._api.storage.get_disks() or []

        for fbx_disk in fbx_disks:
            self.disks[fbx_disk["id"]] = fbx_disk

    async def update_home_devices(self) -> None:
        """Update Home devices (light,cover,alarm,detectors...)"""
        new_device = False
        try:
            home_nodes: Dict[str, Any] = await self._api.home.get_home_nodes()
        except InsufficientPermissionsError as error:
            _LOGGER.warning("Home access is not granted")
            return

        # node sample for DEV
        #home_nodes.append({'adapter': 3, 'category': 'opener', 'group': {'label': ''}, 'id': 42, 'label': 'Dexxo Smart io  ', 'name': 'node_42', 'props': {'Address': 7402911, 'ArcId': 0}, 'show_endpoints': [{'category': '', 'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'ui': {'access': 'rw', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/Porte_Garage_8.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 3, 'label': "Consigne d'ouverture", 'name': 'position_set', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/Porte_Garage_8.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 4, 'label': 'État', 'name': 'state', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'text'}, 'value': 'n/VwQAEARGV4eG8gU21hcnQgaW8gIAL//wAAYBMAIDZCMDg1MTMyNjg2QTA3BYAAyADIAABg3QAAAAA=', 'value_type': 'string', 'visibility': 'normal'}], 'signal_links': [], 'slot_links': [], 'status': 'active', 'type': {'abstract': False, 'endpoints': [{'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 2, 'label': "Consigne d'ouverture", 'name': 'position', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 3, 'label': "Consigne d'ouverture", 'name': 'position_set', 'param_type': 'void', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 4, 'label': 'État', 'name': 'state', 'param_type': 'void', 'value_type': 'string', 'visiblity': 'normal'}], 'generic': False, 'icon': '/resources/images/home/pictos/Porte_Garage_2.png', 'inherit': 'node::ios', 'label': 'Porte de garage connectée', 'name': 'node::ios::5', 'params': {}, 'physical': True}})
        #home_nodes.append({'adapter': 6, 'category': 'shutter', 'group': {'label': 'Chambre'}, 'id': 8, 'label': 'chambre aj', 'name': 'node_8', 'props': {'Address': 6614771, 'ArcId': 0}, 'show_endpoints': [{'category': '', 'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'ui': {'access': 'w', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'text'}, 'value': '8+5kgAAAUyZTTy1SUzEwMCBpbwAAAAL//wAAYBMAIDVCMTI1MTIwODA1QTA1BQAAAAAAAABDVwAAAAA=', 'value_type': 'string', 'visibility': 'normal'}], 'signal_links': [], 'slot_links': [], 'status': 'active', 'type': {'abstract': False, 'endpoints': [{'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 3, 'label': "Consigne d'ouverture", 'name': 'position', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'param_type': 'void', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'param_type': 'void', 'value_type': 'string', 'visiblity': 'normal'}], 'generic': False, 'icon': '/resources/images/home/pictos/volet_3.png', 'inherit': 'node::ios', 'label': 'Volet roulant', 'name': 'node::ios::2', 'params': {}, 'physical': True}})
        #home_nodes.append({'adapter': 6, 'category': 'shutter', 'group': {'label': 'chambre ami'}, 'id': 9, 'label': 'chambre ami', 'name': 'node_9', 'props': {'Address': 2254156, 'ArcId': 1}, 'show_endpoints': [{'category': '', 'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'ui': {'access': 'w', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'text'}, 'value': 'TGUigAAAUyZTTy1SUzEwMCBpbwAAAAL//wAAYBMAIDVCMTI1MTIwODA1QTA1BIAAAAAAAAAAAAAAAAA=', 'value_type': 'string', 'visibility': 'normal'}], 'signal_links': [], 'slot_links': [], 'status': 'active', 'type': {'abstract': False, 'endpoints': [{'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 3, 'label': "Consigne d'ouverture", 'name': 'position', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'param_type': 'void', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'param_type': 'void', 'value_type': 'string', 'visiblity': 'normal'}], 'generic': False, 'icon': '/resources/images/home/pictos/volet_3.png', 'inherit': 'node::ios', 'label': 'Volet roulant', 'name': 'node::ios::2', 'params': {}, 'physical': True}})
        #home_nodes.append({'adapter': 6, 'category': 'shutter', 'group': {'label': 'Bureau'}, 'id': 10, 'label': 'bureau', 'name': 'node_10', 'props': {'Address': 12699842, 'ArcId': 2}, 'show_endpoints': [{'category': '', 'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'ui': {'access': 'w', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 15, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'ui': {'access': 'w', 'display': 'button'}, 'value': None, 'value_type': 'void', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'slider', 'icon_url': '/resources/images/home/pictos/volet_3.png', 'range': [0, 100], 'unit': '%'}, 'value': 0, 'value_type': 'int', 'visibility': 'normal'}, {'category': '', 'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'refresh': 2000, 'ui': {'access': 'r', 'display': 'text'}, 'value': 'wsjBgAAAUyZTTy1SUzEwMCBpbwAAAAL//wAAYBMAIDVCMTI1MTIwODA1QTA1BQAAAAAAAAAAAAAAAAA=', 'value_type': 'string', 'visibility': 'normal'}], 'signal_links': [], 'slot_links': [], 'status': 'active', 'type': {'abstract': False, 'endpoints': [{'ep_type': 'slot', 'id': 0, 'label': "Consigne d'ouverture", 'name': 'position_set', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 1, 'label': 'Stop', 'name': 'stop', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 2, 'label': 'Toggle', 'name': 'toggle', 'value_type': 'void', 'visiblity': 'normal'}, {'ep_type': 'slot', 'id': 3, 'label': "Consigne d'ouverture", 'name': 'position', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 4, 'label': "Consigne d'ouverture", 'name': 'position_set', 'param_type': 'void', 'value_type': 'int', 'visiblity': 'normal'}, {'ep_type': 'signal', 'id': 5, 'label': 'État', 'name': 'state', 'param_type': 'void', 'value_type': 'string', 'visiblity': 'normal'}], 'generic': False, 'icon': '/resources/images/home/pictos/volet_3.png', 'inherit': 'node::ios', 'label': 'Volet roulant', 'name': 'node::ios::2', 'params': {}, 'physical': True}})


        for home_node in home_nodes:
            if( home_node["category"] not in ["pir","camera","alarm","dws","kfb","basic_shutter", "opener", "shutter"] ):
                if( self._warning_once == False ):
                    _LOGGER.warning("Node not supported:\n" +str(home_node))
                continue

            if self.home_devices.get(home_node["id"]) is None:
                new_device = True
            self.home_devices[home_node["id"]] = home_node

        self._warning_once = True
        
        async_dispatcher_send(self.hass,  self.signal_home_device_update)

        if new_device:
            async_dispatcher_send(self.hass, self.signal_home_device_new)

    async def reboot(self) -> None:
        """Reboot the Freebox."""
        await self._api.system.reboot()

    async def close(self) -> None:
        """Close the connection."""
        if self._api is not None:
            await self._api.close()
            self._api = None

        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()

        if self._option_listener is not None:
            self._option_listener()


    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return {
            "connections": {(CONNECTION_NETWORK_MAC, self.mac)},
            "identifiers": {(DOMAIN, self.mac)},
            "name": self.name,
            "manufacturer": "Freebox SAS",
            "sw_version": self._sw_v,
        }

    @property
    def signal_device_new(self) -> str:
        """Event specific per Freebox entry to signal new device."""
        return f"{DOMAIN}-{self._host}-device-new"

    @property
    def signal_home_device_new(self) -> str:
        """Event specific per Freebox entry to signal new home device."""
        return f"{DOMAIN}-{self._host}-home-device-new"

    @property
    def signal_home_device_update(self) -> str:
        """Event specific per Freebox entry to signal update in home devices."""
        return f"{DOMAIN}-{self._host}-home-device-update"

    @property
    def signal_device_update(self) -> str:
        """Event specific per Freebox entry to signal updates in devices."""
        return f"{DOMAIN}-{self._host}-device-update"

    @property
    def signal_sensor_update(self) -> str:
        """Event specific per Freebox entry to signal updates in sensors."""
        return f"{DOMAIN}-{self._host}-sensor-update"

    @property
    def sensors(self) -> dict[str, Any]:
        """Return sensors."""
        return {**self.sensors_temperature, **self.sensors_connection}

    @property
    def wifi(self) -> Wifi:
        """Return the wifi."""
        return self._api.wifi
