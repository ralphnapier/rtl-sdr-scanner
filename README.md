# RF Hunter

RTL-SDR spectrum and waterfall scanner for Raspberry Pi 5, RTL-SDR Blog V4,
and a Waveshare 3.5-inch 640×480 DSI touchscreen.

Initial source: RF Hunter v0.4.3 (`rf_hunter_v043_waveshare_640x480.py`).

## Setup and launch

On Raspberry Pi OS, install the dependencies:

```sh
sudo apt install -y rtl-sdr python3-numpy python3-pygame
python3 rf_hunter_v043_waveshare_640x480.py
```

The installed RTL-SDR tools must support your receiver.
For a windowed simulation without radio hardware:

```sh
python3 rf_hunter_v043_waveshare_640x480.py --demo --windowed
```

Use `--help` for launch options.

## Modes

- **LIVE:** I/Q spectrum and waterfall with touch tuning.
- **WIDE:** Sequential band surveys using `rtl_power`.
- **HITS:** Logged candidate peaks that can be selected to tune LIVE.

Readings are relative, not calibrated dBm. Wide sweeps measure frequencies
sequentially rather than capturing the whole band at once.

Settings are stored at `~/.config/rf_hunter/settings.json` and hits at
`~/rf_hunter_hits.csv`.

## Validation

The initial import was syntax-checked on Windows. Receiver operation and
touchscreen behavior require validation on the Raspberry Pi hardware.
