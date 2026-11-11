# RF Hunter

RTL-SDR spectrum and waterfall scanner for Raspberry Pi 5, RTL-SDR Blog V4,
and a Waveshare 3.5-inch 640×480 DSI touchscreen.

Current version: RF Hunter v0.5.7 (`rf_hunter_v057_audio_controls.py`).
The original v0.4.3 source is retained for reference.

## Setup and launch

On Raspberry Pi OS, install the dependencies:

```sh
sudo apt install -y rtl-sdr python3-numpy python3-pygame
python3 rf_hunter_v057_audio_controls.py
```

The installed RTL-SDR tools must support your receiver.
For a windowed simulation without radio hardware:

```sh
python3 rf_hunter_v057_audio_controls.py --demo --windowed
```

Use `--help` for launch options.

## Touch tuning and recording

Tap the spectrum or waterfall in LIVE or WIDE to select a signal; WIDE
selections switch to LIVE. Native finger events and mouse clicks are supported.
The AUTO button selects automatic receiver gain.

In v0.5.6, LIVE taps snap to a nearby spectrum peak only within 12.5 kHz,
when it is at least 6 dB stronger than the tapped position and 8 dB above
the local median background. The correction uses the displayed smoothed
spectrum, excludes the DC notch and receiver edges, and happens once per tap.
Current WIDE presets are too coarse for reliable snapping, so retain exact
tap selection. Step buttons and HITS selections retain their existing behavior.
`--touch-debug` also reports frequency corrections.

Tap REC (or press R) to arm activity-triggered WAV recording in LIVE mode.
Use `--rec-on` to arm recording at launch. Recordings use AM for
118–136.975 MHz and NFM elsewhere, saved as 32 kHz, 16-bit mono WAV files
in `~/RF_Hunter_Recordings`. Recording includes approximately one second
of pre-roll and stops after about 1.1 seconds below threshold.
Demo mode does not record or generate live audio. Use `--touch-debug` for diagnostics.

## v0.5.7 controls and Bluetooth listening

Tap MENU for frequency entry, selectable fine-tuning steps, recording/listening
squelch (0–40 dB), live listening/mute, volume, recordings, and RF/VIS controls.
Squelch defaults to 8 dB independently of the hit-detection `--threshold`.
The new controls reset to defaults on restart. The 8.333 kHz step is approximate;
it is not an aviation channel-number converter. Enter actual frequencies in MHz.

To use Bluetooth, pair/connect the speaker or headphones in Raspberry Pi OS,
select it as the desktop audio output, then launch RF Hunter normally as your
desktop user (not with sudo). Tune a signal in LIVE, open MENU, and tap LISTEN.
If you changed the output while running, tap AUDIO RESET. Turn down squelch if
the selected signal is too weak to open it. Volume affects playback, not WAVs.
Listening and recording can operate together from the same I/Q stream.
Bluetooth latency and routing depend on the OS/device; hardware verification
is still needed. Audio errors appear in MENU without stopping RF acquisition.

RECORDINGS lists WAV filenames (date, time, frequency and modulation) and
durations. Tap a completed file to play it; this mutes live monitoring. STOP
AUDIO or BACK stops file playback. Tap LISTEN again to resume monitoring.
The list refreshes each time you open RECORDINGS.

Recordings split at approximately five minutes. Recording stops below 100 MiB
free space, checked at file creation and about once per second while writing.
No old recordings are automatically deleted. MENU displays available storage.
After freeing space, toggle REC off and on to retry.

System-output references: [Pygame mixer](https://www.pygame.org/docs/ref/mixer.html)
and [Raspberry Pi audio outputs](https://projects.raspberrypi.org/en/projects/raspberry-pi-using/11).

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

Run `python3 -m unittest discover -s tests` with NumPy and Pygame installed.
Twelve tests cover snapping, AM/NFM tone demodulation, simultaneous monitoring
and recording, bounded audio buffering/reset, WAV splitting/low disk space,
and touch-menu/keypad routing. Dummy SDL audio/video tests and rendered screen
inspection passed on Windows. Physical Pi RF, touchscreen and Bluetooth audio
testing remains pending.
