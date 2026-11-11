# RF Hunter

RTL-SDR spectrum and waterfall scanner for Raspberry Pi 5, RTL-SDR Blog V4,
and a Waveshare 3.5-inch 640×480 DSI touchscreen.

Current version: RF Hunter v0.5.5 (`rf_hunter_v055_waveshare_tap_to_tune.py`).
The original v0.4.3 source is retained for reference.

## Setup and launch

On Raspberry Pi OS, install the dependencies:

```sh
sudo apt install -y rtl-sdr python3-numpy python3-pygame
python3 rf_hunter_v055_waveshare_tap_to_tune.py
```

The installed RTL-SDR tools must support your receiver.
For a windowed simulation without radio hardware:

```sh
python3 rf_hunter_v055_waveshare_tap_to_tune.py --demo --windowed
```

Use `--help` for launch options.

## Touch tuning and recording

Tap the spectrum or waterfall in LIVE or WIDE to select a signal; WIDE
selections switch to LIVE. Native finger events and mouse clicks are supported.
The AUTO button selects automatic receiver gain.

Tap REC (or press R) to arm activity-triggered WAV recording in LIVE mode.
Use `--rec-on` to arm recording at launch. Recordings use AM for
118–136.975 MHz and NFM elsewhere, saved as 32 kHz, 16-bit mono WAV files
in `~/RF_Hunter_Recordings`. Recording includes approximately one second
of pre-roll and stops after about 1.1 seconds below threshold.
Audio is saved to files; speaker playback is not implemented.
Demo mode does not record. Use `--touch-debug` for touch and tuning diagnostics.

## Modes

- **LIVE:** I/Q spectrum and waterfall with touch tuning.
- **WIDE:** Sequential band surveys using `rtl_power`.
- **HITS:** Logged candidate peaks that can be selected to tune LIVE.

Readings are relative, not calibrated dBm. Wide sweeps measure frequencies
sequentially rather than capturing the whole band at once.

Settings are stored at `~/.config/rf_hunter/settings.json` and hits at
`~/rf_hunter_hits.csv`.

## Validation

Both imports were syntax-checked on Windows. The user reports v0.5.5 works
well; hardware behavior has not been independently tested here.
