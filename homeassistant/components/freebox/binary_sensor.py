"""Support for motion detector, door opener detector and check for sensor plastic cover """
import logging
import asyncio

from typing import Dict, Optional
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.components.binary_sensor import BinarySensorEntity, DEVICE_CLASS_MOTION, DEVICE_CLASS_DOOR, DEVICE_CLASS_SAFETY
from datetime import datetime, timedelta

from .base_class import FreeboxHomeBaseClass
from .const import DOMAIN, VALUE_NOT_SET
from .router import FreeboxRouter

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    router = hass.data[DOMAIN][entry.unique_id]
    tracked = set()

    @callback
    def update_callback():
        add_entities(hass, router, async_add_entities, tracked)

    router.listeners.append(async_dispatcher_connect(hass, router.signal_home_device_new, update_callback))
    update_callback()


@callback
def add_entities(hass, router, async_add_entities, tracked):
    """Add new sensors from the router."""
    new_tracked = []

    for nodeId, node in router.home_devices.items():
        if nodeId in tracked:
            continue
        if node["category"]=="pir":
            new_tracked.append(FreeboxPir(hass, router, node))
        elif node["category"]=="dws":
            new_tracked.append(FreeboxDws(hass, router, node))

        sensor_cover_node = next(filter(lambda x: (x["name"]=="cover" and x["ep_type"]=="signal"), node["show_endpoints"]), None)
        if( sensor_cover_node != None and sensor_cover_node.get("value", None) != None):
            new_tracked.append(FreeboxSensorCover(hass, router, node))

        tracked.add(nodeId)

    if new_tracked:
        async_add_entities(new_tracked, True)


''' Freebox motion detector sensor '''
class FreeboxPir(FreeboxHomeBaseClass, BinarySensorEntity):

    def __init__(self, hass, router: FreeboxRouter, node: Dict[str, any]) -> None:
        """Initialize a Pir"""
        super().__init__(hass, router, node)
        self._command_trigger = self.get_command_id(node['type']['endpoints'], "signal", "trigger")
        self._detection = False
        self.start_watcher(timedelta(seconds=2))
        self._had_timeout = False

    async def async_watcher(self, now: Optional[datetime] = None) -> None:
        try:
            detection = await self.get_home_endpoint_value(self._command_trigger)
            self._had_timeout = False
            if( self._detection == detection ):
                self._detection = not detection
                self.async_write_ha_state()
        except TimeoutError as error:
            if( self._had_timeout ):
                _LOGGER.warning("Freebox API Timeout")
                self._had_timeout = False
            else:
                self._had_timeout = True

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._detection

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return DEVICE_CLASS_MOTION


''' Freebox door opener sensor '''
class FreeboxDws(FreeboxPir):
    def __init__(self, hass, router: FreeboxRouter, node: Dict[str, any]) -> None:
        super().__init__(hass, router, node)

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return DEVICE_CLASS_DOOR


''' Freebox plastic cover removal check for some sensors (motion detector, door opener detector...) '''
class FreeboxSensorCover(FreeboxHomeBaseClass, BinarySensorEntity):
    def __init__(self, hass, router: FreeboxRouter, node: Dict[str, any]) -> None:
        """Initialize a Cover for anothe Device"""
        # Get cover node
        cover_node = next(filter(lambda x: (x["name"]=="cover" and x["ep_type"]=="signal"), node['type']['endpoints']), None)
        super().__init__(hass, router, node, cover_node)
        self._command_cover = self.get_command_id(node['show_endpoints'], "signal", "cover")
        self._open          = self.get_value("signal", "cover")

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._open

    async def async_update_node(self):
        """Update name & state."""
        self._open = self.get_value("signal", "cover")

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return DEVICE_CLASS_SAFETY
