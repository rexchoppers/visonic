"""Create a connection to a Visonic PowerMax or PowerMaster Alarm System."""

import logging
import asyncio
import requests.exceptions
import voluptuous as vol

from dataclasses import dataclass
from typing_extensions import TypeVar

#from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, valid_entity_id, CALLBACK_TYPE
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.components import persistent_notification
from .pyconst import AlPanelCommand

from homeassistant.const import (
    ATTR_CODE,
    ATTR_ENTITY_ID,
    SERVICE_RELOAD,
    Platform,
)

from .client import VisonicClient
from .const import (
    DOMAIN,
    DOMAINCLIENT,
    DOMAINDATA,
    DOMAINCLIENTTASK,
    ALARM_PANEL_ENTITY,
    ALARM_PANEL_EVENTLOG,
    ALARM_PANEL_RECONNECT,
    ALARM_PANEL_COMMAND,
    ALARM_SENSOR_BYPASS,
    ALARM_SENSOR_IMAGE,
    ATTR_BYPASS,
    CONF_PANEL_NUMBER,
    PANEL_ATTRIBUTE_NAME,
    NOTIFICATION_ID,
    NOTIFICATION_TITLE,
    BINARY_SENSOR_STR,
    IMAGE_SENSOR_STR,
    SWITCH_STR,
    SELECT_STR,
    CONF_EMULATION_MODE,
    CONF_COMMAND,
    available_emulation_modes,
)

_LOGGER = logging.getLogger(__name__)

# the 5 schemas for the HA service calls
ALARM_SCHEMA_EVENTLOG = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_CODE, default=""): cv.string,
    }
)

ALARM_SCHEMA_COMMAND = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_COMMAND) : vol.In([x.lower().replace("_"," ").title() for x in list(AlPanelCommand.get_variables().keys())]),
        vol.Optional(ATTR_CODE, default=""): cv.string,
    }
)

ALARM_SCHEMA_RECONNECT = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
    }
)

ALARM_SCHEMA_BYPASS = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_BYPASS, default=False): cv.boolean,
        vol.Optional(ATTR_CODE, default=""): cv.string,
    }
)

ALARM_SCHEMA_IMAGE = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
    }
)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL, Platform.BINARY_SENSOR, Platform.IMAGE, Platform.SENSOR, Platform.SWITCH, Platform.SELECT]

#_R = TypeVar("_R")
#
#@dataclass
#class VisonicData:
#    client: VisonicClient
#    client_task: asyncio.Task[_R]
#   
#VisonicConfigEntry = ConfigEntry[VisonicData]

async def combineSettings(entry):
    """Combine the old settings from data and the new from options."""
    # convert python map to dictionary
    conf = {}
    # the entry.data dictionary contains all the old data used on creation and is a complete set
    for k in entry.data:
        conf[k] = entry.data[k]
    # the entry.config dictionary contains the latest/updated values but may not be a complete set
    for k in entry.options:
        conf[k] = entry.options[k]
    return conf

#from homeassistant.helpers import entity_registry as er
#def dummy():
#    # Remove ozone sensors from registry if they exist
#    ent_reg = er.async_get(hass)
#    for day in range(5):
#        unique_id = f"{location_key}-ozone-{day}"
#        if entity_id := ent_reg.async_get_entity_id(SENSOR_PLATFORM, DOMAIN, unique_id):
#            _LOGGER.debug("Removing ozone sensor entity %s", entity_id)
#            ent_reg.async_remove(entity_id)
            
            

async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old schema configuration entry to new."""
    # This function is called when I change VERSION in the ConfigFlow
    # If the config schema ever changes then use this function to convert from old to new config parameters
    version = config_entry.version

    _LOGGER.debug(f"Migrating from version {version}")

    if version == 1:
        # Leave CONF_FORCE_STANDARD in place but use it to add CONF_EMULATION_MODE
        version = 2
        new = config_entry.data.copy()
        CONF_FORCE_STANDARD = "force_standard"
        
        _LOGGER.debug(f"   Migrating CONF_FORCE_STANDARD from {config_entry.data[CONF_FORCE_STANDARD]}")
        if isinstance(config_entry.data[CONF_FORCE_STANDARD], bool):
            _LOGGER.debug(f"   Migrating CONF_FORCE_STANDARD from {config_entry.data[CONF_FORCE_STANDARD]} and its boolean")
            if config_entry.data[CONF_FORCE_STANDARD]:
                _LOGGER.info(f"   Migration: Force standard set so using {available_emulation_modes[1]}")
                new[CONF_EMULATION_MODE] = available_emulation_modes[1]
            else:
                _LOGGER.info(f"   Migration: Force standard not set so using {available_emulation_modes[0]}")
                new[CONF_EMULATION_MODE] = available_emulation_modes[0]
        
        #del new[CONF_FORCE_STANDARD]
        hass.config_entries.async_update_entry(config_entry, data=new, options=new, version=version)
        _LOGGER.info(f"   Emulation mode set to {config_entry.data[CONF_EMULATION_MODE]}")

        #return False # when any changes have failed

    _LOGGER.info("Migration to version %s successful", config_entry.version)
    return True

async def async_setup(hass: HomeAssistant, base_config: dict):
    """Set up the visonic component."""
    
    def sendHANotification(message: str):
        """Send a HA notification and output message to log file"""
        _LOGGER.info(message)
        persistent_notification.create(hass, message, title=NOTIFICATION_TITLE, notification_id=NOTIFICATION_ID)

    def getClient(call):
        """Lookup the panel number from the service call and find the client for that panel"""
        _LOGGER.info(f"getClient called and call is {call}")        
        if isinstance(call.data, dict):
            _LOGGER.info(f"getClient called {call.data}")
            # 'entity_id': 'alarm_control_panel.visonic_alarm'
            if ATTR_ENTITY_ID in call.data:
                eid = str(call.data[ATTR_ENTITY_ID])
                if valid_entity_id(eid):
                    mybpstate = hass.states.get(eid)
                    if mybpstate is not None:
                        if PANEL_ATTRIBUTE_NAME in mybpstate.attributes:
                            panel = mybpstate.attributes[PANEL_ATTRIBUTE_NAME]
                            # Check each connection to get the requested panel
                            for entry in hass.config_entries.async_entries(DOMAIN):
                                if entry.entry_id in hass.data[DOMAIN][DOMAINCLIENT]:
                                    client = hass.data[DOMAIN][DOMAINCLIENT][entry.entry_id]
                                    if client is not None:
                                        if panel == client.getPanelID():
                                            #_LOGGER.info(f"getClient success, found client and panel")
                                            return client, panel
                                else:
                                    _LOGGER.info(f"getClient unknown entry ID {entry.entry_id}")
                            return None, panel
                else:
                    _LOGGER.info(f"getClient called invalid entity ID {eid}")
        return None, None
    
    async def service_panel_eventlog(call):
        """Handler for event log service"""
        _LOGGER.info("Event log called")
        
        client, panel = getClient(call)
        if client is not None:
            await client.service_panel_eventlog(call)
        elif panel is not None:
            sendHANotification(f"Event log failed - Panel {panel} not found")
        else:
            sendHANotification(f"Event log failed - Panel not found")
    
    async def service_panel_reconnect(call):
        """Handler for panel reconnect service"""
        _LOGGER.info("Service Panel reconnect called")
        client, panel = getClient(call)
        if client is not None:
            await client.service_panel_reconnect(call)
        elif panel is not None:
            sendHANotification(f"Service Panel reconnect failed - Panel {panel} not found")
        else:
            sendHANotification(f"Service Panel reconnect failed - Panel not found")
    
    async def service_panel_command(call):
        """Handler for panel command service"""
        _LOGGER.info("Service Panel command called")
        client, panel = getClient(call)
        if client is not None:
            await client.service_panel_command(call)
        elif panel is not None:
            sendHANotification(f"Service Panel command failed - Panel {panel} not found")
        else:
            sendHANotification(f"Service Panel command failed - Panel not found")
    
    async def service_sensor_bypass(call):
        """Handler for sensor bypass service"""
        _LOGGER.info("Service Panel sensor bypass called")
        client, panel = getClient(call)
        if client is not None:
            await client.service_sensor_bypass(call)
        elif panel is not None:
            sendHANotification(f"Service Panel sensor bypass failed - Panel {panel} not found")
        else:
            sendHANotification(f"Service Panel sensor bypass failed - Panel not found")
    
    async def service_sensor_image(call):
        """Handler for sensor image service"""
        _LOGGER.info("Service Panel sensor image update called")
        client, panel = getClient(call)
        if client is not None:
            await client.service_sensor_image(call)
        elif panel is not None:
            sendHANotification(f"Service sensor image update - Panel {panel} not found")
        else:
            sendHANotification(f"Service sensor image update failed - Panel not found")
 
    async def handle_reload(call) -> None: 
        """Handle reload service call."""
        _LOGGER.info("Domain {0} call {1} reload called: reloading integration".format(DOMAIN, call))
        current_entries = hass.config_entries.async_entries(DOMAIN)
        reload_tasks = [
            hass.config_entries.async_reload(entry.entry_id)
            for entry in current_entries
        ]
        await asyncio.gather(*reload_tasks)

    _LOGGER.info("Starting Visonic Component")
    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][DOMAINDATA] = {}
    hass.data[DOMAIN][DOMAINCLIENT] = {}
    hass.data[DOMAIN][DOMAINCLIENTTASK] = {}
    
    # Empty out the lists (these are no longer used in Version 2)
    hass.data[DOMAIN][BINARY_SENSOR_STR] = list()
    hass.data[DOMAIN][IMAGE_SENSOR_STR] = list()    
    hass.data[DOMAIN][SELECT_STR] = list()
    hass.data[DOMAIN][SWITCH_STR] = list()
    hass.data[DOMAIN][ALARM_PANEL_ENTITY] = list()
    
    # Install the 5 handlers for the HA service calls
    hass.services.async_register(
        domain = DOMAIN,
        service = ALARM_PANEL_EVENTLOG,
        service_func = service_panel_eventlog,
        schema = ALARM_SCHEMA_EVENTLOG,
    )
    hass.services.async_register(
        DOMAIN, 
        ALARM_PANEL_RECONNECT, 
        service_panel_reconnect, 
        schema=ALARM_SCHEMA_RECONNECT,
    )
    hass.services.async_register(
        DOMAIN,
        ALARM_PANEL_COMMAND,
        service_panel_command,
        schema=ALARM_SCHEMA_COMMAND,
    )
    hass.services.async_register(
        DOMAIN,
        ALARM_SENSOR_BYPASS,
        service_sensor_bypass,
        schema=ALARM_SCHEMA_BYPASS,
    )
    hass.services.async_register(
        DOMAIN,
        ALARM_SENSOR_IMAGE,
        service_sensor_image,
        schema=ALARM_SCHEMA_IMAGE,
    )
    
    # Install the reload handler
    #    commented out as it reloads all panels, the default in the frontend only reloads the instance
    #async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, handle_reload)

    return True

# This function is called with the flow data to create a client connection to the alarm panel
# From one of:
#    - the imported configuration.yaml values that have created a control flow
#    - the original control flow if it existed
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up visonic from a config entry."""
    
    def configured_hosts(hass):
        """Return a set of the configured hosts."""
        return len(hass.config_entries.async_entries(DOMAIN))
    
    def findPanel(panel):
        for e in hass.config_entries.async_entries(DOMAIN):
            if e.entry_id in hass.data[DOMAIN][DOMAINCLIENT]:
                client = hass.data[DOMAIN][DOMAINCLIENT][e.entry_id]
                if client is not None:
                    if panel == client.getPanelID():
                        #_LOGGER.info(f"findPanel success, found client and panel")
                        return client
            elif e.entry_id == entry.entry_id:
                _LOGGER.info(f"findPanel I've found myself")
                pass  # this is itself and it won't be in the hass.data with a client yet
            else:
                _LOGGER.info(f"findPanel unknown entry ID {e.entry_id}")
        return None

    eid = entry.entry_id

    _LOGGER.debug(f"[Visonic Setup] ************* create connection here **************  entry data={entry.data}   options={entry.options}")

    # remove all old settings for this component, previous versions of this integration
    hass.data[DOMAIN][eid] = {}
    # Empty out the lists
    hass.data[DOMAIN][eid][BINARY_SENSOR_STR] = list()
    hass.data[DOMAIN][eid][IMAGE_SENSOR_STR] = list()    
    hass.data[DOMAIN][eid][SELECT_STR] = list()
    hass.data[DOMAIN][eid][SWITCH_STR] = list()
    hass.data[DOMAIN][eid][ALARM_PANEL_ENTITY] = list()
    
    _LOGGER.info("[Visonic Setup] Starting Visonic with entry id={0} in a total of {1} configured panels".format(eid, configured_hosts(hass)))
    
    # combine and convert python settings map to dictionary
    conf = await combineSettings(entry)

    panel_id = 0
    
    if CONF_PANEL_NUMBER in conf:
        panel_id = int(conf[CONF_PANEL_NUMBER])
        _LOGGER.debug("[Visonic Setup] Panel Config has panel number {0}".format(panel_id))
    else: 
        _LOGGER.debug("[Visonic Setup] CONF_PANEL_NUMBER not in configuration, defaulting to panel 0 (before uniqueness check)")

    # Check for unique panel ids or HA gets really confused and we end up make a big mess in the config files.
    if cl := findPanel(panel_id) is not None:
        _LOGGER.warning("[Visonic Setup] The Panel Number {0} is not Unique, you already have a Panel with this Number".format(panel_id))
        return False

    # When here, panel_id should be unique in the panels configured so far.
    _LOGGER.debug("[Visonic Setup] Panel Ident {0}".format(panel_id))
    
    # push the merged data back in to HA and update the title
    hass.config_entries.async_update_entry(entry, title=f"Panel {panel_id}", options=conf)

    # create client and connect to the panel
    try:
        # create the client ready to connect to the panel
        client = VisonicClient(hass, panel_id, conf, entry)
        # Save the client ref
        hass.data[DOMAIN][DOMAINDATA][eid] = {}
        # connect to the panel        
        clientTask = hass.async_create_task(client.connect())

        _LOGGER.debug("[Visonic Setup] Setting client ID for entry id {0}".format(eid))
        # save the client and its task
        hass.data[DOMAIN][DOMAINCLIENT][eid] = client
        hass.data[DOMAIN][DOMAINCLIENTTASK][eid] = clientTask

        # add update listener
        entry.async_on_unload(entry.add_update_listener(update_listener))

        #entry.runtime_data = VisonicData(client=client, client_task=clientTask)
        
        # return true to indicate success
        return True
    except requests.exceptions.ConnectionError as error:
        _LOGGER.error("Visonic Panel could not be reached: [%s]", error)
        raise ConfigEntryNotReady
    return False


# This function is called to terminate a client connection to the alarm panel
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload visonic entry."""
    _LOGGER.debug("************* terminating connection **************")

    eid = entry.entry_id

    #hass.services.async_remove(DOMAIN, ALARM_PANEL_EVENTLOG)
    #hass.services.async_remove(DOMAIN, ALARM_PANEL_RECONNECT)
    #hass.services.async_remove(DOMAIN, ALARM_PANEL_COMMAND)
    #hass.services.async_remove(DOMAIN, ALARM_SENSOR_BYPASS)
    #hass.services.async_remove(DOMAIN, ALARM_SENSOR_IMAGE)

    if DOMAIN in hass.data:
        if DOMAINCLIENT in hass.data[DOMAIN]:
            if eid in hass.data[DOMAIN][DOMAINCLIENT]:
                if client := hass.data[DOMAIN][DOMAINCLIENT][eid]:
                    clientTask = hass.data[DOMAIN][DOMAINCLIENTTASK][eid]
                    panelid = client.getPanelID()

                    # stop all activity in the client
                    await client.service_panel_stop()

                    #if updateListener is not None:
                    #    updateListener()

                    if clientTask is not None:
                        clientTask.cancel()

                    #unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

                    del hass.data[DOMAIN][DOMAINDATA][eid]
                    del hass.data[DOMAIN][DOMAINCLIENT][eid]
                    del hass.data[DOMAIN][DOMAINCLIENTTASK][eid]
                    
                    #if hass.data[DOMAIN][eid]:
                    #    hass.data[DOMAIN].pop(eid)
                    #
                    _LOGGER.debug(f"************* Panel {panelid} terminate connection success **************")
                    return True
                    #return unload_ok
                else:
                    _LOGGER.debug("************* terminate connection fail, no client **************")

            else:
                _LOGGER.debug("************* terminate connection fail, no valid eid **************")

    return False

# This function is called when there have been changes made to the parameters in the control flow
async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Edit visonic entry."""

    _LOGGER.debug("************* update connection data **************")

    if DOMAIN in hass.data:
        if DOMAINCLIENT in hass.data[DOMAIN]:
            if entry.entry_id in hass.data[DOMAIN][DOMAINCLIENT]:
                if client := hass.data[DOMAIN][DOMAINCLIENT][entry.entry_id]:
                    # combine and convert python settings map to dictionary
                    conf = await combineSettings(entry)
                    # update the client parameter set
                    client.updateConfig(conf)
    return True
