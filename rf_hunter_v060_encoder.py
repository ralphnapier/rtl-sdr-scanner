#!/usr/bin/env python3
"""RF Hunter v0.6.0 - 640x480 Waveshare touchscreen, AM/NFM WAV recorder.

Raspberry Pi 5, RTL-SDR Blog V4, Waveshare 3.5inch DSI LCD (E), Raspberry Pi OS.
Dependencies: sudo apt install -y rtl-sdr python3-numpy python3-pygame
Optional tuning dial: sudo apt install -y python3-gpiozero python3-lgpio
KY-040: CLK GPIO17, DT GPIO27, SW GPIO22, + 3.3V, GND ground.

LIVE: tap spectrum/waterfall to retune to a signal or use step buttons.
WIDE: stitch rtl_power surveys; tap spectrum/waterfall to tune into LIVE.
HITS: list logged candidate RF peaks; touch one to tune the LIVE receiver.
Touch input supports SDL mouse events and native capacitive finger events.

Only one radio worker owns the RTL-SDR. Display readings are relative, not
calibrated dBm. Wide sweeps are sequential, not instantaneous band snapshots.

LIVE audio: use MENU / LISTEN for system-output monitoring (including Bluetooth).
Arm REC to make automatic activity-triggered mono WAV recordings.
118.000-136.975 MHz uses AM; other frequencies use narrowband FM.
Uses existing rtl_sdr I/Q stream, without opening the SDR a second time.
The recording implementation is newly reconstructed, not an exact copy of v0.5.2.
Existing settings, hit CSV, desktop launcher arguments, and older WAVs are kept.
"""

import argparse
import csv
import json
import math
import re
import queue
import shutil
import sys
import os
import select
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime, timedelta
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
RECORDINGS_DIR = Path.home() / 'RF_Hunter_Recordings'
AUDIO_RATE = 32000
AUDIO_DECIMATION = SR // AUDIO_RATE
AUDIO_FIR_TAPS = 513
REC_PREROLL_SECONDS = 1.0
REC_ATTACK_SECONDS = 0.16
REC_RELEASE_SECONDS = 1.1
MAX_RECORD_SECONDS = 300
MIN_FREE_BYTES = 100 * 1024 * 1024
TUNE_STEPS = (1000, 2500, 5000, 8333, 12500, 25000)
ENCODER_HOLD_SECONDS = .8



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
        temp.write_text(json.dumps({**load_settings(), 'gain_db': gain,
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



def demodulation_for_frequency(frequency_hz):
    """Civil airband channels use AM, remaining channels use NFM."""
    return 'AM' if 118_000_000 <= int(frequency_hz) <= 136_975_000 else 'NFM'


def channel_snr_db(spectrum_db, tuned_offset_hz):
    """Estimate band-limited carrier power relative to nearby noise.

    Uses average LINEAR channel power rather than the strongest FFT bin,
    helping prevent noise spikes elsewhere in the 1-MHz display from
    repeatedly opening the recording squelch.
    """
    distances = np.abs(FFT_FREQS - float(tuned_offset_hz))
    inside = spectrum_db[distances <= 8500.]
    outside = spectrum_db[(distances >= 35_000.) & (distances <= 170_000.)]
    if not len(inside) or not len(outside):
        return -100.0
    reference = float(np.median(outside))
    powers = 10.0 ** (np.clip((inside.astype(np.float64) - reference) / 10.0,
                                 -10., 7.))
    return float(10.0 * np.log10(max(np.mean(powers), 1e-10)))


class ChannelAudio:
    """Extract one narrow 32-kHz audio channel from rtl_sdr's 1.024 MS/s I/Q.

    Uses a 513-tap windowed-sinc lowpass + FFT overlap-save and 32:1
    decimation before AM envelope detection or quadrature FM demodulation.
    All operations are NumPy + Python stdlib, preserving installation deps.
    """

    def __init__(self, center_hz, listen_hz):
        self.center_hz = int(center_hz)
        self.listen_hz = int(listen_hz)
        self.demod = demodulation_for_frequency(listen_hz)
        self.offset = self.listen_hz - self.center_hz
        self.block_samples = BLOCK_BYTES // 2
        self.mix_phase = 1.+0.j
        if self.offset:
            omega = -2. * np.pi * self.offset / SR
            self.mix = np.exp(1j * omega * np.arange(self.block_samples)).astype(np.complex64)
            self.phase_advance = complex(np.exp(1j * omega * self.block_samples))
        else:
            self.mix = None
            self.phase_advance = 1.+0.j

        self.tail = np.zeros(AUDIO_FIR_TAPS - 1, dtype=np.complex64)
        # Conservative speech-band lowpass with steep enough transition to
        # reject neighboring stations before a factor-32 decimation.
        n = np.arange(AUDIO_FIR_TAPS) - (AUDIO_FIR_TAPS - 1) / 2
        taps = (2. * 10_000. / SR * np.sinc(2. * 10_000. / SR * n) *
                np.hamming(AUDIO_FIR_TAPS))
        taps /= taps.sum()
        self.fft_n = 1 << (self.block_samples + AUDIO_FIR_TAPS - 2).bit_length()
        self.filter_fft = np.fft.fft(taps, self.fft_n)
        self.last_sample = np.complex64(1.+0.j)
        self.am_level = None
        self.hp_prev_in = 0.
        self.hp_prev_out = 0.
        self.lp_prev_out = 0.
        self.hp_decay = float(np.exp(-2. * np.pi * 165. / AUDIO_RATE))
        self.lp_alpha = float(1. - np.exp(-2. * np.pi * (2700. if self.demod == 'NFM' else 3500.) / AUDIO_RATE))

    def _filter_audio(self, samples):
        """High-pass to remove carrier/DC, low-pass voice and scale to WAV."""
        out = np.empty(samples.size, dtype=np.float32)
        prior_in, prior_hp, prior_lp = (self.hp_prev_in, self.hp_prev_out,
                                       self.lp_prev_out)
        for i, value in enumerate(samples):
            value = float(value)
            hp = self.hp_decay * (prior_hp + value - prior_in)
            lp = prior_lp + self.lp_alpha * (hp - prior_lp)
            out[i] = lp
            prior_in, prior_hp, prior_lp = value, hp, lp
        self.hp_prev_in, self.hp_prev_out, self.lp_prev_out = (prior_in, prior_hp, prior_lp)
        pcm = np.clip(out * 0.85, -1.0, 1.0)
        return (pcm * 28000.).astype('<i2').tobytes()

    def demodulate(self, block):
        raw = np.frombuffer(block, dtype=np.uint8)
        if raw.size != self.block_samples * 2:
            raise ValueError('Unexpected RTL I/Q block length')
        i = (raw[0::2].astype(np.float32) - 127.5) / 127.5
        q = (raw[1::2].astype(np.float32) - 127.5) / 127.5
        mixed = (i + 1j * q).astype(np.complex64)
        if self.mix is not None:
            mixed *= self.mix * self.mix_phase
            self.mix_phase *= self.phase_advance
            self.mix_phase /= abs(self.mix_phase)
        block_in = np.concatenate((self.tail, mixed))
        filtered = np.fft.ifft(np.fft.fft(block_in, self.fft_n) * self.filter_fft)
        baseband = filtered[AUDIO_FIR_TAPS - 1:
                            AUDIO_FIR_TAPS - 1 + self.block_samples:AUDIO_DECIMATION]
        self.tail = mixed[-(AUDIO_FIR_TAPS - 1):].copy()

        if self.demod == 'AM':
            envelope = np.abs(baseband)
            level = float(np.mean(envelope))
            self.am_level = (level if self.am_level is None else
                             0.92 * self.am_level + 0.08 * level)
            audio = (envelope - self.am_level) / max(self.am_level, 1e-4)
        else:
            prior = np.empty_like(baseband)
            prior[0] = self.last_sample
            prior[1:] = baseband[:-1]
            self.last_sample = baseband[-1]
            phase = np.angle(baseband * np.conjugate(prior))
            # NFM voice deviation usually ~2.5 to 5 kHz.
            audio = (phase * AUDIO_RATE / (2. * np.pi * 5000.)).astype(np.float32)
        return self._filter_audio(audio)


class ActivityRecorder:
    """Write activity-gated audio with pre-roll, with one complete WAV per burst."""

    def __init__(self, listen_hz, threshold_db, gain_db=None):
        self.listen_hz = int(listen_hz)
        self.modulation = demodulation_for_frequency(listen_hz)
        self.threshold_db = float(threshold_db)
        self.gain_db = gain_db
        self.recorded_at = None
        self.signal_peak = None
        self.signal_total = 0.
        self.signal_samples = 0
        block_duration = (BLOCK_BYTES // 2) / SR
        self.block_duration = block_duration
        self.previous_audio = deque(maxlen=max(1, round(REC_PREROLL_SECONDS / block_duration)))
        self.hot_time = 0.
        self.quiet_time = 0.
        self.handle = None
        self.file_name = ''
        self.last_file = ''
        self.started = 0.
        self.samples = 0
        self.files_created = 0
        self.error = ''
        self.disabled = False

    @property
    def active(self):
        return self.handle is not None

    @property
    def elapsed(self):
        return self.samples / AUDIO_RATE if self.active else 0.

    def _start(self):
        try:
            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(RECORDINGS_DIR).free < MIN_FREE_BYTES:
                raise OSError('Less than 100 MiB free; recording stopped')
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            mhz_label = f'{self.listen_hz / 1e6:.3f}MHz'
            # Modulation is the final filename component, for compatibility.
            seq = 1
            while True:
                filename = f'RF_{stamp}_{mhz_label}_{seq:03d}_{self.modulation}.wav'
                path = RECORDINGS_DIR / filename
                if not path.exists():
                    break
                seq += 1
            self.handle = wave.open(str(path), 'wb')
            self.handle.setnchannels(1)
            self.handle.setsampwidth(2)
            self.handle.setframerate(AUDIO_RATE)
            self.file_name = str(path)
            self.last_file = str(path)
            self.files_created += 1
            self.samples = 0
            self.signal_peak = None
            self.signal_total = 0.
            self.signal_samples = 0
            preroll = sum(len(pcm) // 2 for pcm, _ in self.previous_audio) / AUDIO_RATE
            self.recorded_at = (datetime.now().astimezone() - timedelta(seconds=preroll)).isoformat()
            for old_pcm, old_signal in self.previous_audio:
                self._write(old_pcm, old_signal)
                if not self.active:
                    break
        except (OSError, wave.Error) as exc:
            self.error = f'Could not save WAV: {exc}'
            self.disabled = True
            self.close()

    def _write(self, pcm, signal_db):
        if self.handle is None:
            return
        try:
            if self.samples % AUDIO_RATE < len(pcm) // 2:
                if shutil.disk_usage(RECORDINGS_DIR).free < MIN_FREE_BYTES:
                    raise OSError('Less than 100 MiB free; recording stopped')
            self.handle.writeframesraw(pcm)
            self.samples += len(pcm) // 2
            if math.isfinite(signal_db):
                self.signal_peak = (float(signal_db) if self.signal_peak is None
                                    else max(self.signal_peak, float(signal_db)))
                self.signal_total += float(signal_db) * (len(pcm) // 2)
                self.signal_samples += len(pcm) // 2
        except (OSError, wave.Error) as exc:
            self.error = f'WAV write failed: {exc}'
            self.disabled = True
            self.close()

    def feed(self, pcm, signal_db):
        if self.disabled:
            return
        if self.active and self.elapsed >= MAX_RECORD_SECONDS:
            self.close()
            self._start()
        self.previous_audio.append((pcm, float(signal_db)))
        signal = float(signal_db) >= self.threshold_db
        if signal:
            self.hot_time += self.block_duration
            self.quiet_time = 0.
        else:
            self.hot_time = 0.
            self.quiet_time += self.block_duration

        if not self.active and self.hot_time >= REC_ATTACK_SECONDS:
            self._start()  # includes current block in the previous_audio deque
        elif self.active:
            self._write(pcm, signal_db)

        if self.active and self.quiet_time >= REC_RELEASE_SECONDS:
            self.close()

    def close(self):
        if self.handle is not None:
            complete = True
            try:
                self.handle.close()  # fixes RIFF chunk length in WAV header
            except (OSError, wave.Error) as exc:
                self.error = f'WAV close failed: {exc}'
                complete = False
            finally:
                metadata = {
                    'schema_version': 1, 'application_version': '0.6.0',
                    'file': Path(self.file_name).name, 'recorded_at': self.recorded_at,
                    'frequency_hz': self.listen_hz, 'modulation': self.modulation,
                    'duration_seconds': self.samples / AUDIO_RATE,
                    'sample_rate_hz': AUDIO_RATE, 'gain_db': self.gain_db,
                    'squelch_db_at_close': self.threshold_db,
                    'peak_signal_db': self.signal_peak,
                    'mean_signal_db': (self.signal_total / self.signal_samples
                                       if self.signal_samples else None),
                    'signal_units': 'dB above local noise',
                    'signal_mean_method': 'sample-weighted arithmetic mean of dB values; includes pre-roll and quiet tail',
                    'complete': complete and not self.disabled,
                }
                if self.file_name:
                    try:
                        target = Path(self.file_name).with_suffix('.json')
                        temp = target.with_suffix('.json.tmp')
                        temp.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding='utf-8')
                        temp.replace(target)
                    except (OSError, ValueError) as exc:
                        self.error = f'WAV saved; metadata failed: {exc}'
                self.handle = None
                self.file_name = ''
                self.samples = 0
        self.previous_audio.clear()
        self.hot_time = 0.
        self.quiet_time = 0.


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
                except (ValueError, KeyError, TypeError):
                    pass
    except OSError:
        pass
    return items

class AudioOutput:
    """Bounded PCM playback through SDL's default system output, including Bluetooth.

    The radio worker never waits for the output device. Four 32 ms blocks per
    Sound tolerate UI scheduling jitter; overflow drops audio instead of RF data.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = bytearray()
        self.error = ''
        self.volume = .5
        self.channel = None

    def reset(self):
        with self.lock:
            self.pending.clear()
            try:
                pygame.mixer.quit()
                pygame.mixer.init(frequency=AUDIO_RATE, size=-16, channels=1,
                                  buffer=1024, allowedchanges=0)
                self.channel = pygame.mixer.Channel(0)
                self.channel.set_volume(self.volume)
                self.error = ''
            except pygame.error as exc:
                self.channel = None
                self.error = str(exc)

    def stop(self):
        with self.lock:
            self.pending.clear()
            if self.channel is not None:
                self.channel.stop()

    def set_volume(self, value):
        with self.lock:
            self.volume = max(0., min(1., value))
            if self.channel is not None:
                self.channel.set_volume(self.volume)
            if pygame.mixer.get_init():
                pygame.mixer.music.set_volume(self.volume)

    def feed(self, pcm):
        with self.lock:
            if self.channel is None:
                return
            self.pending.extend(pcm)
            size = 8192  # 128 ms, signed 16-bit mono at 32 kHz
            if len(self.pending) < size:
                return
            chunk = bytes(self.pending[:size])
            del self.pending[:size]
            try:
                sound = pygame.mixer.Sound(buffer=chunk)
                if not self.channel.get_busy():
                    self.channel.play(sound)
                elif self.channel.get_queue() is None:
                    self.channel.queue(sound)
            except pygame.error as exc:
                self.error = str(exc)
                self.channel = None


class RadioEngine:
    """Single worker owns rtl_sdr or rtl_power; mode changes close old process."""
    RETUNE_DELAY = 0.22
    threshold_db = 8.0

    def __init__(self, center_hz, gain, preset_id, mode='live'):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        # Sixth field: audio channel inside the 1.024 MHz receiver bandwidth.
        self.target = (mode, int(center_hz), float(gain), int(preset_id), False,
                       int(center_hz))
        self.record_armed = False
        self.monitor = False
        self.record_threshold = 8.
        self.audio_output = None
        self.record_count = 0
        self.rec_status = {'active': False, 'file': '', 'last_file': '',
                           'elapsed': 0., 'error': '', 'snr_db': 0.0}
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

    def set_recording(self, armed):
        with self.lock:
            self.record_armed = bool(armed)

    def recording_snapshot(self):
        with self.lock:
            return {'armed': self.record_armed,
                    'count': self.record_count,
                    'listen_hz': self.target[5],
                    'modulation': demodulation_for_frequency(self.target[5]),
                    **self.rec_status}

    def _recording_state(self, recorder=None, snr_db=0.):
        with self.lock:
            self.rec_status.update({
                'active': recorder.active if recorder else False,
                'file': recorder.file_name if recorder else '',
                'last_file': recorder.last_file if recorder else self.rec_status['last_file'],
                'elapsed': recorder.elapsed if recorder else 0.,
                'error': recorder.error if recorder else self.rec_status['error'],
                'snr_db': snr_db,
            })

    def change(self, mode=None, center=None, gain=None, preset=None,
               paused=None, listen_hz=None):
        with self.lock:
            prev = self.target
            new_center = int(center) if center is not None else prev[1]
            new_listen = (int(listen_hz) if listen_hz is not None else
                          new_center if center is not None else prev[5])
            new = (mode if mode is not None else prev[0],
                   new_center,
                   float(gain) if gain is not None else prev[2],
                   int(preset) if preset is not None else prev[3],
                   bool(paused) if paused is not None else prev[4],
                   new_listen)
            # Audio-channel change also resets decoder and completes old WAV.
            changed = (new[0] != prev[0] or new[2:5] != prev[2:5] or
                       (new[0] == 'live' and
                        (new[1] != prev[1] or new[5] != prev[5])))
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
        _mode, center_hz, _gain, _preset, _paused, listen_hz = key
        offset_hz = listen_hz - center_hz
        if abs(offset_hz) > SR / 2 - 16_000:
            return 'Audio channel is outside receiver sample bandwidth'
        decoder = None
        recorder = None
        counted = 0
        snr = 0.
        try:
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
                            with self.lock:
                                armed = self.record_armed
                                monitoring = self.monitor
                                squelch = self.record_threshold
                            if armed or monitoring:
                                if decoder is None:
                                    decoder = ChannelAudio(center_hz, listen_hz)
                                    recorder = ActivityRecorder(listen_hz, self.record_threshold, _gain)
                                    counted = 0
                                snr = channel_snr_db(spec, offset_hz)
                                pcm = decoder.demodulate(block)
                                recorder.threshold_db = squelch
                                if armed:
                                    recorder.feed(pcm, snr)
                                else:
                                    recorder.close()
                                if monitoring and self.audio_output is not None:
                                    self.audio_output.feed(pcm if snr >= squelch else bytes(len(pcm)))
                                if recorder.files_created > counted:
                                    with self.lock:
                                        self.record_count += recorder.files_created - counted
                                    counted = recorder.files_created
                                self._recording_state(recorder, snr)
                            elif decoder is not None:
                                recorder.close()
                                self._recording_state(recorder)
                                decoder = recorder = None
                                counted = 0
                if time.monotonic() - last > 6:
                    return 'No RTL-SDR I/Q samples for 6s'
            return None
        finally:
            if recorder is not None:
                recorder.close()
            self._recording_state(recorder)
            if self.audio_output is not None:
                self.audio_output.stop()


    def _wide(self, proc, key):
        _mode, _, gain_db, preset_id, _paused, _listen = key
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
            mode, center, gain, preset_id, paused, _listen = key
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
        self.target = (mode, int(center_hz), gain, preset_id, False, int(center_hz))
        self.record_armed = False
        self.monitor = False
        self.record_threshold = 8.
        self.audio_output = None
        self.version = 0
        self.last_sweep = None
        self.history = deque(maxlen=1000)
        self.last = 0
        self.data = None

    def start(self):
        pass

    def set_recording(self, armed):
        self.record_armed = bool(armed)

    def recording_snapshot(self):
        return {'armed': self.record_armed, 'count': 0,
                'active': False, 'file': '', 'last_file': '',
                'elapsed': 0., 'error': '', 'snr_db': 0.,
                'listen_hz': self.target[5],
                'modulation': demodulation_for_frequency(self.target[5])}

    def change(self, mode=None, center=None, gain=None, preset=None,
               paused=None, listen_hz=None):
        old = self.target
        next_center = int(center) if center is not None else old[1]
        self.target = (mode if mode is not None else old[0],
                       next_center,
                       gain if gain is not None else old[2],
                       preset if preset is not None else old[3],
                       paused if paused is not None else old[4],
                       int(listen_hz) if listen_hz is not None else
                       next_center if center is not None else old[5])
        self.version += 1
        self.data = None

    def snapshot(self):
        mode, center, _gain, preset_id, paused, _listen = self.target
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


def tapped_frequency(x, mode, center_hz, preset_id):
    """Map a displayed chart/waterfall X coordinate to RF frequency.

    The entire 640-pixel spectrum and waterfall represent the same span.
    LIVE shows SR Hz around the tuner center; WIDE shows the survey preset.
    HITS is handled by its own list-row touch handler.
    """
    x = max(0, min(W - 1, int(x)))
    if mode == 'wide':
        preset = PRESETS[preset_id]
        low_hz = preset[1] * 1_000_000
        high_hz = preset[2] * 1_000_000
    elif mode == 'live':
        low_hz = center_hz - SR / 2
        high_hz = center_hz + SR / 2
    else:
        raise ValueError(f'Not a chart mode: {mode}')
    # Pixel x=320 is the center frequency, as on the displayed axis.
    selected_hz = low_hz + (x / W) * (high_hz - low_hz)
    # 1-kHz frequency resolution is adequate for touch selection.
    return max(1_100_000, min(1_765_000_000,
                              int(round(selected_hz / 1000)) * 1000))


def snap_to_signal(frequency_hz, frequencies, levels, radius_hz=12_500,
                   advantage_db=6., prominence_db=8.):
    """Correct a near miss only when a resolved local peak is clearly stronger.

    Inputs describe the currently displayed spectrum, in increasing frequency
    order. Never extrapolate across missing data, the DC notch, or coarse bins.
    Return the original tap when there is insufficient evidence.
    """
    if frequencies is None or levels is None or len(frequencies) != len(levels):
        return frequency_hz
    points = [(float(f), float(p)) for f, p in zip(frequencies, levels)
              if math.isfinite(f) and math.isfinite(p)
              and abs(f - frequency_hz) <= 4 * radius_hz]
    if len(points) < 5:
        return frequency_hz
    tapped = min(range(len(points)), key=lambda i: abs(points[i][0] - frequency_hz))
    if tapped == 0 or tapped == len(points) - 1:
        return frequency_hz
    # Require samples on both sides of the tap at useful resolution.
    if any(points[j + 1][0] - points[j][0] > radius_hz / 2
           for j in (tapped - 1, tapped)):
        return frequency_hz
    background = sorted(p for _, p in points)[len(points) // 2]
    candidates = []
    for i in range(1, len(points) - 1):
        f, power = points[i]
        if abs(f - frequency_hz) > radius_hz:
            continue
        if power < points[tapped][1] + advantage_db or power < background + prominence_db:
            continue
        if not (power >= points[i - 1][1] and power >= points[i + 1][1]
                and (power > points[i - 1][1] or power > points[i + 1][1])):
            continue
        lo, hi = min(i, tapped), max(i, tapped)
        if any(points[j + 1][0] - points[j][0] > radius_hz / 2
               for j in range(max(0, lo - 1), min(len(points) - 1, hi + 1))):
            continue
        candidates.append((power, -abs(f - frequency_hz), f))
    if not candidates:
        return frequency_hz
    # Keep the measured peak's resolution (250 Hz in LIVE).
    return int(round(max(candidates)[2]))


def nearest_signal_peak(frequency_hz, frequencies, levels, threshold_db=8., radius_hz=12_500):
    """Nearest resolved local peak above noise; None means leave tuning alone.

    Unlike tap correction this does not require a stronger signal than the
    current frequency. Invalid bins and the receiver DC gap are not bridged.
    """
    if frequencies is None or levels is None or len(frequencies) != len(levels):
        return None
    points = [(float(f), float(p)) for f, p in zip(frequencies, levels)
              if math.isfinite(f) and math.isfinite(p)
              and abs(f-frequency_hz) <= 4*radius_hz]
    if len(points) < 5:
        return None
    near = min(range(len(points)), key=lambda i: abs(points[i][0]-frequency_hz))
    if abs(points[near][0]-frequency_hz) > radius_hz/2:
        return None
    baseline = sorted(p for _, p in points)[len(points)//2]
    candidates = []
    for i in range(1, len(points)-1):
        f, power = points[i]
        if not 1_100_000 <= f <= 1_765_000_000 or abs(f-frequency_hz) > radius_hz:
            continue
        if power < baseline + max(8., threshold_db):
            continue
        if not (power >= points[i-1][1] and power >= points[i+1][1]
                and (power > points[i-1][1] or power > points[i+1][1])):
            continue
        lo, hi = min(i, near), max(i, near)
        if any(not 0 < points[j+1][0]-points[j][0] <= radius_hz/2
               for j in range(max(0, lo-1), min(len(points)-1, hi+1))):
            continue
        candidates.append((abs(f-frequency_hz), -power, f))
    return int(round(min(candidates)[2])) if candidates else None


def discover_gpiochip(root=Path('/sys/bus/gpio/devices')):
    """Find the header controller by label, not a kernel-dependent chip number."""
    for directory in sorted(root.glob('gpiochip*')):
        try:
            label_text = (directory / 'label').read_text().strip()
            if label_text in ('pinctrl-rp1', 'pinctrl-bcm2835', 'pinctrl-bcm2711'):
                return int(directory.name.removeprefix('gpiochip'))
        except (OSError, ValueError):
            continue
    raise OSError('Pi header GPIO chip not found; check --gpiochip N')


class EncoderInput:
    """GPIO callbacks only enqueue commands; all tuning stays on the UI thread."""
    def __init__(self, reverse=False, clock=time.monotonic):
        self.reverse, self.clock = reverse, clock
        self.lock = threading.Lock()
        self.events = deque(maxlen=128)
        self.pressed_at, self.held = None, False
        self.closed = True
        self.factory = self.rotor = self.button = None
        self.error, self.status = '', 'Disabled'

    def _enqueue(self, action, value=0):
        self.events.append((self.clock(), action, value))

    def turn(self, direction):
        with self.lock:
            if not self.closed:
                self._enqueue('turn', -direction if self.reverse else direction)

    def press(self):
        with self.lock:
            if not self.closed:
                self.pressed_at, self.held = self.clock(), False

    def hold(self):
        with self.lock:
            if not self.closed and self.pressed_at is not None and not self.held:
                self.held = True
                self._enqueue('step')

    def release(self):
        with self.lock:
            if not self.closed and self.pressed_at is not None:
                if not self.held:
                    self._enqueue('step' if self.clock()-self.pressed_at >= ENCODER_HOLD_SECONDS else 'snap')
                self.pressed_at, self.held = None, False

    def drain(self):
        with self.lock:
            now = self.clock()
            events = [(action, value) for stamp, action, value in self.events if now-stamp <= 1.]
            self.events.clear()
            return events

    def start(self, chip=None):
        self.close()
        try:
            from gpiozero import RotaryEncoder, Button
            from gpiozero.pins.lgpio import LGPIOFactory
            chip = discover_gpiochip() if chip is None else chip
            self.factory = LGPIOFactory(chip=chip)
            if self.factory.chip != chip:
                raise RuntimeError('GPIO Zero selected the wrong chip; update python3-gpiozero')
            # RotaryEncoder's quadrature state machine rejects contact bounce;
            # edge time debouncing on A/B can discard legitimate fast transitions.
            self.rotor = RotaryEncoder(17, 27, max_steps=0, pin_factory=self.factory)
            self.button = Button(22, pull_up=True, bounce_time=.03,
                                 hold_time=ENCODER_HOLD_SECONDS, hold_repeat=False,
                                 pin_factory=self.factory)
            with self.lock:
                self.closed = False
            self.rotor.when_rotated_clockwise = lambda: self.turn(1)
            self.rotor.when_rotated_counter_clockwise = lambda: self.turn(-1)
            self.button.when_pressed = self.press
            self.button.when_held = self.hold
            self.button.when_released = self.release
            self.error, self.status = '', f'GPIO ready (chip {chip})'
        except Exception as exc:
            # Optional hardware must never prevent the scanner from opening.
            self.close()
            self.error = str(exc)
            self.status = 'Unavailable - touchscreen still works'
            print(f'Encoder unavailable: {exc}', file=sys.stderr)

    def close(self):
        with self.lock:
            self.closed = True
            self.events.clear()
            self.pressed_at, self.held = None, False
        for name in ('button', 'rotor', 'factory'):
            device = getattr(self, name)
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
                setattr(self, name, None)
        self.status = 'Disabled'


def center_away_from_dc(frequency_hz):
    """Keep the listening channel 100 kHz off the RTL tuner DC notch."""
    offset = 100_000 if frequency_hz <= 1_764_900_000 else -100_000
    return max(1_100_000, min(1_765_000_000, int(frequency_hz + offset)))


class RotatingDisplay:
    """Render in logical coordinates and invert the same transform for input."""
    def __init__(self, windowed=False, rotation=None):
        self.rotation = rotation if rotation is not None else load_settings().get('rotation', 0)
        self.rotation = 180 if self.rotation == 180 else 0
        self.output = pygame.display.set_mode((W, H), 0 if windowed else pygame.FULLSCREEN)
        self.surface = pygame.Surface((W, H)).convert()
        self.error = ''

    def point(self, x, y):
        x, y = max(0, min(W-1, int(x))), max(0, min(H-1, int(y)))
        return (W-1-x, H-1-y) if self.rotation == 180 else (x, y)

    def toggle(self):
        self.rotation = 0 if self.rotation == 180 else 180
        try:
            settings = load_settings()
            settings['rotation'] = self.rotation
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = SETTINGS_FILE.with_suffix('.tmp')
            temp.write_text(json.dumps(settings), encoding='utf-8')
            temp.replace(SETTINGS_FILE)
            self.error = ''
        except OSError as exc:
            self.error = f'Rotation changed but could not save: {exc}'

    def present(self):
        self.output.blit(pygame.transform.rotate(self.surface, 180)
                         if self.rotation == 180 else self.surface, (0, 0))
        pygame.display.flip()


def recording_info(path):
    """Read finalized RF metadata; old WAVs retain unknown RF strength."""
    item = {'path': path, 'name': path.name, 'frequency': None, 'modulation': '?',
            'date': '', 'timestamp': 0., 'duration': None, 'peak': None,
            'mean': None, 'error': '', 'metadata_note': 'No RF metadata (older or unfinished recording)'}
    match = re.fullmatch(r'RF_(\d{8})_(\d{6})_([\d.]+)MHz_\d+_(AM|NFM)\.wav', path.name, re.I)
    if match:
        try:
            date = datetime.strptime(match[1] + match[2], '%Y%m%d%H%M%S')
            item.update(date=date.strftime('%Y-%m-%d %H:%M:%S'), timestamp=date.timestamp(),
                        frequency=float(match[3]), modulation=match[4].upper())
        except (ValueError, OverflowError, OSError):
            pass
    try:
        with wave.open(str(path), 'rb') as wav:
            item['duration'] = wav.getnframes() / wav.getframerate()
            if not wav.getnframes():
                item['error'] = 'Empty or still recording'
    except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
        item['error'] = f'Cannot play WAV: {exc}'
    try:
        meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        if not isinstance(meta, dict) or meta.get('schema_version') != 1:
            raise ValueError('Unrecognized metadata format')
        def number(key):
            value = meta.get(key)
            return float(value) if type(value) in (int, float) and math.isfinite(value) else None
        hz = number('frequency_hz')
        if hz is not None and 1_100_000 <= hz <= 1_765_000_000:
            item['frequency'] = hz / 1e6
        if meta.get('modulation') in ('AM', 'NFM'):
            item['modulation'] = meta['modulation']
        if meta.get('complete') is True and meta.get('signal_units') == 'dB above local noise':
            item.update(peak=number('peak_signal_db'), mean=number('mean_signal_db'),
                        metadata_note='RF dB above local noise; not calibrated dBm')
        else:
            item['metadata_note'] = 'Incomplete recording or unsupported signal units'
        try:
            date = datetime.fromisoformat(meta['recorded_at'])
            item.update(date=date.strftime('%Y-%m-%d %H:%M:%S'), timestamp=date.timestamp())
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        item['metadata_note'] = f'RF metadata unavailable: {exc}'
    return item


def filter_recordings(items, sort_key='peak', descending=True, modulation='ALL',
                      min_db=None, frequency_text=''):
    """Numeric ordering with unknown measurements always last in either direction."""
    visible = [item for item in items
               if (modulation == 'ALL' or item['modulation'] == modulation)
               and (min_db is None or (item['peak'] is not None and item['peak'] >= min_db))
               and (not frequency_text or (item['frequency'] is not None
                    and f"{item['frequency']:.6f}".startswith(frequency_text)))]
    known = [item for item in visible if item[sort_key] is not None]
    unknown = [item for item in visible if item[sort_key] is None]
    known.sort(key=lambda item: (item[sort_key], item['name']), reverse=descending)
    unknown.sort(key=lambda item: item['name'], reverse=True)
    return known + unknown


class RecordingBrowser:
    """Touch-first library, shared by the scanner and standalone browser mode."""
    SORTS = (('peak', 'PEAK dB'), ('mean', 'AVG dB'), ('timestamp', 'DATE'),
             ('frequency', 'FREQ'), ('duration', 'LENGTH'))

    def __init__(self, screen, audio, directory=RECORDINGS_DIR, before_play=None, active_file=None, display=None):
        self.display = display
        self.screen, self.audio, self.directory = screen, audio, Path(directory)
        self.before_play = before_play or (lambda: None)
        self.active_file = active_file or (lambda: '')
        self.small = pygame.font.SysFont('DejaVu Sans', 14)
        self.tiny = pygame.font.SysFont('DejaVu Sans', 12)
        self.medium = pygame.font.SysFont('DejaVu Sans', 18, bold=True)
        self.items, self.visible, self.buttons = [], [], []
        self.sort_index, self.page = 0, 0
        self.descending, self.modulation, self.min_db = True, 'ALL', None
        self.frequency_text, self.entry = '', None
        self.selected = None
        self.notice, self.playing, self.paused = '', None, False
        self.results = queue.Queue()
        self.loading = False
        self.refresh()

    def refresh(self):
        if self.loading:
            return
        self.loading, self.notice = True, 'Reading recordings...'
        def scan():
            try:
                entries = [recording_info(path) for path in self.directory.glob('*.wav') if path.is_file()]
                self.results.put((entries, ''))
            except OSError as exc:
                self.results.put(([], str(exc)))
        threading.Thread(target=scan, daemon=True).start()

    def apply_filter(self):
        self.visible = filter_recordings(self.items, self.SORTS[self.sort_index][0],
                                         self.descending, self.modulation, self.min_db,
                                         self.frequency_text)
        self.page = min(self.page, max(0, (len(self.visible) - 1) // 4))

    def stop(self):
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        self.playing, self.paused = None, False

    def tap(self, x, y):
        for rect, action in self.buttons:
            if rect.collidepoint(x, y):
                return self.action(action)
        return False

    def action(self, action):
        self.notice = ''
        if action == 'rotate' and self.display:
            self.display.toggle()
            self.notice = self.display.error
        elif action == 'back':
            if self.entry is not None:
                self.entry = None
            elif self.selected is not None:
                self.selected = None
            else:
                self.stop()
                return True
        elif action == 'sort':
            self.sort_index = (self.sort_index + 1) % len(self.SORTS)
            self.page = 0
        elif action == 'order':
            self.descending = not self.descending
            self.page = 0
        elif action == 'mod':
            modes = ('ALL', 'AM', 'NFM')
            self.modulation = modes[(modes.index(self.modulation) + 1) % len(modes)]
            self.page = 0
        elif action == 'min':
            values = (None, 0, 6, 10, 15, 20, 30)
            self.min_db = values[(values.index(self.min_db) + 1) % len(values)]
            self.page = 0
        elif action == 'freq':
            self.entry = self.frequency_text
        elif action == 'clear':
            self.entry = ''
        elif action == 'delete':
            self.entry = self.entry[:-1]
        elif action == 'enter':
            self.frequency_text, self.entry, self.page = self.entry, None, 0
        elif self.entry is not None and action in '0123456789.':
            if len(self.entry) < 11 and (action != '.' or '.' not in self.entry):
                self.entry += action
        elif action == 'refresh':
            self.refresh()
        elif action in ('prev', 'next'):
            self.page = max(0, min(max(0, (len(self.visible)-1)//4), self.page + (-1 if action == 'prev' else 1)))
        elif action.startswith('select:'):
            self.selected = self.visible[int(action.split(':')[1])]
        elif action == 'play' and self.selected:
            # Recheck the file immediately before playback, since scanning is asynchronous.
            self.selected = recording_info(self.selected['path'])
            if str(self.selected['path']) == self.active_file():
                self.notice = 'This file is still recording. Choose a completed recording.'
            elif self.selected['error']:
                self.notice = self.selected['error']
            else:
                self.before_play()
                self.audio.stop()
                try:
                    if not pygame.mixer.get_init():
                        self.audio.reset()
                    pygame.mixer.music.load(str(self.selected['path']))
                    pygame.mixer.music.set_volume(self.audio.volume)
                    pygame.mixer.music.play()
                    self.playing, self.paused = self.selected['path'], False
                except (pygame.error, OSError) as exc:
                    self.notice = f'Playback error: {exc}'
                    self.playing = None
        elif action == 'pause' and self.playing and pygame.mixer.get_init():
            self.paused = not self.paused
            pygame.mixer.music.pause() if self.paused else pygame.mixer.music.unpause()
        elif action == 'stop':
            self.stop()
        elif action in ('vol-', 'vol+'):
            self.audio.set_volume(self.audio.volume + (-.1 if action == 'vol-' else .1))
        elif action == 'reset':
            self.stop()
            self.audio.reset()
            self.notice = self.audio.error or 'Audio reopened using the system output'
        self.apply_filter()
        return False

    def draw(self):
        try:
            self.items, self.notice = self.results.get_nowait()
            self.loading = False
            self.apply_filter()
        except queue.Empty:
            pass
        if self.playing and not self.paused and pygame.mixer.get_init() and not pygame.mixer.music.get_busy():
            self.playing = None
        self.screen.fill(BGC)
        self.buttons = []
        def text(value, y, color=INK, font=None, x=10):
            label(self.screen, font or self.small, str(value), color, x, y)
        def button(x, y, width, title, action, height=44):
            rect = pygame.Rect(x, y, width, height)
            self.buttons.append((rect, action))
            pygame.draw.rect(self.screen, (38, 60, 80), rect, border_radius=6)
            label(self.screen, self.small, title, INK, rect.centerx, y+14, 'center')
        def db(value):
            return 'Unknown' if value is None else f'{value:.1f} dB'
        text('RF HUNTER / RECORDINGS', 10, CYAN, self.medium)
        if self.display:
            button(482, 2, 150, 'ROTATE 180', 'rotate', 36)
        if self.entry is not None:
            text('Frequency starts with: ' + (self.entry or 'ALL') + ' MHz', 55)
            keys = ('1', '2', '3', 'delete', '4', '5', '6', 'clear',
                    '7', '8', '9', 'enter', '.', '0')
            for i, key in enumerate(keys):
                button(8+i%4*158, 90+i//4*68, 150, key.upper(), key, 58)
            text('Example: 125 finds 125.xxx MHz. CLEAR + ENTER shows all.', 375, MUTED, self.tiny)
        elif self.selected:
            item = self.selected
            frequency = 'Unknown frequency' if item['frequency'] is None else f"{item['frequency']:.6f} MHz"
            text(f"{frequency}  {item['modulation']}", 52, CYAN, self.medium)
            text(item['date'] or 'Unknown date', 86)
            duration = '?' if item['duration'] is None else f"{item['duration']:.1f}"
            text(f"Duration: {duration}s    Peak: {db(item['peak'])}    Average: {db(item['mean'])}", 118)
            text(item['metadata_note'][:85], 150, MUTED, self.tiny)
            text('Average includes pre-roll and the quiet end of the recording.', 170, MUTED, self.tiny)
            text(item['name'][:85], 199, INK, self.tiny)
            text(item['error'][:85], 220, RED, self.tiny)
            button(8, 254, 150, 'PLAY', 'play', 54)
            button(166, 254, 150, 'RESUME' if self.paused else 'PAUSE', 'pause', 54)
            button(324, 254, 150, 'STOP', 'stop', 54)
            button(482, 254, 150, 'AUDIO RESET', 'reset', 54)
            button(8, 324, 150, 'VOLUME -', 'vol-')
            button(166, 324, 150, 'VOLUME +', 'vol+')
            text(f'Volume: {self.audio.volume:.0%}', 338, x=335)
            if self.playing:
                position = max(0, pygame.mixer.music.get_pos()) / 1000
                text(('Paused' if self.paused else 'Playing') + f'  {position:.1f}s  ' + self.playing.stem[:58], 389, GREEN, self.tiny)
        else:
            button(8, 42, 182, 'SORT: '+self.SORTS[self.sort_index][1], 'sort')
            button(198, 42, 140, 'HIGH / NEW' if self.descending else 'LOW / OLD', 'order')
            button(346, 42, 90, self.modulation, 'mod')
            button(444, 42, 188, 'MIN PEAK: '+('OFF' if self.min_db is None else str(self.min_db)+' dB'), 'min')
            button(8, 94, 220, 'FREQ: '+(self.frequency_text or 'ALL'), 'freq')
            button(236, 94, 110, 'REFRESH', 'refresh')
            text(f'{len(self.visible)} of {len(self.items)} files', 108, x=360)
            for row, item in enumerate(self.visible[self.page*4:self.page*4+4]):
                y = 148 + row*58
                rect = pygame.Rect(8, y, 624, 52)
                self.buttons.append((rect, f'select:{self.page*4+row}'))
                pygame.draw.rect(self.screen, (23, 42, 60), rect, border_radius=5)
                freq = '?' if item['frequency'] is None else f"{item['frequency']:.6f}"
                text(f"{freq} MHz  {item['modulation']}", y+5, CYAN, x=18)
                text(f"Peak {db(item['peak'])}   Avg {db(item['mean'])}", y+5, GREEN, x=325)
                duration = '?' if item['duration'] is None else f"{item['duration']:.1f}"
                text((item['date'] or item['name'][:40]) + f'   {duration}s' + ('   unavailable' if item['error'] else ''), y+30, MUTED, self.tiny, 18)
            if not self.visible:
                text('Reading recordings...' if self.loading else 'No matching recordings. Try clearing the filters.', 188, MUTED)
            button(8, 386, 130, 'PREVIOUS', 'prev')
            button(146, 386, 130, 'NEXT', 'next')
            text(f'Page {self.page+1}/{max(1, (len(self.visible)+3)//4)}', 400, x=296)
        text(self.notice[:65], 437, ORANGE, self.tiny)
        button(482, 430, 150, 'BACK', 'back')


def run_recording_browser(args):
    """Standalone library: no SDR worker or radio subprocess is started."""
    pygame.mixer.pre_init(AUDIO_RATE, -16, 1, 1024)
    pygame.init()
    display = RotatingDisplay(args.windowed, args.rotation)
    screen = display.surface
    pygame.display.set_caption('RF Hunter Recordings v0.6.0')
    audio = AudioOutput()
    audio.reset()
    browser = RecordingBrowser(screen, audio, args.recordings_dir, display=display)
    clock = pygame.time.Clock()
    last_tap, start = -1000, time.monotonic()
    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = not browser.action('back')
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    now = pygame.time.get_ticks()
                    if now-last_tap >= 90:
                        running = not browser.tap(*display.point(*event.pos))
                        last_tap = now
                elif event.type == getattr(pygame, 'FINGERDOWN', -1):
                    now = pygame.time.get_ticks()
                    if now-last_tap >= 90:
                        running = not browser.tap(*display.point(event.x*W, event.y*H))
                        last_tap = now
            browser.draw()
            display.present()
            if args.screenshot and time.monotonic()-start > 2.5:
                pygame.image.save(display.output, args.screenshot)
                running = False
            clock.tick(20)
    finally:
        browser.stop()
        audio.stop()
        pygame.quit()


def main():
    parser = argparse.ArgumentParser(description='RF Hunter v0.6.0 640x480 SDR + AM/NFM WAV recorder')
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
    parser.add_argument('--rotation', type=int, choices=(0, 180), default=None,
                        help='Override saved display/touch rotation for this launch')
    parser.add_argument('--encoder', action='store_true', default=None,
                        help='Enable the GPIO encoder, including in demo mode')
    parser.add_argument('--no-encoder', dest='encoder', action='store_false',
                        help='Leave GPIO pins unused and use touchscreen controls')
    parser.add_argument('--encoder-reverse', action='store_true', default=None,
                        help='Reverse the dial direction (also available in MENU > ENCODER)')
    parser.add_argument('--gpiochip', type=int, default=None,
                        help='Override automatic Pi header chip detection, e.g. 0')
    parser.add_argument('--encoder-test', action='store_true',
                        help='Print dial actions in the terminal without starting the screen or SDR')
    parser.add_argument('--recordings-only', action='store_true', help='Open the recording browser without starting the SDR')
    parser.add_argument('--recordings-dir', type=Path, default=RECORDINGS_DIR,
                        help='Folder for standalone recording browsing')
    parser.add_argument('--demo', action='store_true', help='Simulated RF, no SDR hardware')
    parser.add_argument('--rec-on', action='store_true', help='Arm automatic LIVE audio recording on launch')
    parser.add_argument('--touch-debug', action='store_true', help='Print touch coordinates and tuning decisions')
    parser.add_argument('--screenshot', type=str, default=None,
                        help='Save screenshot after 2.5s, then quit')
    args = parser.parse_args()
    if args.encoder_test:
        encoder = EncoderInput(reverse=(args.encoder_reverse if args.encoder_reverse is not None
                                        else load_settings().get('encoder_reverse', False) is True))
        encoder.start(args.gpiochip)
        if encoder.closed:
            raise SystemExit(1)
        print(encoder.status + ': turn, click, or hold the dial. Ctrl+C to exit.', flush=True)
        try:
            while True:
                for action, value in encoder.drain():
                    print(('TUNE UP' if value > 0 else 'TUNE DOWN') if action == 'turn'
                          else 'CLICK / SNAP' if action == 'snap' else 'HOLD / STEP', flush=True)
                time.sleep(.05)
        except KeyboardInterrupt:
            pass
        finally:
            encoder.close()
        return
    if args.recordings_only:
        run_recording_browser(args)
        return

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

    pygame.mixer.pre_init(AUDIO_RATE, -16, 1, 1024)
    pygame.init()
    display = RotatingDisplay(args.windowed, args.rotation)
    screen = display.surface
    pygame.display.set_caption('RF Hunter v0.6.0 - 640x480 + recording')
    pygame.mouse.set_visible(True)
    clock = pygame.time.Clock()
    big = pygame.font.SysFont('DejaVu Sans', 21, bold=True)
    medium = pygame.font.SysFont('DejaVu Sans', 17)
    small = pygame.font.SysFont('DejaVu Sans', 14)
    tiny = pygame.font.SysFont('DejaVu Sans', 12)

    engine = (DemoEngine(center, gain_db, preset_id, mode) if args.demo else
              RadioEngine(center, gain_db, preset_id, mode))
    audio_output = AudioOutput()
    audio_output.reset()
    engine.audio_output = audio_output
    engine.record_threshold = 8.
    engine.threshold_db = threshold
    engine.set_recording(args.rec_on and not args.demo)
    engine.start()
    encoder = EncoderInput(reverse=(args.encoder_reverse if args.encoder_reverse is not None
                                    else settings.get('encoder_reverse', False) is True))
    encoder_enabled = (args.encoder if args.encoder is not None else
                       not args.demo and settings.get('encoder_enabled', True) is not False)
    if encoder_enabled:
        encoder.start(args.gpiochip)
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
    last_live_frame_at = 0.
    dial_note, dial_note_until = '', 0.
    snap_requested_until = 0.
    target_marker = None
    panel = None
    recording_browser = None
    entry = ''
    notice = ''
    step_hz = settings.get('tune_step_hz', 1000)
    if step_hz not in TUNE_STEPS:
        step_hz = 1000
    panel_buttons = []
    buttons = [pygame.Rect(4 + i * 128, BUTTON_VIS_Y, 120, BUTTON_VIS_H)
               for i in range(5)]
    hitboxes = [pygame.Rect(rect.x, BUTTON_TOUCH_Y, rect.width, BUTTON_TOUCH_H)
                for rect in buttons]
    # Dedicated AGC button, restored for the capacitive touchscreen.
    # Keep these four top controls separated for 640px touch targeting.
    auto_btn = pygame.Rect(339, 4, 65, 49)
    rec_btn = pygame.Rect(412, 4, 65, 49)
    ad_btn = pygame.Rect(485, 4, 65, 49)
    mode_btn = pygame.Rect(558, 4, 78, 49)

    def clear_spectrum():
        nonlocal smooth, spectrum, last_version, strength, active_until, last_live_frame_at, snap_requested_until
        last_live_frame_at = 0.
        snap_requested_until = 0.
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

    def select_auto_gain():
        """Enable the RTL-SDR's automatic gain in LIVE and WIDE modes."""
        nonlocal gain_db
        if gain_db != AUTO_GAIN:
            gain_db = AUTO_GAIN
            engine.change(gain=gain_db)
            clear_spectrum()

    def change_vis(delta):
        nonlocal vis
        vis = max(VIS_MIN, min(VIS_MAX, vis + delta))

    def tune_target(frequency):
        """Select the clicked RF channel and switch to LIVE in one update.

        Keep the signal away from the receiver DC notch, but leave the audio
        listen_hz at the frequency actually touched. One engine.change call
        avoids half-completed retunes and preserves the recording channel.
        """
        nonlocal center, target_marker, mode, view, paused, adjust
        frequency = max(1_100_000, min(1_765_000_000, int(frequency)))
        center = center_away_from_dc(frequency)
        target_marker = frequency
        mode = view = 'live'
        paused = False
        adjust = False
        engine.change(mode='live', center=center, listen_hz=frequency, paused=False)
        clear_spectrum()
        if args.touch_debug:
            print(f'RF Hunter tuned to {mhz(frequency)} MHz '
                  f'(receiver center {mhz(center)} MHz)', flush=True)

    def tune_from_x(x):
        if view not in ('live', 'wide'):
            return
        frequency = tapped_frequency(x, view, center, preset_id)
        original = frequency
        if view == 'live' and smooth is not None:
            frequency = snap_to_signal(frequency, (center + FFT_FREQS)[VALID], smooth[VALID])
        elif view == 'wide' and spectrum is not None:
            low, high = PRESETS[preset_id][1:3]
            pixel_hz = (high - low) * 1e6 / W
            # rtl_power bin size and display pixels must both resolve the search.
            if max(pixel_hz, PRESETS[preset_id][3] * 1000) <= 6250:
                frequencies = low * 1e6 + (np.arange(W) + .5) * pixel_hz
                frequency = snap_to_signal(frequency, frequencies, spectrum)
        if args.touch_debug and frequency != original:
            print(f'RF Hunter snap: {original / 1e6:.6f} -> '
                  f'{frequency / 1e6:.6f} MHz', flush=True)
        tune_target(frequency)

    def dial_message(message):
        nonlocal dial_note, dial_note_until
        dial_note, dial_note_until = message, time.monotonic()+2.5

    def save_dial_settings():
        nonlocal notice
        try:
            saved = load_settings()
            saved.update(encoder_enabled=encoder_enabled, encoder_reverse=encoder.reverse,
                         tune_step_hz=step_hz)
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = SETTINGS_FILE.with_suffix('.tmp')
            temp.write_text(json.dumps(saved), encoding='utf-8')
            temp.replace(SETTINGS_FILE)
        except OSError as exc:
            notice = f'Dial setting could not be saved: {exc}'

    def cycle_step():
        nonlocal step_hz
        step_hz = TUNE_STEPS[(TUNE_STEPS.index(step_hz)+1) % len(TUNE_STEPS)]
        dial_message(f'Dial step: {step_hz/1000:g} kHz')
        save_dial_settings()

    def process_encoder():
        nonlocal snap_requested_until
        commands = encoder.drain()
        # Do not retune the radio while entering a frequency or browsing audio.
        if panel not in (None, 'controls', 'encoder'):
            return
        target = engine.recording_snapshot()['listen_hz']
        pending = False
        def flush():
            nonlocal pending
            if pending:
                tune_target(target)
                dial_message(f'Tuned {target/1e6:.6f} MHz | step {step_hz/1000:g}k')
                pending = False
        for action, value in commands:
            if action == 'turn':
                snap_requested_until = 0.
                target = max(1_100_000, min(1_765_000_000, target+value*step_hz))
                pending = target != engine.recording_snapshot()['listen_hz']
            elif action == 'step':
                flush()
                cycle_step()
            elif action == 'snap':
                flush()
                if mode != 'live' or paused:
                    dial_message('Snap is available in LIVE mode')
                    continue
                snap_requested_until = time.monotonic()+1.5
                dial_message('Snap: waiting for fresh LIVE data')
        flush()

    def finish_encoder_snap():
        nonlocal snap_requested_until
        if not snap_requested_until:
            return
        if panel not in (None, 'controls', 'encoder') or mode != 'live' or paused:
            snap_requested_until = 0.
            return
        now = time.monotonic()
        if now > snap_requested_until:
            snap_requested_until = 0.
            dial_message('Snap timed out: no fresh LIVE data')
            return
        if smooth is None or now-last_live_frame_at > .5:
            return
        snap_requested_until = 0.
        target = engine.recording_snapshot()['listen_hz']
        frequency = nearest_signal_peak(target, (center+FFT_FREQS)[VALID],
                                        smooth[VALID], engine.record_threshold)
        if frequency is None:
            dial_message('No qualifying peak within +/-12.5 kHz')
        else:
            if frequency != target:
                tune_target(frequency)
            dial_message(f'Snap: {frequency/1e6:.6f} MHz')

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

    def panel_action(action):
        nonlocal panel, entry, notice, step_hz, adjust, recording_browser, encoder_enabled
        if panel == 'files':
            if recording_browser.action(action):
                panel = 'controls'
            return
        notice = ''
        if action == 'back':
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
            panel = None if panel == 'controls' else 'controls'
        elif action == 'keypad':
            panel, entry = 'keypad', ''
        elif action == 'delete':
            entry = entry[:-1]
        elif action == 'enter':
            try:
                hz = round(float(entry) * 1e6)
                if not 1_100_000 <= hz <= 1_765_000_000:
                    raise ValueError()
                tune_target(hz)
                panel = None
            except (ValueError, OverflowError):
                notice = 'Enter a frequency from 1.100 to 1765.000 MHz'
        elif panel == 'keypad' and action in '0123456789.':
            if len(entry) < 12 and (action != '.' or '.' not in entry):
                entry += action
        elif action == 'step':
            cycle_step()
        elif action == 'encoder':
            panel = 'encoder'
        elif action == 'encoder-toggle':
            encoder_enabled = not encoder_enabled
            encoder.start(args.gpiochip) if encoder_enabled else encoder.close()
            save_dial_settings()
        elif action == 'encoder-reverse':
            with encoder.lock:
                encoder.reverse = not encoder.reverse
                encoder.events.clear()
            save_dial_settings()
        elif action == 'encoder-retry':
            encoder_enabled = True
            encoder.start(args.gpiochip)
            save_dial_settings()
        elif action in ('fine-', 'fine+'):
            tune_target(engine.recording_snapshot()['listen_hz'] +
                        (-step_hz if action == 'fine-' else step_hz))
        elif action in ('sql-', 'sql+'):
            engine.record_threshold = max(0., min(40., engine.record_threshold +
                                                  (-1 if action == 'sql-' else 1)))
        elif action == 'monitor':
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
            engine.monitor = not engine.monitor
            audio_output.stop()
            if engine.monitor and audio_output.channel is None:
                audio_output.reset()
        elif action in ('vol-', 'vol+'):
            audio_output.set_volume(audio_output.volume + (-.1 if action == 'vol-' else .1))
        elif action == 'rotate':
            display.toggle()
            notice = display.error
        elif action == 'reconnect':
            audio_output.reset()
        elif action == 'adjust':
            panel, adjust = None, True
        elif action == 'files':
            panel = 'files'
            recording_browser = RecordingBrowser(
                screen, audio_output,
                before_play=lambda: setattr(engine, 'monitor', False),
                active_file=lambda: engine.recording_snapshot()['file'], display=display)

    def draw_panel():
        nonlocal panel_buttons
        if panel == 'files':
            recording_browser.draw()
            return
        panel_buttons = []
        screen.fill(BGC)
        def button(x, y, width, height, title, action):
            rect = pygame.Rect(x, y, width, height)
            panel_buttons.append((rect, title, action))
            pygame.draw.rect(screen, (38, 60, 80), rect, border_radius=6)
            label(screen, small, title, INK, rect.centerx, y + 18, 'center')
        label(screen, medium, 'RF HUNTER v0.6.0  /  ' + panel.upper(), CYAN, 10, 10)
        if panel == 'controls':
            status = engine.recording_snapshot()
            label(screen, small, f"{status['listen_hz']/1e6:.6f} MHz {status['modulation']}"
                  f"   Squelch {engine.record_threshold:.0f} dB   Vol {audio_output.volume:.0%}", INK, 10, 40)
            try:
                free = shutil.disk_usage(RECORDINGS_DIR if RECORDINGS_DIR.exists() else Path.home()).free
                disk = f'{free / 1024**3:.1f} GiB free | WAV split: 5 min | reserve: 100 MiB'
            except OSError:
                disk = 'Storage information unavailable'
            label(screen, tiny, disk, MUTED, 10, 64)
            controls = [('FREQ MHz', 'keypad'), (f'STEP {step_hz/1000:g}k', 'step'),
                        ('TUNE -', 'fine-'), ('TUNE +', 'fine+'),
                        ('SQUELCH -', 'sql-'), ('SQUELCH +', 'sql+'),
                        ('MUTE' if engine.monitor else 'LISTEN', 'monitor'), ('RECORDINGS', 'files'),
                        ('VOLUME -', 'vol-'), ('VOLUME +', 'vol+'),
                        ('AUDIO RESET', 'reconnect'), ('RF / VIS', 'adjust')]
            for i, (title, action) in enumerate(controls):
                button(8 + i % 4 * 158, 95 + i // 4 * 80, 150, 65, title, action)
            label(screen, tiny, 'DEMO: no live audio' if args.demo else
                  'Audio uses OS output. Pair/select Bluetooth in the Pi desktop.', MUTED, 10, 343)
            label(screen, tiny, 'Squelch controls listening + recording, separately from hit detection.', MUTED, 10, 363)
            if audio_output.error:
                label(screen, tiny, audio_output.error[:88], RED, 10, 385)
        elif panel == 'encoder':
            label(screen, medium, encoder.status, GREEN if not encoder.closed else ORANGE, 10, 52)
            label(screen, small, 'CLK GPIO17 (11) | DT GPIO27 (13) | SW GPIO22 (15)', INK, 10, 86)
            label(screen, small, '+ to 3.3V (1) | GND to ground (6)', INK, 10, 110)
            label(screen, small, f'Turn: tune   Click: nearby peak   Hold 0.8s: step {step_hz/1000:g}k', INK, 10, 146)
            label(screen, tiny, 'Click requires fresh LIVE data; searches only +/-12.5 kHz.', MUTED, 10, 175)
            label(screen, tiny, 'Dial is inactive in keypads and the recording browser.', MUTED, 10, 195)
            button(8, 230, 200, 58, 'DISABLE' if encoder_enabled else 'ENABLE', 'encoder-toggle')
            button(216, 230, 200, 58, 'DIR: REVERSE' if encoder.reverse else 'DIR: NORMAL', 'encoder-reverse')
            button(424, 230, 208, 58, 'RETRY GPIO', 'encoder-retry')
            button(8, 308, 200, 58, f'STEP {step_hz/1000:g}k', 'step')
            if encoder.error:
                label(screen, tiny, encoder.error[:90], RED, 10, 380)
        elif panel == 'keypad':
            label(screen, big, (entry or '_') + ' MHz', INK, 10, 45)
            keys = ['1', '2', '3', 'delete', '4', '5', '6', 'enter',
                    '7', '8', '9', 'back', '.', '0']
            for i, key in enumerate(keys):
                button(8 + i % 4 * 158, 90 + i // 4 * 72, 150, 62, key.upper(), key)
        label(screen, tiny, notice[:90], ORANGE, 10, 416)
        if panel == 'controls':
            button(8, 430, 180, 46, 'ROTATE 180', 'rotate')
            button(196, 430, 180, 46, 'ENCODER', 'encoder')
        button(482, 430, 150, 46, 'BACK', 'back')

    running = True

    def handle_tap(x, y):
        """Route mouse and native touchscreen finger taps through one hit map."""
        nonlocal last_input_ms, running, adjust, panel
        now = pygame.time.get_ticks()
        if now - last_input_ms < 90:
            return  # Suppress duplicate mouse+finger event from one touch.
        last_input_ms = now
        x, y = display.point(x, y)
        if args.touch_debug:
            print(f'RF Hunter touch: x={x} y={y} view={view}', flush=True)
        if panel:
            if panel == 'files':
                if recording_browser.tap(x, y):
                    panel = 'controls'
                return
            for rect, title, action in panel_buttons:
                if rect.collidepoint(x, y):
                    panel_action(action)
                    break
            return
        if auto_btn.collidepoint(x, y):
            select_auto_gain()
        elif rec_btn.collidepoint(x, y) and not args.demo:
            engine.set_recording(not engine.recording_snapshot()['armed'])
        elif ad_btn.collidepoint(x, y):
            # ADJ and TUNE are the same top-row toggle control.
            panel = 'controls'
        elif mode_btn.collidepoint(x, y):
            switch('live' if mode == 'wide' else 'wide')
        elif y >= BUTTON_TOUCH_Y:
            for i, hitbox in enumerate(hitboxes):
                if hitbox.collidepoint(x, y):
                    running = button_press(i, history)
                    break
        elif view in ('live', 'wide') and (
                PLOT_TOP <= y < PLOT_BOTTOM or WF_TOP <= y < WF_BOTTOM):
            tune_from_x(x)
        elif view == 'hits' and HITS_START_Y <= y < HITS_START_Y + HITS_VISIBLE * HITS_ROW_H:
            row = (y - HITS_START_Y) // HITS_ROW_H
            entries = list(reversed(history))
            at = hit_scroll + row
            if at < len(entries):
                tune_target(int(entries[at]['hz']))

    try:
        while running:
            frame, version, state, error, stamp, history = engine.snapshot()
            last_sweep = stamp or last_sweep
            record_state = engine.recording_snapshot()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if panel:
                        if event.key == pygame.K_ESCAPE:
                            panel_action('back')
                        elif panel == 'keypad':
                            if event.key == pygame.K_RETURN:
                                panel_action('enter')
                            elif event.key == pygame.K_BACKSPACE:
                                panel_action('delete')
                            elif event.unicode in '0123456789.' and event.unicode:
                                panel_action(event.unicode)
                        continue
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key == pygame.K_TAB:
                        adjust = not adjust
                    elif event.key == pygame.K_r and not args.demo:
                        engine.set_recording(not engine.recording_snapshot()['armed'])
                    elif event.key == pygame.K_w:
                        switch('wide')
                    elif event.key == pygame.K_l:
                        switch('live')
                    elif event.key == pygame.K_a:
                        select_auto_gain()
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
                    handle_tap(*event.pos)
                elif event.type == getattr(pygame, 'FINGERDOWN', -1):
                    # SDL2 sends normalized touch coordinates in [0, 1].
                    handle_tap(int(event.x * W), int(event.y * H))

            if running:
                process_encoder()
            # A touch or dial event may have retuned since the initial snapshot.
            # Never reinterpret an old-center frame at the new frequency.
            frame, version, state, error, stamp, history = engine.snapshot()
            record_state = engine.recording_snapshot()

            if frame is not None and version != last_version:
                last_version = version
                frame_mode, _frame_stamp, data = frame
                if frame_mode == mode:
                    if mode == 'live':
                        last_live_frame_at = time.monotonic()
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

            finish_encoder_snap()
            record_state = engine.recording_snapshot()
            screen.fill(BGC)
            label(screen, medium, 'RF HUNTER v0.6.0', INK, 9, 7)
            active = ((mode == 'wide' and bool(newest_hits)) or
                      (mode == 'live' and time.monotonic() < active_until))
            status = 'ACTIVITY' if active else ('QUIET' if spectrum is not None else state)
            label(screen, tiny, status, GREEN if active else MUTED, 324, 12, 'right')
            rec_color = ((64, 35, 39) if args.demo else
                         (185, 48, 53) if record_state['active'] else
                         (140, 95, 36) if record_state['armed'] else (40, 58, 70))
            auto_color = ((22, 104, 91) if gain_db == AUTO_GAIN else
                          (38, 60, 80))
            for rect, title, color in (
                    (auto_btn, 'AUTO', auto_color),
                    (rec_btn, 'REC' if not record_state['active'] else 'ON AIR', rec_color),
                    (ad_btn, 'MENU', (42, 100, 112)),
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
                  324, 40, 'right')

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
            elif view in ('live', 'wide'):
                label(screen, tiny, 'TAP SPECTRUM / WATERFALL TO TUNE', MUTED, 9, 200)

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

            if mode == 'live':
                rec_label = ('RECORDING' if record_state['active'] else
                             'ARMED' if record_state['armed'] else 'OFF')
                audio_note = (f"LISTEN {mhz(record_state['listen_hz'])} MHz "
                              f"{record_state['modulation']}   REC {rec_label}")
                if record_state['active']:
                    audio_note += f"  {record_state['elapsed']:.1f}s"
                label(screen, tiny, audio_note, ORANGE if record_state['armed'] else MUTED,
                      9, 394)
            elif record_state['armed']:
                label(screen, tiny, 'REC ARMED - audio is recorded only in LIVE', ORANGE, 9, 394)
            if record_state['error']:
                label(screen, tiny, record_state['error'][:90], RED, 9, 200)

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

            if panel:
                draw_panel()
            if time.monotonic() < dial_note_until and panel in (None, 'controls', 'encoder'):
                pygame.draw.rect(screen, BGC, (0, 196 if panel is None else 397, W, 18))
                label(screen, tiny, dial_note, ORANGE, 9, 198 if panel is None else 399)
            display.present()
            if args.screenshot and time.monotonic() - first > 2.5:
                pygame.image.save(display.output, args.screenshot)
                running = False
            clock.tick(15)
    finally:
        encoder.close()
        engine.close()
        audio_output.stop()
        save_settings(gain_db, vis, preset_id)
        pygame.quit()


if __name__ == '__main__':
    main()
