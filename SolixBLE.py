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
RECONNECT_ATTEMPTS_MAX = -1

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
        self._timer_ac: int | None = None
        self._timer_dc: int | None = None
        self._remain_hours: float | None = None
        self._remain_days: int | None = None
        self._power_ac_in: int | None = None
        self._power_ac_out: int | None = None
        self._power_usb_c1: int | None = None
        self._power_usb_c2: int | None = None
        self._power_usb_c3: int | None = None
        self._power_usb_a1: int | None = None
        self._power_dc_out: int | None = None
        self._power_solar_in: int | None = None
        self._power_in: int | None = None
        self._power_out: int | None = None
        self._status_solar: int | None = None
        self._battery_percentage: int | None = None
        self._status_usb_c1: int | None = None
        self._status_usb_c2: int | None = None
        self._status_usb_c3: int | None = None
        self._status_usb_a1: int | None = None
        self._status_dc_out: int | None = None
        self._status_light: int | None = None
        self._data: bytes | None = None
        self._last_data_timestamp: datetime | None = None
        self._supports_telemetry: bool = False
        self._state_changed_callbacks: list[Callable[[], None]] = []
        self._reconnect_task: asyncio.Task | None = None
        self._expect_disconnect: bool = True
        self._connection_attempts: int = 0

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
    def ac_timer_remaining(self) -> int:
        """Time remaining on AC timer.

        :returns: Seconds remaining or default int value.
        """
        return self._timer_ac if self._timer_ac is not None else DEFAULT_METADATA_INT

    @property
    def ac_timer(self) -> datetime | None:
        """Timestamp of AC timer.

        :returns: Timestamp of when AC timer expires or None.
        """
        if self._timer_ac is None or self._timer_ac == 0:
            return None
        return datetime.now() + timedelta(seconds=self._timer_ac)

    @property
    def dc_timer_remaining(self) -> int:
        """Time remaining on DC timer.

        :returns: Seconds remaining or default int value.
        """
        return self._timer_dc if self._timer_dc is not None else DEFAULT_METADATA_INT

    @property
    def dc_timer(self) -> datetime | None:
        """Timestamp of DC timer.

        :returns: Timestamp of when DC timer expires or None.
        """
        if self._timer_dc is None or self._timer_dc == 0:
            return None
        return datetime.now() + timedelta(seconds=self._timer_dc)

    @property
    def hours_remaining(self) -> float:
        """Time remaining to full/empty.

        Note that any hours over 24 are overflowed to the
        days remaining. Use time_remaining if you want
        days to be included.

        :returns: Hours remaining or default float value.
        """
        return (
            self._remain_hours
            if self._remain_hours is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def days_remaining(self) -> int:
        """Time remaining to full/empty.

        :returns: Days remaining or default int value.
        """
        return (
            self._remain_days if self._remain_days is not None else DEFAULT_METADATA_INT
        )

    @property
    def time_remaining(self) -> float:
        """Time remaining to full/empty.

        This includes any hours which were overflowed
        into days.

        :returns: Hours remaining or default float value.
        """
        if self._remain_hours is None or self._remain_days is None:
            return DEFAULT_METADATA_FLOAT

        return (self._remain_days * 24) + self._remain_hours

    @property
    def timestamp_remaining(self) -> datetime | None:
        """Timestamp of when device will be full/empty.

        :returns: Timestamp of when will be full/empty or None.
        """
        if self._remain_hours is None or self._remain_days is None:
            return None
        return datetime.now() + timedelta(
            days=self._remain_days, hours=self._remain_hours
        )

    @property
    def ac_power_in(self) -> int:
        """AC Power In.

        :returns: Total AC power in or default int value.
        """
        return (
            self._power_ac_in if self._power_ac_in is not None else DEFAULT_METADATA_INT
        )

    @property
    def ac_power_out(self) -> int:
        """AC Power Out.

        :returns: Total AC power out or default int value.
        """
        return (
            self._power_ac_out
            if self._power_ac_out is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_c1_power(self) -> int:
        """USB C1 Power.

        :returns: USB port C1 power or default int value.
        """
        return (
            self._power_usb_c1
            if self._power_usb_c1 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_c2_power(self) -> int:
        """USB C2 Power.

        :returns: USB port C2 power or default int value.
        """
        return (
            self._power_usb_c2
            if self._power_usb_c2 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_c3_power(self) -> int:
        """USB C3 Power.

        :returns: USB port C3 power or default int value.
        """
        return (
            self._power_usb_c3
            if self._power_usb_c3 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_a1_power(self) -> int:
        """USB A1 Power.

        :returns: USB port A1 power or default int value.
        """
        return (
            self._power_usb_a1
            if self._power_usb_a1 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def dc_power_out(self) -> int:
        """DC Power Out.

        :returns: DC power out or default int value.
        """
        return (
            self._power_dc_out
            if self._power_ac_out is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def solar_power_in(self) -> int:
        """Solar Power In.

        :returns: Total solar power in or default int value.
        """
        return (
            self._power_solar_in
            if self._power_solar_in is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def power_in(self) -> int:
        """Total Power In.

        :returns: Total power in or default int value.
        """
        return self._power_in if self._power_in is not None else DEFAULT_METADATA_INT

    @property
    def power_out(self) -> int:
        """Total Power Out.

        :returns: Total power out or default int value.
        """
        return self._power_out if self._power_out is not None else DEFAULT_METADATA_INT

    @property
    def solar_port(self) -> PortStatus:
        """Solar Port Status.

        :returns: Status of the solar port.
        """
        return PortStatus(
            self._status_solar
            if self._status_solar is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def battery_percentage(self) -> int:
        """Battery Percentage.

        :returns: Percentage charge of battery or default int value.
        """
        return (
            self._battery_percentage
            if self._battery_percentage is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_port_c1(self) -> PortStatus:
        """USB C1 Port Status.

        :returns: Status of the USB C1 port.
        """
        return PortStatus(
            self._status_usb_c1
            if self._status_usb_c1 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_port_c2(self) -> PortStatus:
        """USB C2 Port Status.

        :returns: Status of the USB C2 port.
        """
        return PortStatus(
            self._status_usb_c2
            if self._status_usb_c2 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_port_c3(self) -> PortStatus:
        """USB C3 Port Status.

        :returns: Status of the USB C3 port.
        """
        return PortStatus(
            self._status_usb_c3
            if self._status_usb_c3 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def usb_port_a1(self) -> PortStatus:
        """USB A1 Port Status.

        :returns: Status of the USB A1 port.
        """
        return PortStatus(
            self._status_usb_a1
            if self._status_usb_a1 is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def dc_port(self) -> PortStatus:
        """DC Port Status.

        :returns: Status of the DC port.
        """
        return PortStatus(
            self._status_dc_out
            if self._status_dc_out is not None
            else DEFAULT_METADATA_INT
        )

    @property
    def light(self) -> LightStatus:
        """Light Status.

        :returns: Status of the light bar.
        """
        return LightStatus(
            self._status_light
            if self._status_light is not None
            else DEFAULT_METADATA_INT
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

    def _parse_int(self, index: int) -> int:
        """Parse a 16-bit integer at the index in the telemetry bytes.

        :param index: Index of 16-bit integer in array.
        :returns: 16-bit integer.
        :raises IndexError: If index is out of range.
        """
        return int.from_bytes(self._data[index : index + 2], byteorder="little")

    def _parse_telemetry(self, data: bytearray) -> None:
        """Update internal values using the telemetry data.

        :param data: Bytes from status update message.
        """

        # If the size is wrong then it is not a telemetry message
        if len(data) != EXPECTED_TELEMETRY_SIZE:
            _LOGGER.debug(
                f"Data is not telemetry data. The size is wrong ({len(data)} != {EXPECTED_TELEMETRY_SIZE})"
            )
            return

        self._data = data
        self._last_data_timestamp = datetime.now()
        self._timer_ac = self._parse_int(16)
        self._timer_dc = self._parse_int(23)
        self._remain_hours = data[30] / 10.0
        self._remain_days = data[31]
        self._power_ac_in = self._parse_int(35)
        self._power_ac_out = self._parse_int(40)
        self._power_usb_c1 = data[45]
        self._power_usb_c2 = data[50]
        self._power_usb_c3 = data[55]
        self._power_usb_a1 = data[60]
        self._power_dc_out = data[65]
        self._power_solar_in = self._parse_int(70)
        self._power_in = self._parse_int(75)
        self._power_out = self._parse_int(80)
        self._status_solar = data[129]
        self._battery_percentage = data[141]
        self._status_usb_c1 = data[149]
        self._status_usb_c2 = data[153]
        self._status_usb_c3 = data[157]
        self._status_usb_a1 = data[161]
        self._status_dc_out = data[165]
        self._status_light = data[241]

        _LOGGER.debug(
            f"\n===== STATUS UPDATE ({self.name}) =====\n"
            f"TIMER AC: {self._timer_ac}\n"
            f"TIMER DC: {self._timer_dc}\n"
            f"REMAINING HOURS: {self._remain_hours}\n"
            f"REMAINING DAYS: {self._remain_days}\n"
            f"POWER AC IN: {self._power_ac_in}\n"
            f"POWER AC OUT: {self._power_ac_out}\n"
            f"POWER USB C1: {self._power_usb_c1}\n"
            f"POWER USB C2: {self._power_usb_c2}\n"
            f"POWER USB C3: {self._power_usb_c3}\n"
            f"POWER USB A1: {self._power_usb_a1}\n"
            f"POWER DC OUT: {self._power_dc_out}\n"
            f"POWER SOLAR IN: {self._power_solar_in}\n"
            f"POWER IN: {self._power_in}\n"
            f"POWER OUT: {self._power_out}\n"
            f"STATUS SOLAR: {self._status_solar}\n"
            f"BATTERY PERCENTAGE: {self._battery_percentage}\n"
            f"STATUS USB C1: {self._status_usb_c1}\n"
            f"STATUS USB C2: {self._status_usb_c2}\n"
            f"STATUS USB C3: {self._status_usb_c3}\n"
            f"STATUS USB A1: {self._status_usb_a1}\n"
            f"STATUS DC OUT: {self._status_dc_out}\n"
            f"STATUS LIGHT: {self._status_light}"
        )

    def _run_state_changed_callbacks(self) -> None:
        """Execute all registered callbacks for a state change."""
        for function in self._state_changed_callbacks:
            function()

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
            self._run_state_changed_callbacks()

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
