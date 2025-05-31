# SolixBLE

Python module for monitoring Anker Solix Solarbank power stations over Bluetooth based on
the [code from flip-dots](https://github.com/flip-dots/SolixBLE).

 - 👌 Free software: MIT license
 - 🍝 Sauce: https://github.com/heeplr/SolixBLE


This Python module enables you to monitor Anker Solix Solarbank devices directly
from your computer, without the need for any cloud services or Anker app.
It leverages the Bleak library to interact with Bluetooth Anker Solix power stations.
No pairing is required in order to receive telemetry data.


## Features

- 🔋 Battery percentage, charge/discharge, temperature
- ⚡ Total Power In/Out
- 🔌 Total AC Power Out
- 🚗 Total Solar Power In/Out
- ☀️ Solar Power In
(- 💡 Light bar status)
- 🔂 Simple structure
- ✔️ More emojis than strictly necessary


## Supported Devices

- Solarbank 2 Pro
- Maybe more? IDK


## Requirements

- 🐍 Python 3.11+
- 📶 Bleak 0.19.0+
- 📶 bleak-retry-connector


## Supported Operating Systems

- 🐧 Linux (BlueZ)
  - Ubuntu Desktop
  - Arch (HomeAssistant OS)
- 🏢 Windows
  - Windows 10 
- 💾 Mac OSX
  - Maybe?


## Installation


### PIP

```
pip install SolixBLE
```


### Manual

SolixBLE consists of a single file (SolixBLE.py) which you can simply put in the
same directory as your program. If you are using manual installation make sure
the dependencies are installed as well.

```
pip install bleak bleak-retry-connector
```

```solixble.py``` is a standalone example that outputs status messages as one json dictionary per line.
