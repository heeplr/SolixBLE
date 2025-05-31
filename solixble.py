#!/usr/bin/env python

"""
monitor SolixBLE device

"""

import asyncio
import json
import logging

from bleak import BleakScanner

import SolixBLE


logging.basicConfig(level=logging.INFO)


async def main():


    # find solix device
    devices = await SolixBLE.discover_devices()

    # no device found?
    if len(devices) <= 0:
        exit(1)


    # Initialize the device
    device = SolixBLE.SolixBLEDevice(devices[0])

    # Connect
    if not await device.connect():
        # could not connect
        exit(1)

    if not device.available:
        # connected but unable to subscribe to telemetry
        exit(1)

    # timestamp of current frame
    last_update = None

    # callback to handle state changes
    def update():
        # timestamp of last update
        nonlocal last_update
        # silently ignore duplicate updates
        if not device.last_update or device.last_update == last_update:
            return
        # prepare json output
        dataframe = {
            "time": device.last_update.strftime("%Y-%m-%dT%H:%M:%S"),
            "serial_no": device.serial_no,
            "solar_power_in": device.power_solar_in,
            "battery_percent": device.charge_percentage_battery,
            "battery_power": device.power_battery,
            "battery_temp": device.temperature_battery,
            "ac_power_out": device.power_ac_out,
            "energy_solar_total": device.energy_total_solar,
            "energy_total_battery": device.energy_total_battery,
            "energy_total_out": device.energy_total_out,
            "unknown1": device.unknown1,
            "unknown3": device.unknown3
        }
        print(json.dumps(dataframe))
        # remember timestamp of this frame
        last_update = device.last_update

    # register callback
    device.add_callback(update)

    # run until disconnected
    while True:
        await asyncio.sleep(3.0)


# -----------------------------------------------------------------------------
if __name__ == "__main__":
        asyncio.run(main())
