# RF Hunter

RTL-SDR spectrum and waterfall scanner for Raspberry Pi 5, RTL-SDR Blog V4,
and a Waveshare 3.5-inch 640×480 DSI touchscreen.

Current version: RF Hunter v0.5.8 (`rf_hunter_v058_recording_metadata.py`).
The original v0.4.3 source is retained for reference.

## Setup and launch

On Raspberry Pi OS, install the dependencies:

```sh
sudo apt install -y rtl-sdr python3-numpy python3-pygame
python3 rf_hunter_v058_recording_metadata.py
```

The installed RTL-SDR tools must support your receiver.
For a windowed simulation without radio hardware:

```sh
python3 rf_hunter_v058_recording_metadata.py --demo --windowed
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

RECORDINGS opens the touchscreen library described below. Playing a file mutes
live monitoring; tap LISTEN again to resume monitoring afterward.

Recordings split at approximately five minutes. Recording stops below 100 MiB
free space, checked at file creation and about once per second while writing.
No old recordings are automatically deleted. MENU displays available storage.
After freeing space, toggle REC off and on to retry.

System-output references: [Pygame mixer](https://www.pygame.org/docs/ref/mixer.html)
and [Raspberry Pi audio outputs](https://projects.raspberrypi.org/en/projects/raspberry-pi-using/11).

## v0.5.8 recording browser

Open MENU > RECORDINGS. The library defaults to strongest peak signal first.
Tap SORT to cycle through peak dB, average dB, date, frequency and duration.
HIGH / NEW and LOW / OLD change sort direction. Unknown measurements always
sort last. AM/NFM/ALL filters modulation, MIN PEAK filters peak signal strength,
and FREQ opens a touchscreen prefix filter (for example, `125` matches
125.xxx MHz). CLEAR then ENTER removes the frequency filter. REFRESH loads
new recordings; folder scanning runs in the background.

Tap a recording for details and PLAY, PAUSE/RESUME, STOP, volume and AUDIO RESET.
BACK returns to the list while playback continues; leaving the browser stops
playback. Files still being recorded by this scanner cannot be played from its
embedded browser. Empty/damaged WAVs are listed as unavailable.

The same browser can run independently, without starting the SDR. Run this
from a terminal on the Pi desktop (rather than a plain SSH session):

```sh
python3 ~/rf_hunter.py --recordings-only
```

Before installing as `~/rf_hunter.py`, use the versioned filename instead.
`--windowed` and `--recordings-dir /path/to/folder` also work in standalone mode.

New recordings save a matching `.json` beside each WAV when the file closes.
Keep these files together when copying recordings. Metadata includes exact
frequency, modulation, timestamp with UTC offset, duration, tuner gain,
peak and average **dB above local noise**, and completion status. These values
are RF estimates, not calibrated dBm or audio loudness. Average is the
sample-weighted arithmetic mean of dB readings, including pre-roll and quiet
tail. Each five-minute segment has its own statistics. Sidecars are written
atomically after WAV finalization; errors appear in the recording status.

Older WAVs remain browsable and playable, but RF strength is Unknown because
it was not stored. Missing, invalid or incomplete metadata never becomes an
invented dB reading. No database server or new runtime dependency is needed.

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
Twenty-three tests cover snapping, AM/NFM demodulation, monitoring/recording,
audio buffering/reset, WAV splitting/low disk space, touch routing, metadata
accuracy, numeric sorting/filtering, legacy/corrupt metadata, playback and
standalone operation without a radio worker. Dummy SDL audio/video tests and
640x480 screen inspection passed on Windows. Physical Pi RF, touchscreen and
Bluetooth audio testing remains pending. `python tests/preview_recordings.py`
renders library previews with synthetic recordings under the ignored `.qa/`
folder.
