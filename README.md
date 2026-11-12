# RF Hunter

RTL-SDR spectrum and waterfall scanner for Raspberry Pi 5, RTL-SDR Blog V4,
and a Waveshare 3.5-inch 640×480 DSI touchscreen.

Current version: RF Hunter v0.6.3 (`rf_hunter_v063_frequency_names.py`).
The original v0.4.3 source is retained for reference.

## Setup and launch

On Raspberry Pi OS, install the dependencies:

```sh
sudo apt install -y rtl-sdr python3-numpy python3-pygame python3-gpiozero python3-lgpio
python3 rf_hunter_v063_frequency_names.py
```

The installed RTL-SDR tools must support your receiver.
For a windowed simulation without radio hardware:

```sh
python3 rf_hunter_v063_frequency_names.py --demo --windowed
```

Use `--help` for launch options.

## v0.6.3 saved frequency names

Open **MENU > NAMES**. **SAVE CURRENT** stores the listening frequency;
**RENAME** opens a touchscreen keyboard for your own name; **CLEAR** removes
that manual name so a downloaded name can show again. Tap a saved row to
select it, then **TUNE** to listen. Named frequencies are shown next to the
LIVE target and in the HITS list. Names are stored locally at
`~/.config/rf_hunter/frequencies.json` and survive upgrades. Your own names
always take priority over downloaded names. The list holds up to 500 entries.

**UPDATE NAMES** looks for active analog repeaters within 50 km of Florence,
Kentucky, using [Open Repeater](https://www.openrepeater.org/docs). It updates
only frequencies you have saved, skips channels with multiple possible
repeaters, and never changes your own names. This is a manual, online update;
the scanner keeps running while it downloads. Repeater data is reference
information, not proof that a particular signal comes from that repeater.

Open Repeater requires a free personal API key. Register and find your key from
its [API page](https://www.openrepeater.org/api-docs), then on the Pi create
`~/.config/rf_hunter/openrepeater.key` containing only your `or_...` key:

```sh
mkdir -p ~/.config/rf_hunter
nano ~/.config/rf_hunter/openrepeater.key
chmod 600 ~/.config/rf_hunter/openrepeater.key
```

In nano, paste the key, press Ctrl+O, Enter, then Ctrl+X. Keep this key on the
Pi; never add it to GitHub. Open Repeater currently allows 30 API requests per
day per key. A refresh uses up to three requests. A missing key, network error,
limit, or unexpected response is shown on the NAMES screen without disturbing
the saved list.

RepeaterBook currently requires approved, token-based API access for client
apps; its public pages cannot be used to build a local repeater directory.
See [RepeaterBook API policy](https://www.repeaterbook.com/wiki/doku.php?id=api).

## v0.6.2 diagnostics overlay

Tap **DIAG** at the top right of the waterfall to show or hide a translucent
health card. It displays overall CPU use, used/total RAM, and up to two thermal
sensors reported by the Pi. Readings refresh once per second while visible. The
button works in LIVE and WIDE, including when the display/touch is rotated 180°.
The `D` key also toggles it. No extra Python package is needed.

The RTL-SDR does not expose a temperature reading through this program. The card
labels that reading as unavailable; Pi sensors that are absent or unreadable are
shown as unavailable. The overlay starts hidden on each launch.

## v0.6.1 WIDE receiver recovery

When `rtl_power` ends its CSV stream, RF Hunter reads its exit code and recent
error output, closes the process, and tries to reopen it. Three consecutive
short WIDE failures trigger one USB-level reset of the SDR dongle, followed by
a new receive attempt. A brief sweep that fails before two completed sweeps
still counts toward recovery. Two completed sweeps clear the failure streak.
The reset is rate-limited and will not repeat until successful reception has
been observed. Retries back off to at most ten seconds when failures continue.

The reset only targets a single detected Realtek `0bda:2838` USB device. It
never cycles the Pi's entire USB hub. Missing or multiple matching devices,
unsupported platforms, or missing permissions leave software retries active
and display a reason for the skipped reset. `--no-usb-recovery` disables USB
resets. A USB-level reset does not remove power; a hardware fault may still
need a physical power cycle or a power/cable investigation.

Receiver diagnostics now survive reboot in
`~/.local/state/rf_hunter/receiver.log`. On the Pi, read the last entries with:

```sh
tail -n 80 ~/.local/state/rf_hunter/receiver.log
```

If it remains stuck, that log and `dmesg -T | tail -n 80` can help distinguish
receiver, USB, and power issues. The earlier version's temporary log was at
`/tmp/rf_hunter_rtlsdr.log` and is lost on reboot.

Reference: [Linux USB device reset API](https://docs.kernel.org/6.12/driver-api/usb/usb.html).

## v0.6.0 rotary tuning dial

The KY-040-style encoder uses these fixed BCM GPIO assignments. Shut down and
unplug the Pi before wiring; make sure these GPIOs are not used by other hardware.

| Module label | Pi connection | Physical header pin |
| --- | --- | --- |
| CLK | GPIO17 | 11 |
| DT | GPIO27 | 13 |
| SW | GPIO22 | 15 |
| + | **3.3 V (not 5 V)** | 1 |
| GND | Ground | 6 |

Encoder inputs are enabled on normal launch unless disabled in saved settings.
GPIO Zero uses lgpio with input pull-ups and a debounced pushbutton. The header
GPIO chip is found by its controller label rather than assuming a chip number.
If GPIO support is missing or unavailable, the scanner still opens with touch
controls. MENU > ENCODER shows status/errors and has ENABLE/DISABLE, direction
reversal, RETRY GPIO, and a step selector. GPIO ready means the inputs opened;
it does not prove the physical encoder is connected.

- Turn to tune the listening frequency. Turning from WIDE/HITS switches to LIVE.
- Short click/release snaps to the **nearest** qualifying local peak within
  +/-12.5 kHz. Peaks must exceed local background by at least 8 dB or the current
  squelch, whichever is higher. This is an RF activity estimate, not proof of a
  transmitter. No qualifying peak leaves tuning unchanged.
- Click snapping requires LIVE mode. It waits up to 1.5 seconds for fresh data
  after a retune. A later tune cancels a pending snap. DC/edge-invalid bins and
  coarse/gapped data are excluded.
- Hold approximately 0.8 seconds to cycle 1 / 2.5 / 5 / 8.333 / 12.5 / 25 kHz
  steps. The 8.333 kHz setting is approximate, not aviation channel numbering.
  Releasing after a hold does not also snap.
- Direction, enabled state and step are remembered. App display rotation does
  not reverse the physical dial. Dial input is ignored in keypads and the
  recording browser; standalone browsing never claims GPIO pins.

GPIO callbacks only enqueue bounded commands. The UI applies tuning on its own
thread and combines rapid turns into one update per frame. Tuning is bounded to
1.1–1765 MHz. Stale queued input is discarded rather than applied much later.

For a wiring test from SSH, close RF Hunter first (only one process can own the
pins), then run this after installing the current file as `~/rf_hunter.py`:

```sh
python3 ~/rf_hunter.py --encoder-test
```

It prints TUNE UP/DOWN, CLICK / SNAP, or HOLD / STEP without opening the SDR or
display. Ctrl+C exits. If direction is wrong, use MENU > ENCODER > DIR or test
with `--encoder-reverse`. `--no-encoder` leaves pins unused; `--encoder` enables
them explicitly, including in demo mode. `--gpiochip 0` overrides chip detection
when the header controller is known to be chip 0. Run as your normal desktop
user with GPIO access; do not use sudo to launch the GUI/audio application.

References: [GPIO Zero rotary input](https://gpiozero.readthedocs.io/en/stable/api_input.html)
and [lgpio backend](https://gpiozero.readthedocs.io/en/stable/_modules/gpiozero/pins/lgpio.html).

## v0.5.9 display rotation

Tap MENU > ROTATE 180 to flip the application and its touch mapping together.
Tap again to return to normal. The recordings browser also has ROTATE 180 at
the top right, including in standalone mode. The choice is saved immediately
and shared by both modes, while preserving existing receiver settings.
Mouse and native finger events use the same coordinate conversion; spectrum
tuning, keypads, menus and recording rows all follow the rotation.

This rotates RF Hunter only, not the Raspberry Pi desktop. If you already
rotated the desktop and touch input in OS settings, leave the application at
normal orientation unless you want an additional flip. `--rotation 0` or
`--rotation 180` overrides the saved preference for that launch. For example:
`python3 ~/rf_hunter.py --rotation 0`. Screenshots reflect the visible rotation.

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
Squelch and volume reset to defaults on restart; the tuning step is now saved.
The 8.333 kHz step is approximate;
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

Run `python3 -m unittest discover -s tests` with NumPy, Pygame and GPIO Zero installed.
Sixty-five tests cover receiver retry and scoped USB recovery, simulated encoder quadrature/bounce, click/hold handling,
GPIO cleanup, chip discovery, terminal diagnostics, nearest-peak selection,
actual UI tuning, display/touch rotation, persisted settings, snapping, AM/NFM demodulation, monitoring/recording,
audio buffering/reset, WAV splitting/low disk space, touch routing, metadata
accuracy, numeric sorting/filtering, legacy/corrupt metadata, playback and
standalone operation without a radio worker. Dummy SDL audio/video tests and
640x480 screen inspection passed on Windows. Physical Pi RF, encoder, touchscreen
and Bluetooth audio testing remains pending. `python tests/preview_recordings.py`
renders library previews with synthetic recordings under the ignored `.qa/`
folder. `python tests/preview_encoder.py` renders the encoder settings screen
with simulated GPIO and isolated settings.
