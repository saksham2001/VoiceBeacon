#!/usr/bin/env python3
"""
SDR diagnostic: captures raw IQ, auto-detects signal frequency, demodulates in Python.
Run this, then transmit when prompted.
"""
import subprocess, sys, time, io, wave
import numpy as np
from scipy.signal import decimate

import argparse as _ap
_p = _ap.ArgumentParser()
_p.add_argument("--freq", default="145.500M",
                help="Approximate frequency to search around (default: 145.500M)")
_p.add_argument("--gain", type=float, default=10.0)
_args = _p.parse_args()

def _parse_freq(s: str) -> int:
    s = s.upper()
    if s.endswith("M"): return int(float(s[:-1]) * 1_000_000)
    if s.endswith("K"): return int(float(s[:-1]) * 1_000)
    return int(s)

SEARCH_FREQ = _parse_freq(_args.freq)
SAMP_RATE   = 240_000      # IQ sample rate — ±120 kHz capture window
GAIN        = _args.gain
DURATION    = 15           # seconds to capture during TX
OUT_RATE    = 16_000


def capture_iq(duration_secs: int, freq: int = SEARCH_FREQ) -> bytes:
    cmd = [
        "rtl_sdr", "-d", "0",
        "-f", str(freq),
        "-s", str(SAMP_RATE),
        "-g", str(GAIN),
        "-n", str(SAMP_RATE * duration_secs),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=duration_secs + 5)
    return result.stdout


def iq_to_complex(raw: bytes) -> np.ndarray:
    u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    f = (u8 - 127.5) / 127.5
    return f[0::2] + 1j * f[1::2]


def spectrum(iq: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    fft = np.abs(np.fft.fftshift(np.fft.fft(iq)))
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1 / SAMP_RATE))
    return fft, freqs


def detect_signal(iq_baseline: np.ndarray, iq_tx: np.ndarray, centre_freq: int) -> int | None:
    """Find where the signal appeared by comparing TX spectrum to baseline.
    Returns frequency offset (Hz) from centre_freq, or None if not found."""
    # Use same length so FFT bins align
    n = min(len(iq_baseline), len(iq_tx))
    fft_base, freqs = spectrum(iq_baseline[:n])
    fft_tx, _       = spectrum(iq_tx[:n])

    # Zero out DC bin (LO leakage always wins there)
    dc_bin = len(freqs) // 2
    fft_base[dc_bin] = 0
    fft_tx[dc_bin]   = 0

    # Signal = bins that got stronger during TX
    ratio = fft_tx / (fft_base + 1e-9)

    # Find the peak ratio bin
    peak_bin = int(np.argmax(ratio))
    peak_ratio = ratio[peak_bin]
    offset_hz = int(freqs[peak_bin])
    actual_hz = centre_freq + offset_hz

    floor = np.median(fft_tx)
    peak_snr = 20 * np.log10(fft_tx[peak_bin] / floor) if floor > 0 else 0

    print(f"\n  Signal detected at offset {offset_hz:+d} Hz from {centre_freq/1e6:.3f} MHz")
    print(f"  → Actual frequency: {actual_hz/1e6:.4f} MHz")
    print(f"  → Ratio vs baseline: {peak_ratio:.1f}×  SNR: {peak_snr:.1f} dB")

    # Nearest 12.5 kHz channel
    channel = round(actual_hz / 12_500) * 12_500
    print(f"  → Nearest 12.5 kHz channel: {channel/1e6:.4f} MHz")

    if peak_ratio < 2.0:
        print("  ✗ No clear signal found (ratio too low — did you transmit?)")
        return None
    return offset_hz


def fm_demod(iq: np.ndarray, offset_hz: int) -> np.ndarray:
    """Shift signal from offset_hz to DC, FM discriminate, decimate to 16k."""
    t = np.arange(len(iq)) / SAMP_RATE
    iq_shifted = iq * np.exp(-1j * 2 * np.pi * offset_hz * t)
    phase = np.angle(iq_shifted[1:] * np.conj(iq_shifted[:-1]))
    audio = decimate(phase, 15, ftype='fir', zero_phase=True)
    return audio


def save_wav(audio: np.ndarray, path: str):
    peak = np.max(np.abs(audio))
    pcm = (audio / (peak + 1e-9) * 0.8 * 32767).astype(np.int16)
    rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(OUT_RATE)
        wf.writeframes(pcm.tobytes())
    open(path, 'wb').write(buf.getvalue())

    fft = np.abs(np.fft.rfft(pcm.astype(np.float32))); fft[0] = 0
    freqs = np.fft.rfftfreq(len(pcm), 1 / OUT_RATE)
    top5 = np.argsort(fft)[-5:][::-1]
    print(f"\n  Audio RMS: {rms:.0f}")
    print(f"  Top audio freqs: {', '.join(f'{freqs[i]:.0f} Hz' for i in top5 if freqs[i] > 50)}")
    print(f"  Saved: {path}")
    print(f"  Play: sox {path} -d")


# ── Step 1: baseline ──────────────────────────────────────
print("=" * 60)
print(f"Step 1: Baseline capture at {SEARCH_FREQ/1e6:.3f} MHz (do NOT transmit)...")
raw_base = capture_iq(5)
iq_base  = iq_to_complex(raw_base)
fft_b, freqs_b = spectrum(iq_base)
floor_b = np.median(fft_b)
print(f"  Baseline noise floor: {floor_b:.1f}  ({len(iq_base)} samples)")

# ── Step 2: TX capture ────────────────────────────────────
print("\n" + "=" * 60)
print(f"Step 2: Transmit for {DURATION}s when prompted...")
for i in range(3, 0, -1):
    print(f"  {i}...", flush=True); time.sleep(1)
print("  >>> TRANSMIT NOW <<<", flush=True)

raw_tx = capture_iq(DURATION)
iq_tx  = iq_to_complex(raw_tx)
fft_t, _ = spectrum(iq_tx)
floor_t = np.median(fft_t)
dc_bin = len(fft_t) // 2
fft_t[dc_bin] = 0
peak_snr = 20 * np.log10(np.max(fft_t) / floor_t)
print(f"  TX noise floor: {floor_t:.1f}  peak SNR (ex-DC): {peak_snr:.1f} dB")

# ── Step 3: auto-detect signal frequency ─────────────────
print("\n" + "=" * 60)
print("Step 3: Auto-detecting signal frequency...")
offset_hz = detect_signal(iq_base, iq_tx, SEARCH_FREQ)

if offset_hz is None:
    print("\nCould not detect signal. Check that you transmitted during the countdown.")
    sys.exit(1)

# ── Step 4: FM demodulate at detected frequency ───────────
print("\n" + "=" * 60)
print(f"Step 4: FM demodulating at {offset_hz:+d} Hz offset...")
audio = fm_demod(iq_tx, offset_hz)
save_wav(audio, "/tmp/diagnose_demod.wav")

# ── Step 5: report what receiver.py should use ───────────
actual_freq_mhz = (SEARCH_FREQ + offset_hz) / 1e6
channel_mhz     = round((SEARCH_FREQ + offset_hz) / 12_500) * 12_500 / 1e6
print("\n" + "=" * 60)
print("RESULT:")
print(f"  Your radio is transmitting on: {actual_freq_mhz:.4f} MHz")
print(f"  Nearest standard channel:      {channel_mhz:.4f} MHz")
print(f"  Recommended gain:              10 dB")
print(f"\nRun receiver with:")
print(f"  python3 receiver.py --freq {channel_mhz:.4f}M --mode fm --gain 10 --save-audio recordings/")
