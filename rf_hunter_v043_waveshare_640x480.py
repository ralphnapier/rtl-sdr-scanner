#!/usr/bin/env python3
"""RF Hunter v0.4.3 - 640x480 Waveshare DSI touchscreen edition.

Raspberry Pi 5, RTL-SDR Blog V4, Waveshare 3.5inch DSI LCD (E), Raspberry Pi OS.
Dependencies: sudo apt install -y rtl-sdr python3-numpy python3-pygame

LIVE: receive I/Q with rtl_sdr, show spectrum/waterfall, tune with touch buttons.
WIDE: stitch rtl_power frequency-hop measurements into sequential band surveys.
HITS: list logged candidate RF peaks; touch one to tune the LIVE receiver.

Only one radio worker owns the RTL-SDR. Display readings are relative, not
calibrated dBm. Wide sweeps are sequential, not instantaneous band snapshots.

Designed to replace RF Hunter v0.4.2 without changing saved settings, hit CSV,
desktop launcher arguments, or radio-processing behavior.
"""

import argparse
import csv
import json
import os
import select
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pygame

# Native Waveshare resolution and readable, capacitive-touch layout.
W, H = 640, 480
PLOT_TOP, PLOT_BOTTOM = 65, 178
WF_TOP, WF_BOTTOM = 214, 391
WF_H = WF_BOTTOM - WF_TOP
BUTTON_VIS_Y, BUTTON_VIS_H = 415, 53
BUTTON_TOUCH_Y, BUTTON_TOUCH_H = 398, H - 398
HITS_START_Y, HITS_ROW_H, HITS_VISIBLE = 96, 48, 6

SR = 1_024_000
FFT_SIZE = 4096
BLOCK_BYTES = FFT_SIZE * 2 * 8
BGC = (8, 15, 27)
PANEL = (17, 29, 46)
INK = (227, 240, 248)
MUTED = (137, 164, 186)
CYAN = (75, 223, 244)
GREEN = (65, 230, 148)
ORANGE = (255, 193, 99)
RED = (244, 131, 123)

AUTO_GAIN = -1.0  # Internal sentinel: omit -g for automatic receiver gain.
GAIN_STEPS = (AUTO_GAIN, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4,
              15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7,
              32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.9,
              44.5, 48.0, 49.6)
# Name, low MHz, high MHz, maximum bin kHz, integration seconds.
PRESETS = (
    ('2M 144-148', 144.0, 148.0, 10, 4),
    ('VHF 150-174', 150.0, 174.0, 25, 10),
    ('VHF 144-174', 144.0, 174.0, 25, 12),
    ('70CM 420-450', 420.0, 450.0, 25, 12),
    ('AIR 118-137', 118.0, 137.0, 25, 10),
    ('WIDE 100-200', 100.0, 200.0, 100, 30),
)
SETTINGS_FILE = Path.home() / '.config' / 'rf_hunter' / 'settings.json'
HITS_FILE = Path.home() / 'rf_hunter_hits.csv'
VIS_STEP, VIS_MIN, VIS_MAX = 3, 0, 24


def nearest_gain(value):
    # Legacy settings saved 0.0 for AUTO; retain that behavior.
    value = float(value)
    return AUTO_GAIN if value <= 0 else min(GAIN_STEPS[1:], key=lambda x: abs(x - value))


def rtl_gain_args(gain):
    return [] if float(gain) == AUTO_GAIN else ['-g', str(gain)]


def gain_label(gain):
    return 'AUTO' if float(gain) == AUTO_GAIN else f'{float(gain):.1f}'


def next_gain(value, change):
    i = GAIN_STEPS.index(nearest_gain(value))
    return GAIN_STEPS[max(0, min(len(GAIN_STEPS) - 1, i + change))]


def load_settings():
    try:
        value = json.loads(SETTINGS_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def save_settings(gain, boost, preset):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps({'gain_db': gain,
                                    'visual_boost': boost,
                                    'preset': preset}))
        temp.replace(SETTINGS_FILE)
    except OSError:
        pass


def build_palette():
    pts = [(0, (7, 14, 26)), (34, (14, 27, 58)),
           (75, (18, 78, 146)), (115, (40, 159, 209)),
           (155, (145, 87, 173)), (205, (249, 112, 66)),
           (235, (255, 210, 91)), (255, (255, 250, 226))]
    xs = np.arange(256)
    return np.stack([np.interp(xs, [p[0] for p in pts], [p[1][i] for p in pts])
                     for i in range(3)], axis=1).astype(np.uint8)


PALETTE = build_palette()
FFT_WINDOW = np.hanning(FFT_SIZE).astype(np.float32)
FFT_FREQS = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, d=1 / SR))
VALID = ((np.abs(FFT_FREQS) > 15_000) & (np.abs(FFT_FREQS) < SR * 0.45))
PIXEL_EDGES = np.arange(W, dtype=np.int32) * FFT_SIZE // W


def spectrum_from_bytes(data):
    """Process interleaved unsigned RTL I/Q into relative FFT power."""
    a = np.frombuffer(data, dtype=np.uint8)
    n = a.size // (FFT_SIZE * 2)
    if not n:
        return None
    a = a[:n * FFT_SIZE * 2]
    real = (a[0::2].astype(np.float32) - 127.5) / 127.5
    imag = (a[1::2].astype(np.float32) - 127.5) / 127.5
    iq = (real + 1j * imag).reshape(n, FFT_SIZE)
    iq -= iq.mean(axis=1, keepdims=True)
    powers = np.abs(np.fft.fftshift(np.fft.fft(iq * FFT_WINDOW, axis=1), axes=1)) ** 2
    mean = powers.mean(axis=0)
    return (10 * np.log10(np.maximum(mean, 1e-18)) -
            20 * np.log10(np.sum(FFT_WINDOW))).astype(np.float32)


def pix_peaks(spectrum):
    """Bin the 4096-point FFT to native display width; keep narrow peaks."""
    return np.maximum.reduceat(spectrum, PIXEL_EDGES)


def waterfall_colors(samples_db, baseline, boost):
    span = max(9., 35. - boost)
    return np.clip((samples_db - baseline - 2.) * (255. / span), 0, 255).astype(np.uint8)


def parse_power_line(line):
    """Read one rtl_power CSV frequency hop, excluding spurious edge bins."""
    try:
        row = next(csv.reader([line]))
        if len(row) < 7:
            return None
        timestamp = row[0].strip() + ' ' + row[1].strip()
        low, high, step = float(row[2]), float(row[3]), float(row[4])
        if not (0 < step < 3e6 and high > low):
            return None
        values = np.fromiter((float(x) for x in row[6:] if x.strip()),
                             dtype=np.float32)
        if not values.size:
            return None
        frequencies = low + step * (np.arange(values.size, dtype=np.float64) + 0.5)
        valid = np.isfinite(values) & (frequencies >= low) & (frequencies < high)
        if not valid.any():
            return None
        return timestamp, frequencies[valid], values[valid], step
    except (ValueError, IndexError, csv.Error, OverflowError):
        return None


def stitch_survey(rows, lo_mhz, hi_mhz):
    """Aggregate frequency hops into native-width display pixels."""
    if not rows:
        return None
    low, high = lo_mhz * 1e6, hi_mhz * 1e6
    freq_parts, db_parts = [], []
    for f, p, _step in rows:
        mask = (f >= low) & (f < high) & np.isfinite(p)
        if np.any(mask):
            freq_parts.append(f[mask])
            db_parts.append(p[mask])
    if not freq_parts:
        return None
    frequencies = np.concatenate(freq_parts)
    values = np.concatenate(db_parts)
    indexes = np.floor((frequencies - low) / (high - low) * W).astype(np.int32)
    indexes = np.clip(indexes, 0, W - 1)
    pixels = np.full(W, -np.inf, dtype=np.float32)
    np.maximum.at(pixels, indexes, values)
    seen = np.flatnonzero(np.isfinite(pixels))
    if not len(seen):
        return None
    if len(seen) == 1:
        pixels.fill(pixels[seen[0]])
    else:
        pixels = np.interp(np.arange(W), seen, pixels[seen]).astype(np.float32)
    return pixels, frequencies, values


def find_hits(frequencies, values, threshold_db, limit=12):
    """Return candidate RF peaks above local baseline, not verified signals."""
    if len(values) < 9:
        return []
    order = np.argsort(frequencies)
    f, s = frequencies[order], values[order]
    steps = np.diff(f)
    steps = steps[steps > 0]
    spacing = float(np.median(steps)) if len(steps) else 25000.
    radius = max(5, min(45, round(400000 / max(1., spacing))))
    padded = np.pad(s, (radius, radius), mode='edge')
    rolling = np.lib.stride_tricks.sliding_window_view(padded, radius * 2 + 1)
    background = np.median(rolling, axis=1)
    above = s - background
    candidates = np.flatnonzero((above >= threshold_db) &
                                (s >= np.r_[s[0], s[:-1]]) &
                                (s >= np.r_[s[1:], s[-1]]))
    chosen = []
    exclusion = max(spacing * 2.2, 20000.)
    for index in sorted(candidates, key=lambda j: above[j], reverse=True):
        if any(abs(f[index] - hit['hz']) < exclusion for hit in chosen):
            continue
        chosen.append({'hz': float(f[index]), 'db': round(float(above[index]), 1),
                       'level': round(float(s[index]), 1), 'bin_hz': round(spacing)})
        if len(chosen) >= limit:
            break
    return chosen


def log_hits(timestamp, preset, gain_db, hits):
    if not hits:
        return
    try:
        exists = HITS_FILE.exists() and HITS_FILE.stat().st_size > 0
        with HITS_FILE.open('a', newline='') as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(['time_local', 'band_low_mhz', 'band_high_mhz',
                                 'frequency_mhz', 'above_local_noise_db',
                                 'relative_level_db', 'rf_gain_db', 'bin_hz'])
            for hit in hits:
                writer.writerow([timestamp, preset[1], preset[2],
                                 f"{hit['hz'] / 1e6:.6f}", hit['db'], hit['level'],
                                 gain_label(gain_db), hit['bin_hz']])
    except OSError as exc:
        print(f'RF Hunter: could not save hits CSV: {exc}', flush=True)


def previous_hits(limit=1000):
    items = deque(maxlen=limit)
    try:
        with HITS_FILE.open(newline='') as handle:
            for row in csv.DictReader(handle):
                try:
                    items.append({'time': row['time_local'],
                                  'hz': float(row['frequency_mhz']) * 1e6,
                                  'db': float(row['above_local_noise_db']),
                                  'bin_hz': float(row.get('bin_hz') or 25000)})
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return items

class RadioEngine:
    """Single worker owns rtl_sdr or rtl_power; mode changes close old process."""
    RETUNE_DELAY = 0.22
    threshold_db = 8.0

    def __init__(self, center_hz, gain, preset_id, mode='live'):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.target = (mode, int(center_hz), float(gain), int(preset_id), False)
        self.target_changed_at = time.monotonic() - 1.
        self.version = 0
        self.data = None
        self.state = 'STARTING'
        self.error = ''
        self.last_sweep = None
        self.history = previous_hits()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def change(self, mode=None, center=None, gain=None, preset=None, paused=None):
        with self.lock:
            prev = self.target
            new = (mode if mode is not None else prev[0],
                   int(center) if center is not None else prev[1],
                   float(gain) if gain is not None else prev[2],
                   int(preset) if preset is not None else prev[3],
                   bool(paused) if paused is not None else prev[4])
            # Changing live center while in WIDE should not restart rtl_power.
            changed = (new[0] != prev[0] or new[2:] != prev[2:] or
                       (new[0] == 'live' and new[1] != prev[1]))
            self.target = new
            if changed:
                self.target_changed_at = time.monotonic()
                if not new[4]:
                    self.state = 'TUNING'
                    self.data = None
                    self.version += 1
                else:
                    self.state = 'PAUSED'
        return changed

    def _current(self):
        with self.lock:
            return self.target, self.target_changed_at

    def snapshot(self):
        with self.lock:
            return (self.data, self.version, self.state, self.error,
                    self.last_sweep, list(self.history))

    def _publish(self, key, mode, timestamp, payload, hits=None):
        with self.lock:
            if self.target != key or self.stop.is_set():
                return False
            self.data = (mode, timestamp, payload)
            self.state = 'RECEIVING' if mode == 'live' else 'SWEEPING'
            self.error = ''
            self.version += 1
            if mode == 'wide':
                self.last_sweep = timestamp
                if hits:
                    for hit in hits:
                        self.history.append({'time': timestamp, **hit})
            return True

    @staticmethod
    def _stop_process(proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            else:
                proc.wait(timeout=0.1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if proc.stdout:
                proc.stdout.close()

    def _live(self, proc, key):
        pending = bytearray()
        last = time.monotonic()
        while not self.stop.is_set() and self._current()[0] == key:
            if proc.poll() is not None:
                return f'rtl_sdr exited (code {proc.returncode})'
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(proc.stdout.fileno(), BLOCK_BYTES)
                if not chunk:
                    return 'rtl_sdr ended its I/Q stream'
                last = time.monotonic()
                pending.extend(chunk)
                if len(pending) >= BLOCK_BYTES:
                    block = bytes(pending[:BLOCK_BYTES])
                    del pending[:BLOCK_BYTES]
                    spec = spectrum_from_bytes(block)
                    if spec is not None:
                        self._publish(key, 'live', time.strftime('%Y-%m-%d %H:%M:%S'), spec)
            if time.monotonic() - last > 6:
                return 'No RTL-SDR I/Q samples for 6s'
        return None

    def _wide(self, proc, key):
        _mode, _, gain_db, preset_id, _paused = key
        preset = PRESETS[preset_id]
        pending = bytearray()
        rows = []
        current_stamp = None
        completed_stamp = None
        last = time.monotonic()
        timeout = max(40.0, preset[4] * 4.0)

        def flush_sweep(stamp, current_rows):
            result = stitch_survey(current_rows, preset[1], preset[2])
            if result is None:
                return
            pixels, frequencies, db = result
            hits = find_hits(frequencies, db, self.threshold_db)
            if self._publish(key, 'wide', stamp, (pixels, hits), hits):
                log_hits(stamp, preset, gain_db, hits)

        while not self.stop.is_set() and self._current()[0] == key:
            if proc.poll() is not None:
                return f'rtl_power exited (code {proc.returncode})'
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return 'rtl_power ended its CSV stream'
                last = time.monotonic()
                pending.extend(chunk)
                if len(pending) > 4_000_000:
                    pending.clear()
                    rows = []
                    current_stamp = None
                    continue
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    row = parse_power_line(line.decode('ascii', errors='replace'))
                    if row is None:
                        continue
                    stamp, freqs, values, step = row
                    if stamp == completed_stamp:
                        continue
                    if current_stamp is None:
                        current_stamp = stamp
                    if stamp != current_stamp:
                        flush_sweep(current_stamp, rows)
                        rows = []
                        current_stamp = stamp
                    rows.append((freqs, values, step))
                    # A sweep can complete before the next timestamp arrives.
                    full_band_seen = (rows[0][0][0] <= preset[1] * 1e6 + 1_000_000 and
                                      freqs[-1] >= preset[2] * 1e6 - 400_000)
                    if full_band_seen:
                        flush_sweep(current_stamp, rows)
                        completed_stamp = current_stamp
                        rows = []
                        current_stamp = None
            if time.monotonic() - last > timeout:
                return f'No rtl_power CSV for {int(timeout)}s'
        return None

    def run(self):
        while not self.stop.is_set():
            key, changed = self._current()
            mode, center, gain, preset_id, paused = key
            if paused:
                with self.lock:
                    self.state = 'PAUSED'
                self.stop.wait(.10)
                continue
            elapsed = time.monotonic() - changed
            if elapsed < self.RETUNE_DELAY:
                self.stop.wait(min(.10, self.RETUNE_DELAY - elapsed))
                continue
            proc, problem = None, None
            try:
                if mode == 'live':
                    cmd = (['rtl_sdr', '-f', str(center), '-s', str(SR)]
                           + rtl_gain_args(gain)
                           + ['-b', str(BLOCK_BYTES), '-'])
                else:
                    preset = PRESETS[preset_id]
                    cmd = (['rtl_power', '-f',
                            f'{preset[1]:.3f}M:{preset[2]:.3f}M:{preset[3]}k']
                           + rtl_gain_args(gain)
                           + ['-c', '0.2', '-i', f'{preset[4]}s', '-'])
                with self.lock:
                    if self.target == key:
                        self.state = 'STARTING'
                with open('/tmp/rf_hunter_rtlsdr.log', 'ab', buffering=0) as log:
                    log.write(('\n' + time.strftime('%F %T') + ' ' +
                               ' '.join(cmd) + '\n').encode())
                    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=log, bufsize=0)
                    if mode == 'live':
                        problem = self._live(proc, key)
                    else:
                        problem = self._wide(proc, key)
            except (OSError, ValueError, select.error) as exc:
                problem = f'RF receiver error: {exc}'
            except Exception as exc:
                problem = f'{type(exc).__name__}: {exc}'
            finally:
                self._stop_process(proc)
            if self.stop.is_set():
                break
            if self._current()[0] != key:
                continue  # User-initiated retunes are not receiver failures.
            with self.lock:
                self.state = 'RETRYING'
                self.error = problem or 'Receiver stopped; retrying'
                self.data = None
                self.version += 1
            self.stop.wait(1.0)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5.0)
        if self.thread.is_alive():
            print('RF Hunter warning: SDR worker has not exited yet', flush=True)


class DemoEngine:
    """UI-only demonstration without SDR hardware; use --demo."""
    threshold_db = 8.

    def __init__(self, center_hz, gain, preset_id, mode='live'):
        self.target = (mode, int(center_hz), gain, preset_id, False)
        self.version = 0
        self.last_sweep = None
        self.history = deque(maxlen=1000)
        self.last = 0
        self.data = None

    def start(self):
        pass

    def change(self, mode=None, center=None, gain=None, preset=None, paused=None):
        old = self.target
        self.target = (mode if mode is not None else old[0],
                       int(center) if center is not None else old[1],
                       gain if gain is not None else old[2],
                       preset if preset is not None else old[3],
                       paused if paused is not None else old[4])
        self.version += 1
        self.data = None

    def snapshot(self):
        mode, center, _gain, preset_id, paused = self.target
        now = time.monotonic()
        interval = .09 if mode == 'live' else 1.3
        if not paused and now - self.last >= interval:
            self.last = now
            tick = time.monotonic()
            if mode == 'live':
                spec = np.random.normal(-58, 1.2, FFT_SIZE)
                for freq, level, width in [(162_550_000, 31, 2800),
                                           (162_450_000, 24 if int(tick) % 3 else 4, 3500),
                                           (162_915_000, 21 if int(tick) % 5 < 2 else 0, 4400)]:
                    spec += level * np.exp(-.5 * ((FFT_FREQS + center - freq) / width) ** 2)
                payload = spec.astype(np.float32)
            else:
                preset = PRESETS[preset_id]
                low, high = preset[1] * 1e6, preset[2] * 1e6
                freq = np.linspace(low, high, W)
                spec = np.random.normal(-58, 1.2, W)
                for signal in [146_880_000, 151_970_000, 152_915_000, 162_550_000,
                               430_100_000, 440_000_000, 122_800_000]:
                    spec += (25 if int(tick) % 7 != 0 else 3) * np.exp(
                        -.5 * ((freq - signal) / max((high - low) / 2500, 12000)) ** 2)
                payload = (spec.astype(np.float32),
                           find_hits(freq, spec, self.threshold_db))
                self.last_sweep = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                for hit in payload[1]:
                    self.history.append({'time': self.last_sweep, **hit})
            self.data = (mode, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), payload)
            self.version += 1
        state = 'PAUSED' if paused else 'SWEEPING' if mode == 'wide' else 'RECEIVING'
        return self.data, self.version, state, '', self.last_sweep, list(self.history)

    def close(self):
        pass


def label(screen, font, value, color, x, y, align='left'):
    image = font.render(str(value), True, color)
    rect = image.get_rect()
    if align == 'center':
        rect.midtop = (x, y)
    elif align == 'right':
        rect.topright = (x, y)
    else:
        rect.topleft = (x, y)
    screen.blit(image, rect)


def mhz(hz):
    return f'{hz / 1e6:.3f}'


def main():
    parser = argparse.ArgumentParser(description='RF Hunter v0.4.3 640x480 live SDR + WIDE survey')
    parser.add_argument('--center', type=float, default=162.700,
                        help='Initial live center frequency, MHz')
    parser.add_argument('--mode', choices=('live', 'wide'), default='live')
    parser.add_argument('--preset', type=int, default=None,
                        help=f'Wide preset index (0-{len(PRESETS)-1})')
    parser.add_argument('--gain', type=float, default=None,
                        help='Tuner gain in dB; 0 or -1 selects AUTO')
    parser.add_argument('--visual-boost', type=int, default=None)
    parser.add_argument('--threshold', type=float, default=8.0,
                        help='dB above local baseline for candidate activity')
    parser.add_argument('--windowed', action='store_true')
    parser.add_argument('--demo', action='store_true', help='Simulated RF, no SDR hardware')
    parser.add_argument('--screenshot', type=str, default=None,
                        help='Save screenshot after 2.5s, then quit')
    args = parser.parse_args()

    settings = load_settings()
    gain_db = nearest_gain(args.gain if args.gain is not None else settings.get('gain_db', 19.7))
    vis = int(args.visual_boost if args.visual_boost is not None
              else settings.get('visual_boost', 12))
    vis = min(VIS_MAX, max(VIS_MIN, int(round(vis / VIS_STEP) * VIS_STEP)))
    preset_id = args.preset if args.preset is not None else int(settings.get('preset', 2))
    preset_id %= len(PRESETS)
    center = min(1_765_000_000, max(1_100_000, int(args.center * 1e6)))
    mode = args.mode
    previous_mode = 'wide'
    view = mode  # 'live', 'wide', or 'hits' (HITS does not change the SDR worker).
    adjust = False
    paused = False
    hit_scroll = 0
    threshold = max(3., float(args.threshold))

    pygame.init()
    screen = pygame.display.set_mode((W, H), 0 if args.windowed else pygame.FULLSCREEN)
    pygame.display.set_caption('RF Hunter v0.4.3 - 640x480')
    pygame.mouse.set_visible(True)
    clock = pygame.time.Clock()
    big = pygame.font.SysFont('DejaVu Sans', 21, bold=True)
    medium = pygame.font.SysFont('DejaVu Sans', 17)
    small = pygame.font.SysFont('DejaVu Sans', 14)
    tiny = pygame.font.SysFont('DejaVu Sans', 12)

    engine = (DemoEngine(center, gain_db, preset_id, mode) if args.demo else
              RadioEngine(center, gain_db, preset_id, mode))
    engine.threshold_db = threshold
    engine.start()
    wf = pygame.Surface((W, WF_H))
    wf.fill((7, 13, 24))
    smooth = None
    spectrum = None
    baseline = -60.
    strength = 0.
    peak_freq = center
    active_until = 0.
    last_version = -1
    last_input_ms = -1000
    first = time.monotonic()
    newest_hits = []
    last_sweep = None
    target_marker = None
    buttons = [pygame.Rect(4 + i * 128, BUTTON_VIS_Y, 120, BUTTON_VIS_H)
               for i in range(5)]
    hitboxes = [pygame.Rect(rect.x, BUTTON_TOUCH_Y, rect.width, BUTTON_TOUCH_H)
                for rect in buttons]
    ad_btn = pygame.Rect(500, 4, 64, 49)
    mode_btn = pygame.Rect(569, 4, 67, 49)

    def clear_spectrum():
        nonlocal smooth, spectrum, last_version, strength, active_until
        smooth = spectrum = None
        strength = 0.
        active_until = 0.
        last_version = -1
        wf.fill((7, 13, 24))

    def retune(frequency, preserve_marker=False):
        nonlocal center, target_marker
        if not preserve_marker:
            target_marker = None
        frequency = min(1_765_000_000, max(1_100_000, int(frequency)))
        if frequency != center:
            center = frequency
            engine.change(center=center)
            clear_spectrum()

    def switch(target):
        nonlocal mode, view, adjust, paused, previous_mode, hit_scroll
        if target == 'hits':
            if view != 'hits':
                previous_mode = mode
            view = 'hits'
            adjust = False
            hit_scroll = 0
            return
        if mode != target or paused:
            engine.change(mode=target, paused=False)
            clear_spectrum()
        mode, view = target, target
        paused = False
        adjust = False

    def change_preset(direction):
        nonlocal preset_id
        preset_id = (preset_id + direction) % len(PRESETS)
        engine.change(preset=preset_id)
        clear_spectrum()

    def change_gain(direction):
        nonlocal gain_db
        new_gain = next_gain(gain_db, direction)
        if new_gain != gain_db:
            gain_db = new_gain
            engine.change(gain=gain_db)
            clear_spectrum()

    def change_vis(delta):
        nonlocal vis
        vis = max(VIS_MIN, min(VIS_MAX, vis + delta))

    def tune_from_x(x):
        nonlocal target_marker
        if mode != 'wide':
            return
        preset = PRESETS[preset_id]
        frequency = round((preset[1] + (min(W - 1, max(0, x)) / (W - 1)) *
                           (preset[2] - preset[1])) * 1000) * 1000
        # The DC notch is at live center; offset chosen signal by 100kHz.
        live_center = (frequency + 100_000 if frequency < 1_764_900_000
                       else frequency - 100_000)
        retune(live_center, preserve_marker=True)
        target_marker = frequency
        switch('live')

    def button_press(index, history):
        nonlocal adjust, paused, view, hit_scroll
        if adjust:
            if index == 0:
                change_gain(-1)
            elif index == 1:
                change_gain(+1)
            elif index == 2:
                change_vis(-VIS_STEP)
            elif index == 3:
                change_vis(VIS_STEP)
            elif index == 4:
                adjust = False
            return True
        if view == 'live':
            if index == 4:
                return False
            delta = [-1_000_000, +1_000_000, -100_000, +100_000][index]
            retune(center + delta)
        elif view == 'wide':
            if index == 0:
                change_preset(-1)
            elif index == 1:
                change_preset(+1)
            elif index == 2:
                switch('hits')
            elif index == 3:
                paused = not paused
                engine.change(paused=paused)
            elif index == 4:
                return False
        else:  # HITS
            if index == 0:
                hit_scroll = max(0, hit_scroll - HITS_VISIBLE)
            elif index == 1:
                hit_scroll = min(max(0, len(history) - HITS_VISIBLE),
                                 hit_scroll + HITS_VISIBLE)
            elif index == 2:
                switch('live')
            elif index == 3:
                switch('wide')
            elif index == 4:
                return False
        return True

    running = True
    try:
        while running:
            frame, version, state, error, stamp, history = engine.snapshot()
            last_sweep = stamp or last_sweep
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key == pygame.K_TAB:
                        adjust = not adjust
                    elif event.key == pygame.K_w:
                        switch('wide')
                    elif event.key == pygame.K_l:
                        switch('live')
                    elif event.key == pygame.K_g:
                        change_gain(+1)
                    elif event.key == pygame.K_h:
                        change_gain(-1)
                    elif event.key == pygame.K_v:
                        change_vis(+VIS_STEP)
                    elif event.key == pygame.K_b:
                        change_vis(-VIS_STEP)
                    elif event.key in (pygame.K_LEFT, pygame.K_RIGHT) and view == 'live':
                        retune(center + (-100_000 if event.key == pygame.K_LEFT else 100_000))
                    elif event.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN) and view == 'live':
                        retune(center + (1_000_000 if event.key == pygame.K_PAGEUP
                                         else -1_000_000))
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    ms = pygame.time.get_ticks()
                    if ms - last_input_ms < 90:
                        continue
                    last_input_ms = ms
                    x, y = event.pos
                    if ad_btn.collidepoint(x, y):
                        adjust = not adjust
                    elif mode_btn.collidepoint(x, y):
                        switch('live' if mode == 'wide' else 'wide')
                    elif y >= BUTTON_TOUCH_Y:
                        for i, box in enumerate(hitboxes):
                            if box.collidepoint(x, y):
                                running = button_press(i, history)
                                break
                    elif view == 'wide' and (WF_TOP <= y < BUTTON_TOUCH_Y or
                                             PLOT_TOP <= y < PLOT_BOTTOM):
                        tune_from_x(x)
                    elif view == 'hits':
                        row = (y - HITS_START_Y) // HITS_ROW_H
                        if 0 <= row < HITS_VISIBLE and (HITS_START_Y <= y <
                                                         HITS_START_Y + HITS_VISIBLE * HITS_ROW_H):
                            items = list(reversed(history))
                            at = hit_scroll + row
                            if at < len(items):
                                picked = int(items[at]['hz'])
                                retune(picked + 100_000, preserve_marker=True)
                                target_marker = picked
                                switch('live')

            if frame is not None and version != last_version:
                last_version = version
                frame_mode, _frame_stamp, data = frame
                if frame_mode == mode:
                    if mode == 'live':
                        smooth = (data.copy() if smooth is None
                                  else smooth * 0.65 + data * 0.35)
                        spectrum = pix_peaks(smooth)
                        baseline = float(np.median(smooth[VALID]))
                        peak_index = int(np.argmax(np.where(VALID, smooth, -1e6)))
                        peak_freq = center + FFT_FREQS[peak_index]
                        strength = float(smooth[peak_index] - baseline)
                        if strength >= threshold:
                            active_until = time.monotonic() + .35
                    else:
                        spectrum, newest_hits = data
                        baseline = float(np.median(spectrum))
                        preset = PRESETS[preset_id]
                        peak_index = int(np.argmax(spectrum))
                        peak_freq = ((preset[1] + (peak_index + .5) / W *
                                      (preset[2] - preset[1])) * 1e6)
                        strength = float(spectrum[peak_index] - baseline)
                        if newest_hits:
                            active_until = time.monotonic() + max(1, preset[4])
                    if spectrum is not None:
                        color_codes = waterfall_colors(spectrum, baseline, vis)
                        wf.scroll(dy=1)
                        pixels = pygame.surfarray.pixels3d(wf)
                        pixels[:, 0, :] = PALETTE[color_codes]
                        del pixels

            screen.fill(BGC)
            label(screen, big, 'RF HUNTER', INK, 9, 7)
            active = ((mode == 'wide' and bool(newest_hits)) or
                      (mode == 'live' and time.monotonic() < active_until))
            status = 'ACTIVITY' if active else ('QUIET' if spectrum is not None else state)
            label(screen, tiny, status, GREEN if active else MUTED, 488, 12, 'right')
            for rect, title, color in (
                    (ad_btn, 'TUNE' if adjust else 'ADJ', (42, 100, 112)),
                    (mode_btn, 'LIVE' if mode == 'wide' else 'WIDE', (52, 82, 124))):
                pygame.draw.rect(screen, color, rect, border_radius=7)
                label(screen, small, title, INK, rect.centerx, rect.y + 15, 'center')
            if mode == 'live':
                label(screen, small, f'CENTER {mhz(center)} MHz', CYAN, 9, 38)
            else:
                preset = PRESETS[preset_id]
                title = f'{preset[0]} MHz' if 'MHz' not in preset[0] else preset[0]
                label(screen, small, title, CYAN, 9, 38)
            label(screen, tiny, f'RF {gain_label(gain_db)}  VIS +{vis}', MUTED,
                  440, 40, 'right')

            # Spectrum plot, axes, and target annotation.
            pygame.draw.rect(screen, PANEL, (0, PLOT_TOP, W, PLOT_BOTTOM - PLOT_TOP))
            for x in (W // 4, W // 2, 3 * W // 4):
                pygame.draw.line(screen, (40, 58, 76), (x, PLOT_TOP), (x, PLOT_BOTTOM))
            for y in (93, 121, 149):
                pygame.draw.line(screen, (36, 51, 68), (0, y), (W, y))
            if spectrum is not None:
                low_db, high_db = baseline - 8, baseline + 39
                plot_y = np.clip(PLOT_BOTTOM - 3 - (spectrum - low_db) *
                                 ((PLOT_BOTTOM - PLOT_TOP - 7) / (high_db - low_db)),
                                 PLOT_TOP + 2, PLOT_BOTTOM - 2).astype(np.int32)
                pygame.draw.lines(screen, CYAN, False,
                                  [(i, int(y)) for i, y in enumerate(plot_y)], 1)
                if mode == 'live':
                    label(screen, tiny,
                          f'PEAK {mhz(peak_freq)} MHz  +{strength:.0f} dB',
                          GREEN if active else ORANGE, 8, 160)
                else:
                    label(screen, tiny,
                          f'{len(newest_hits)} candidate hits  |  max +{strength:.0f} dB',
                          GREEN if newest_hits else MUTED, 8, 160)
            elif error:
                label(screen, small, error[:85], RED, 8, 123)
            else:
                waiting = ('Paused' if state == 'PAUSED' else
                           'Waiting for sweep...' if mode == 'wide' else 'Opening RTL-SDR...')
                label(screen, medium, waiting, MUTED, 9, 122)

            if mode == 'live':
                low_edge, center_mark, high_edge = center - SR / 2, center, center + SR / 2
            else:
                preset = PRESETS[preset_id]
                low_edge = preset[1] * 1e6
                center_mark = (preset[1] + preset[2]) * 5e5
                high_edge = preset[2] * 1e6
            label(screen, small, mhz(low_edge), MUTED, 8, 183)
            label(screen, small, mhz(center_mark), MUTED, W // 2, 183, 'center')
            label(screen, small, mhz(high_edge), MUTED, W - 8, 183, 'right')
            if view == 'live' and target_marker is not None:
                pos = int((target_marker - (center - SR / 2)) / SR * W)
                if 0 <= pos < W:
                    pygame.draw.line(screen, ORANGE, (pos, PLOT_TOP), (pos, PLOT_BOTTOM), 1)
                    label(screen, tiny, f'TARGET {mhz(target_marker)} MHz', ORANGE, 9, 200)

            # Either extended waterfall (177 samples tall) or six-row HITS list.
            if view == 'hits':
                screen.blit(wf, (0, WF_TOP))
                pygame.draw.rect(screen, PANEL, (7, 65, W - 14, 332), border_radius=6)
                label(screen, medium, f'WIDE HITS  ({len(history)} saved/session)',
                      INK, 13, 69)
                items = list(reversed(history))
                for idx in range(HITS_VISIBLE):
                    position = hit_scroll + idx
                    if position >= len(items):
                        break
                    hit = items[position]
                    y = HITS_START_Y + idx * HITS_ROW_H
                    row_col = (28, 48, 67) if idx % 2 == 0 else (20, 38, 56)
                    pygame.draw.rect(screen, row_col, (12, y, W - 24, 44), border_radius=4)
                    label(screen, medium, f"{mhz(hit['hz'])} MHz", CYAN, 21, y + 4)
                    label(screen, medium, f"+{hit['db']:.0f} dB", GREEN,
                          W - 21, y + 4, 'right')
                    label(screen, tiny, hit['time'][11:19] + '  tap to tune',
                          MUTED, 21, y + 27)
            else:
                screen.blit(wf, (0, WF_TOP))
                pygame.draw.line(screen, (60, 90, 111), (0, WF_TOP), (W, WF_TOP))
                if mode == 'wide':
                    short = ('PAUSED' if paused else
                             f'LAST {last_sweep[11:19]}' if last_sweep else 'FIRST SWEEP...')
                    label(screen, tiny, short, INK, W - 7, 393, 'right')
            if view == 'live' and target_marker is not None:
                pos = int((target_marker - (center - SR / 2)) / SR * W)
                if 0 <= pos < W:
                    pygame.draw.line(screen, ORANGE, (pos, WF_TOP), (pos, WF_BOTTOM), 1)

            if adjust:
                names = ['RF -', 'RF +', 'VIS -', 'VIS +', 'BACK']
            elif view == 'live':
                names = ['-1 MHz', '+1 MHz', '-100 k', '+100 k', 'EXIT']
            elif view == 'wide':
                names = ['BAND <', 'BAND >', 'HITS', 'RESUME' if paused else 'PAUSE', 'EXIT']
            else:
                names = ['UP', 'DOWN', 'LIVE', 'WIDE', 'EXIT']
            for idx, rect in enumerate(buttons):
                color = ((44, 78, 111) if idx < 4 else
                         (46, 97, 82) if adjust else (105, 49, 62))
                pygame.draw.rect(screen, color, rect, border_radius=9)
                pygame.draw.rect(screen, (80, 113, 139), rect, width=1, border_radius=9)
                label(screen, medium, names[idx], INK,
                      rect.centerx, rect.y + 17, 'center')

            pygame.display.flip()
            if args.screenshot and time.monotonic() - first > 2.5:
                pygame.image.save(screen, args.screenshot)
                running = False
            clock.tick(15)
    finally:
        engine.close()
        save_settings(gain_db, vis, preset_id)
        pygame.quit()


if __name__ == '__main__':
    main()
