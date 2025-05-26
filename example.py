"""Example usage of SolixBLE.

.. moduleauthor:: Harvey Lelliott (flip-dots) <harveylelliott@duck.com>

"""

import asyncio
import logging

from bleak import BleakScanner

import SolixBLE


logging.basicConfig(level=logging.DEBUG)


async def main():

    # Find device
    devices = await SolixBLE.discover_devices()

    # Initialize the device
    device = SolixBLE.SolixBLEDevice(devices[0])

    # Connect
    connected = await device.connect()

    if not connected:
        raise Exception

    # Do nothing, the library will print status updates in debug mode
    await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main())
