"""SolixBLE module.

.. moduleauthor:: Harvey Lelliott (flip-dots) <harveylelliott@duck.com>

"""

# ruff: noqa: G004
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
import logging

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.client import BaseBleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

#: GATT Service UUID for device telemetry. Is subscribable. Handle 17.
UUID_TELEMETRY = "8c850003-0302-41c5-b46e-cf057c562025"

#: GATT Service UUID for identifying Solix devices (Tested on C300X).
UUID_IDENTIFIER = "0000ff09-0000-1000-8000-00805f9b34fb"

#: Time to wait before re-connecting on an unexpected disconnect.
RECONNECT_DELAY = 3

#: Maximum number of automatic re-connection attempts the program will make.
RECONNECT_ATTEMPTS_MAX = 5

#: Time to allow for a re-connect before considering the
#: device to be disconnected and running state changed callbacks.
DISCONNECT_TIMEOUT = 30

#: Size of expected telemetry packet in bytes.
EXPECTED_TELEMETRY_SIZE = 253

#: String value for unknown string attributes.
DEFAULT_METADATA_STRING = "Unknown"

#: Int value for unknown int attributes.
DEFAULT_METADATA_INT = -1

#: Float value for unknown float attributes.
DEFAULT_METADATA_FLOAT = -1.0


_LOGGER = logging.getLogger(__name__)


async def discover_devices(
    scanner: BleakScanner | None = None, timeout: int = 5
) -> list[BLEDevice]:
    """Scan feature.

    Scans the BLE neighborhood for Solix BLE device(s) and returns
    a list of nearby devices based upon detection of a known UUID.

    :param scanner: Scanner to use. Defaults to new scanner.
    :param timeout: Time to scan for devices (default=5).
    """

    if scanner is None:
        scanner = BleakScanner

    devices = []

    def callback(device, advertising_data):
        if UUID_IDENTIFIER in advertising_data.service_uuids and device not in devices:
            devices.append(device)

    async with BleakScanner(callback) as scanner:
        await asyncio.sleep(timeout)

    return devices


class PortStatus(Enum):
    """The status of a port on the device."""

    #: The status of the port is unknown.
    UNKNOWN = -1

    #: The port is not connected.
    NOT_CONNECTED = 0

    #: The port is an output.
    OUTPUT = 1

    #: The port is an input.
    INPUT = 2

    def __repr__(self):
        match self:
            case PortStatus.NOT_CONNECTED:
                status = "NOT_CONNECTED"

            case PortStatus.OUTPUT:
                status = "OUTPUT"

            case PortStatus.INPUT:
                status = "INPUT"

            case _:
                status = "UNKNOWN"

        return f"PortStatus({status})"


class LightStatus(Enum):
    """The status of the light on the device."""

    #: The status of the light is unknown.
    UNKNOWN = -1

    #: The light is off.
    OFF = 0

    #: The light is on low.
    LOW = 1

    #: The light is on medium.
    MEDIUM = 2

    #: The light is on high.
    HIGH = 3

    def __repr__(self):
        match self:
            case LightStatus.OFF:
                status = "OFF"

            case LightStatus.LOW:
                status = "LOW"

            case LightStatus.MEDIUM:
                status = "MEDIUM"

            case LightStatus.HIGH:
                status = "HIGH"

            case _:
                status = "UNKNOWN"

        return f"LightStatus({status})"


class SolixBLEDevice:
    """Solix BLE device object."""

    def __init__(self, ble_device: BLEDevice) -> None:
        """Initialise device object. Does not connect automatically."""

        _LOGGER.debug(
            f"Initializing Solix device '{ble_device.name}' with"
            f"address '{ble_device.address}' and details '{ble_device.details}'"
        )

        self._ble_device: BLEDevice = ble_device
        self._client: BleakClient | None = None

        self._serial_no: str | None = None
        self._charge_percentage_battery: int | None = None
        self._temperature_battery: int | None = None
        self._power_battery_discharge: int | None = None
        self._energy_battery_total_in: int | None = None
        self._power_ac_out: int | None = None
        self._power_solar_in: int | None = None
        self._energy_solar_total_in: int | None = None
        self._energy_total_out: int | None = None
        self._status_solar: int | None = None
        self._status_light: int | None = None
        self.unknown1: int | None = None
        self.unknown2: int | None = None
        self.unknown3: int | None = None

        self._data: bytes | None = None
        self._last_data_timestamp: datetime | None = None
        self._supports_telemetry: bool = False
        self._state_changed_callbacks: list[Callable[[], None]] = []
        self._reconnect_task: asyncio.Task | None = None
        self._expect_disconnect: bool = True
        self._connection_attempts: int = 0
        self.timed_out: bool = False

    def add_callback(self, function: Callable[[], None]) -> None:
        """Register a callback to be run on state updates.

        Triggers include changes to pretty much anything, including,
        battery percentage, output power, solar, connection status, etc.

        :param function: Function to run on state changes.
        """
        self._state_changed_callbacks.append(function)

    def remove_callback(self, function: Callable[[], None]) -> None:
        """Remove a registered state change callback.

        :param function: Function to remove from callbacks.
        :raises ValueError: If callback does not exist.
        """
        self._state_changed_callbacks.remove(function)

    async def connect(self, max_attempts: int = 3, run_callbacks: bool = True) -> bool:
        """Connect to device.

        This will connect to the device, determine if it is supported
        and subscribe to status updates, returning True if successful.

        :param max_attempts: Maximum number of attempts to try to connect (default=3).
        :param run_callbacks: Execute registered callbacks on successful connection (default=True).
        """

        # If we are not connected then connect
        if not self.connected:
            self._connection_attempts += 1
            _LOGGER.debug(
                f"Connecting to '{self.name}' with address '{self.address}'..."
            )

            try:
                # Make a new Bleak client and connect
                self._client = await establish_connection(
                    BleakClient,
                    device=self._ble_device,
                    name=self.address,
                    max_attempts=max_attempts,
                    disconnected_callback=self._disconnect_callback,
                )

            except BleakError as e:
                _LOGGER.error(f"Error connecting to '{self.name}'. E: '{e}'")

        # If we are still not connected then we have failed
        if not self.connected:
            _LOGGER.error(
                f"Failed to connect to '{self.name}' on attempt {self._connection_attempts}!"
            )
            return False

        _LOGGER.debug(f"Connected to '{self.name}'")

        # If we are not subscribed to telemetry then check that
        # we can and then subscribe
        if not self.available:
            try:
                await self._determine_services()
                await self._subscribe_to_services()

            except BleakError as e:
                _LOGGER.error(f"Error subscribing to '{self.name}'. E: '{e}'")
                return False

        # If we are still not subscribed to telemetry then we have failed
        if not self.available:
            return False

        # Else we have succeeded
        self._expect_disconnect = False
        self._connection_attempts = 0

        # Execute callbacks if enabled
        if run_callbacks:
            self._run_state_changed_callbacks()

        return True

    async def disconnect(self) -> None:
        """Disconnect from device.

        Disconnects from device and does not execute callbacks.
        """
        self._expect_disconnect = True

        # If there is a client disconnect and throw it away
        if self._client:
            self._client.disconnect()
            self._client = None

    async def _subscribe_to_services(self) -> None:
        """Subscribe to state updates from device."""
        if self._supports_telemetry:

            def _telemetry_update(handle: int, data: bytearray) -> None:
                """Update internal state and run callbacks."""
                _LOGGER.debug(f"Received notification from '{self.name}'")
                self._parse_telemetry(data)
                self._run_state_changed_callbacks()

            await self._client.start_notify(UUID_TELEMETRY, _telemetry_update)

    async def _reconnect(self) -> None:
        """Re-connect to device and run state change callbacks on timeout/failure."""
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await asyncio.sleep(RECONNECT_DELAY)
                await self.connect(run_callbacks=False)
                if self.available:
                    _LOGGER.debug(f"Successfully re-connected to '{self.name}'")

        except TimeoutError as e:
            _LOGGER.error(f"Failed to re-connect to '{self.name}'. E: '{e}'")
            self.timed_out = True
            self._run_state_changed_callbacks()

    def _run_state_changed_callbacks(self) -> None:
        """Execute all registered callbacks for a state change."""
        for function in self._state_changed_callbacks:
            function()

    def _disconnect_callback(self, client: BaseBleakClient) -> None:
        """Re-connect on unexpected disconnect and run callbacks on failure.

        This function will re-connect if this is not an expected
        disconnect and if it fails to re-connect it will run
        state changed callbacks. If the re-connect is successful then
        no callbacks are executed.

        :param client: Bleak client.
        """

        # Ignore disconnect callbacks from old clients
        if client != self._client:
            return

        # Reset to false to ensure we
        self._supports_telemetry = False

        # If we expected the disconnect then we don't try to reconnect.
        if self._expect_disconnect:
            _LOGGER.info(f"Received expected disconnect from '{client}'.")
            return

        # Else we did not expect the disconnect and must re-connect if
        # there are attempts remaining
        _LOGGER.debug(f"Unexpected disconnect from '{client}'.")
        if (
            RECONNECT_ATTEMPTS_MAX == -1
            or self._connection_attempts < RECONNECT_ATTEMPTS_MAX
        ):
            # Try and reconnect
            self._reconnect_task = asyncio.create_task(self._reconnect())

        else:
            _LOGGER.warning(
                f"Maximum re-connect attempts to '{client}' exceeded. Auto re-connect disabled!"
            )

    async def _determine_services(self) -> None:
        """Determine GATT services available on the device."""

        # Print services
        services = self._client.services
        for service_id, service in services.services.items():
            _LOGGER.debug(
                f"ID: {service_id} Service: {service}, description: {service.description}"
            )

            if service.characteristics is None:
                continue

            for char in service.characteristics:
                _LOGGER.debug(
                    f"Characteristic: {char}, "
                    f"description: {char.description}, "
                    f"descriptors: {char.descriptors}"
                )

        # Populate supported services
        self._supports_telemetry = bool(services.get_characteristic(UUID_TELEMETRY))
        if not self._supports_telemetry:
            _LOGGER.warning(
                f"Device '{self.name}' does not support the telemetry characteristic!"
            )

    @property
    def connected(self) -> bool:
        """Connected to device.

        :returns: True/False if connected to device.
        """
        return self._client is not None and self._client.is_connected

    @property
    def available(self) -> bool:
        """Connected to device and receiving data from it.

        :returns: True/False if the device is connected and sending telemetry.
        """
        return self.connected and self.supports_telemetry

    @property
    def supports_telemetry(self) -> bool:
        """Device supports the libraries telemetry standard.

        :returns: True/False if telemetry supported.
        """
        return self._supports_telemetry

    @property
    def last_update(self) -> datetime | None:
        """Timestamp of last telemetry data update from device.

        :returns: Timestamp of last update or None.
        """
        return self._last_data_timestamp

    @property
    def address(self) -> str:
        """MAC address of device.

        :returns: The Bluetooth MAC address of the device.
        """
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Bluetooth name of the device.

        :returns: The name of the device or default string value.
        """
        return self._ble_device.name or DEFAULT_METADATA_STRING

    @property
    def serial_no(self) -> str:
        """Anker serial number of the device.

        :returns: 10 character serial number of the device or default string value.
        """
        return self._serial_no or DEFAULT_METADATA_STRING

    @property
    def power_ac_out(self) -> int:
        """AC Power Out.

        :returns: Total AC power out or default int value.
        """
        return (
            self._power_ac_out
            if self._power_ac_out is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def power_solar_in(self) -> int:
        """Solar Power In.

        :returns: Total solar power in or default int value.
        """
        return (
            self._power_solar_in
            if self._power_solar_in is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def power_battery(self) -> int:
        """Battery charged power (negative = discharge).

        :returns: Total power battery is charged with or default int value.
        """
        return (
            self._power_solar_in - self._power_ac_out
            if self._power_solar_in is not None and self._power_ac_out is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def power_battery_discharge(self) -> int:
        return (
            self._power_battery_discharge
            if self._power_battery_discharge is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def temperature_battery(self) -> int:
        """current temperature of battery"""
        return (
            self._temperature_battery
            if self._temperature_battery is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def charge_percentage_battery(self) -> int:
        """Battery charge percentage

        :returns: Percentage charge of battery or default int value.
        """
        return (
            self._charge_percentage_battery
            if self._charge_percentage_battery is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def energy_total_solar(self) -> int:
        """Solar energy produced

        :returns: Total amount of solar energy output since system setup in kWh
        """
        return (
            self._energy_solar_total_in
            if self._energy_solar_total_in is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def energy_total_battery(self) -> int:
        """Battery energy stored

        :returns: Total amount of battery storage since system setup in kWh
        """
        return (
            self._energy_battery_total_in
            if self._energy_battery_total_in is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def energy_total_out(self) -> int:
        """Overall energy output

        :returns: Total amount of energy output since system setup in kWh
        """
        return (
            self._energy_total_out
            if self._energy_total_out is not None
            else DEFAULT_METADATA_INT
        )

    # ~ @property
    # ~ def status_port_solar(self) -> PortStatus:
        # ~ """Solar Port Status.

        # ~ :returns: Status of the solar port.
        # ~ """
        # ~ return PortStatus(
            # ~ self._status_solar
            # ~ if self._status_solar is not None
            # ~ else DEFAULT_METADATA_INT
        # ~ )

    # ~ @property
    # ~ def status_light(self) -> LightStatus:
        # ~ """Light Status.

        # ~ :returns: Status of the light bar.
        # ~ """
        # ~ return LightStatus(
            # ~ self._status_light
            # ~ if self._status_light is not None
            # ~ else DEFAULT_METADATA_INT
        # ~ )

    def _parse_int(self, index: int) -> int:
        """Parse a 16-bit integer at the index in the telemetry bytes.

        :param index: Index of 16-bit integer in array.
        :returns: 16-bit integer.
        :raises IndexError: If index is out of range.
        """
        return int.from_bytes(self._data[index : index + 2], byteorder="little")

    def _parse_int32(self, index: int) -> int:
        """Parse a 32-bit integer at the index in the telemetry bytes.

        :param index: Index of 32-bit integer in array.
        :returns: 32-bit integer.
        :raises IndexError: If index is out of range.
        """
        return int.from_bytes(self._data[index : index + 4], byteorder="little")

    def _parse_telemetry(self, data: bytearray) -> None:
        """Update internal values using the telemetry data.

        :param data: Bytes from status update message.
        """

        # dump all data to stderr
        # (@todo remove this if data for analysis isn't needed anymore)
        _LOGGER.info(''.join(format(x, '02x') for x in data))

        # If the size is wrong then it is not a telemetry message
        if len(data) != EXPECTED_TELEMETRY_SIZE:
            _LOGGER.debug(
                f"Data is not telemetry data. The size is wrong ({len(data)} != {EXPECTED_TELEMETRY_SIZE})"
            )
            return

        # not correct message type?
        if data[8] != 0x05 or data[9] != 0x13:
            # silently ignore message
            _LOGGER.debug(
                f"Unknown message type: 0x{data[8]:02x}{data[9]:02x}"
            )
            return

        self._data = data
        self._last_data_timestamp = datetime.now()

        # ascii serial number
        self._serial_no = data[0x10:0x20].decode('ascii')
        # battery charge status [percent]
        self._charge_percentage_battery = data[35]
        self.unknown1 = data[39]
        # battery temperature [°C]
        self._temperature_battery = data[73]
        # total solar power input [W * 10]
        self._power_solar_in = self._parse_int(77) / 10.0
        # total AC power output to home [W * 10]
        self._power_ac_out = self._parse_int(84) / 10.0
        # battery charge status [percent]
        # (maybe total of all batteries if more than one battery connected?)
        #self.unknown2 = data[91]
        self.unknown3 = self._parse_int32(103) / 100000.0
        # total solar (Wh * 10)
        self._energy_solar_total_in = self._parse_int32(110) / 10000.0
        # total amount of energy stored in battery (Wh * 100)
        self._energy_battery_total_in = self._parse_int32(117) / 100000.0
        # total amount of energy output (Wh * 10)
        self._energy_total_out = self._parse_int32(124) / 10000.0
        # battery discharge power (W * 100)
        self._power_battery_discharge = self._parse_int32(132) / 100.0

        _LOGGER.debug(
            f"\n===== STATUS UPDATE ({self.name}) =====\n"
            f"SERIAL NUMBER: {self._serial_no}\n"
            f"BATTERY PERCENTAGE: {self._charge_percentage_battery}\n"
            f"BATTERY_TEMPERATURE: {self._temperature_battery}\n"
            f"POWER SOLAR IN: {self._power_solar_in}\n"
            f"POWER AC OUT: {self._power_ac_out}\n"
            f"TOTAL SOLAR IN: {self._energy_solar_total_in}\n"
            f"TOTAL BATTERY OUT: {self._energy_battery_total_in}\n"
            f"TOTAL OUT: {self._energy_total_out}\n"
            f"BATTERY DISCHARGE: {self._power_battery_discharge}\n"
            f"UNKNOWN1: {self.unknown1}\n"
            f"UNKNOWN3: {self.unknown3}\n"
        )


