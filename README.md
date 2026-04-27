# ble-gatt-server

Simple app to simulate sensor sending data over BLE (Nordic UART Service).

- **Windows:** WinRT GATT peripheral (`ble/nus_winrt.py`).
- **Linux:** BlueZ via [Bless](https://github.com/kevincar/bless) (`ble/nus_bless.py`). Bluetooth adapter must be available; you may need to run from an account that can use the system D-Bus (often `bluetooth` group membership on Linux).
- **macOS:** Bless with Core Bluetooth (same code path as Linux).

## Requirements

- **Python 3.10+**
- Bluetooth enabled on the machine.

## Local setup

Create a virtual environment and install dependencies (markers pick WinRT vs Bless).

**Linux / macOS** — use `python3`, not `py`. The `py` command on many Linux distros is a different utility and does not support `py -3 -m venv`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r ble/requirements.txt
```

**Windows** — either of:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r ble\requirements.txt
```

Or, if the [Python launcher for Windows](https://docs.python.org/3/using/windows.html#python-launcher-for-windows) (`py.exe`) is on your PATH:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r ble\requirements.txt
```

## Run

With the venv activated, from the repository root:

```bash
python ble/main.py
```

On Windows:

```bat
python ble\main.py
```

You need a graphical session (tkinter). Allow Bluetooth access if the OS prompts you.
