import asyncio
import logging
import contextlib

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient

from homeassistant.const import (
    CONF_NAME,
    CONF_DEVICE,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_MODE,
    CONF_HOST,
    CONF_PORT,
    CONF_TIMEOUT,
)

from .const import (
    DOMAIN,
    CONF_BAUDRATE,
    CONF_PARITY,
    CONF_STOPBITS,
    CONF_BYTESIZE,
    CONF_RETRIES,
    CONF_RECONNECT_DELAY,
    CONF_RECONNECT_DELAY_MAX,
    CONF_DEVICE_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_DEVICE_ID,
    get_sensors_for_model,
    MODEL_PV1900,
    MODEL_PH1100,
)

from .mapper import (
    convert_ph1100_partArr1,
    convert_ph1100_partArr2,
    convert_ph1100_partArr3,
    convert_ph1100_partArr4,
    convert_ph1100_workmode,
    convert_ph1100_soc_high,
    convert_ph1100_soc_low,
    convert_ph1100_antireflux,
    convert_ph1100_advmodedefault,
    convert_ph1100_adv_mode,
    convert_ph1100_rtc,
    convert_partArr2,
    convert_partArr3,
    convert_partArr4,
    convert_partArr5,
    convert_partArr6,
    convert_battery_status,
    convert_pv_data,
)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TIME,
]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the must inverter component."""
    try:
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

        inverter = MustInverter(hass, entry)

        successConnecting = await inverter.connect()

        if not successConnecting:
            raise ConfigEntryNotReady("Unable to connect to modbus device")

        successReading = await inverter._async_refresh_modbus_data()

        if not successReading:
            raise ConfigEntryNotReady("Unable to read from modbus device")

        model = inverter.model
        sensors = get_sensors_for_model(model)
        _LOGGER.debug("Setting up Must Inverter with model: %s", model)

        hass.data[DOMAIN][entry.entry_id] = {"inverter": inverter, "sensors": sensors}

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        return True
    except ConfigEntryNotReady as ex:
        raise ex
    except Exception as ex:
        _LOGGER.exception("Error setting up modbus device", exc_info=True)
        raise ConfigEntryNotReady("Unknown error connecting to modbus device") from ex


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        if monitor_task := hass.data[DOMAIN][entry.entry_id].get("monitor_task"):
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener, called when the config entry options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


class MustInverter:
    def __init__(self, hass, entry: ConfigEntry):
        self._hass = hass
        self._callbacks = []
        self._entry = entry

        common = {
            "timeout": entry.options[CONF_TIMEOUT],
            "retries": entry.options[CONF_RETRIES],
            "reconnect_delay": entry.options[CONF_RECONNECT_DELAY],
            "reconnect_delay_max": entry.options[CONF_RECONNECT_DELAY_MAX],
        }

        match entry.options[CONF_MODE]:
            case "serial":
                self._client = AsyncModbusSerialClient(
                    entry.options[CONF_DEVICE],
                    baudrate=entry.options[CONF_BAUDRATE],
                    stopbits=entry.options[CONF_STOPBITS],
                    bytesize=entry.options[CONF_BYTESIZE],
                    parity=entry.options[CONF_PARITY],
                    **common,
                )
            case "tcp":
                self._client = AsyncModbusTcpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case "udp":
                self._client = AsyncModbusUdpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case _:
                raise Exception("Invalid mode")

        self._client.rts = False
        self._client.dtr = False
        self._lock = asyncio.Lock()
        self._device_id = entry.options.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID)
        self._scan_interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self._reading = False
        self.registers = {}
        self.data = {}

    @callback
    def async_add_must_inverter_sensor(self, update_callback):
        if not self._callbacks:
            self._unsub_interval_method = async_track_time_interval(
                self._hass, self._async_refresh_modbus_data, self._scan_interval
            )

        self._callbacks.append(update_callback)

    @callback
    def async_remove_must_inverter_sensor(self, update_callback):
        self._callbacks.remove(update_callback)

        if not self._callbacks:
            self._unsub_interval_method()
            self._unsub_interval_method = None
            self.close()

    async def _async_refresh_modbus_data(self, now=None):
        if not await self._check_and_reopen():
            _LOGGER.warning("not connected, skipping refresh")
            return False

        try:
            update_result = await self.read_modbus_data()

            if update_result:
                for update_callback in self._callbacks:
                    update_callback()
        except Exception:
            _LOGGER.exception("error reading inverter data", exc_info=True)
            update_result = False

        return update_result

    @property
    def name(self):
        return self._entry.options.get(CONF_NAME, self.data.get("InverterMachineType", self.model))

    @property
    def model(self):
        from_config = self._entry.options.get(CONF_MODEL)
        detected = self.data.get("InverterMachineType")

        if from_config == "autodetect":
            return detected

        return from_config or detected

    @property
    def has_extra_registers(self):
        return self.model == MODEL_PV1900

    def close(self):
        _LOGGER.info("closing modbus client")
        self._client.close()

    async def _check_and_reopen(self):
        if not self._client.connected:
            _LOGGER.info("modbus client is not connected, trying to reconnect")
            return await self.connect()

        return self._client.connected

    async def connect(self):
        result = False

        _LOGGER.debug("connecting to %s:%s", self._client.comm_params.host, self._client.comm_params.port)

        async with self._lock:
            result = await self._client.connect()

        if result:
            _LOGGER.info(
                "successfully connected to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        else:
            _LOGGER.warning(
                "not able to connect to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        return result

    async def write_modbus_data(self, address, value):
        await self._check_and_reopen()

        _LOGGER.debug("writing modbus data: %s %s", address, value)
        async with self._lock:
            response = await self._client.write_register(address=address, value=value, device_id=self._device_id)

        if response.isError():
            _LOGGER.error("error writing modbus data: %s", response)
        else:
            _LOGGER.debug("successfully wrote modbus data: %s %s", address, value)

        return response

    async def read_modbus_data(self):
        _LOGGER.debug("reading modbus data")

        if self._reading:
            _LOGGER.warning(
                "skipping reading modbus data, previous read still in progress, make sure your scan interval is not too low"
            )
            return False
        self._reading = True

        if self.model == MODEL_PH1100:
            registersAddresses = [
                (10121, 10121, convert_ph1100_workmode),
                (10124, 10124, convert_ph1100_soc_high),
                (10125, 10125, convert_ph1100_soc_low),
                (10126, 10126, convert_ph1100_advmodedefault),
                (10102, 10123, convert_ph1100_partArr1),
                (15104, 15119, convert_ph1100_partArr2),
                (20001, 20003, convert_ph1100_partArr3),
                (25225, 25339, convert_ph1100_partArr4),
                (10127, 10149, convert_ph1100_adv_mode),
                (20201, 20207, convert_ph1100_rtc),
                (20213, 20213, convert_ph1100_antireflux),
            ]
        else:
            registersAddresses = [
                (10101, 10124, convert_partArr2),
                (15201, 15221, convert_partArr3),
                (20000, 20016, convert_partArr4),
                (20101, 20214, convert_partArr5),
                (25201, 25279, convert_partArr6),
            ]

            if self.has_extra_registers:
                registersAddresses.extend(
                    [
                        (113, 114, convert_battery_status),
                        (15207, 15208, convert_pv_data),
                        (16205, 16208, convert_pv_data),
                    ]
                )

        read = {}

        for register in registersAddresses:
            if self.has_extra_registers:
                await asyncio.sleep(0.1)

            start = register[0]
            end = register[1]
            count = end - start + 1

            try:
                _LOGGER.debug("reading modbus data from %s to %s (%s)", start, end, count)

                if not await self._check_and_reopen():
                    break

                async with self._lock:
                    response = await self._client.read_holding_registers(
                        address=start, count=count, device_id=self._device_id
                    )
                if response.isError():
                    _LOGGER.error("error reading modbus data at address %s: %s", start, response)
                elif len(response.registers) != count:
                    _LOGGER.warning(
                        "wrong number of registers read at address %s, expected %s, got %s - this usually is caused by concurrent access to modbus (usb/wifi/rs485), please remove other devices",
                        start,
                        count,
                        len(response.registers),
                    )
                    _LOGGER.debug("Received registers (wrong): %s", response.registers)

                    ir.async_create_issue(
                        self._hass,
                        DOMAIN,
                        "mismatched_registers",
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="mismatched_registers",
                        is_persistent=False,
                        learn_more_url="https://github.com/mukaschultze/ha-must-inverter/issues/47",
                    )
                else:
                    _LOGGER.debug("Correctly read %s registers from start %s", count, start)
                    for i in range(count):
                        read[start + i] = response.registers[i]
                    self.data.update(register[2](read))
            except asyncio.exceptions.CancelledError:
                _LOGGER.warning("cancelled modbus read")
                raise
            except:
                _LOGGER.error("error reading modbus data at address %s", start, exc_info=True)

        _LOGGER.debug("finished reading modbus data, %s", read)
        self.registers = read
        self._reading = False

        if self.data["InverterSerialNumber"] == 0xFFFFFFFF or self.data["InverterSerialNumber"] == 0:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                "no_serial_number",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_serial_number",
                data={"entry_id": self._entry.entry_id},
            )

        return True

    def _device_info(self):
        return {
            "identifiers": {(DOMAIN, self.data["InverterSerialNumber"])},
            "name": self.name,
            "manufacturer": "Must Solar",
            "model": self.data.get("InverterMachineType", self.model),
            "hw_version": self.data.get("InverterHardwareVersion"),
            "sw_version": self.data.get("InverterSoftwareVersion"),
            "serial_number": self.data["InverterSerialNumber"],
        }
