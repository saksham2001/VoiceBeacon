#!/usr/bin/env python3
"""
VoiceBeacon SDR Receiver — FM/AM/SSB node

Usage:
    python3 receiver.py --freq 145.150M --mode fm
    python3 receiver.py --freq 1710k    --mode am  --gain 30
    python3 receiver.py --freq 14.225M  --mode usb --gain 30
    python3 receiver.py --freq 145.150M --mode fm  --save-audio recordings/
    python3 receiver.py --freq 145.150M --mode fm  --no-api
"""

import argparse
import io
import os
import re
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")

# ── Audio constants ────────────────────────────────────────────────────────────
SAMPLE_RATE        = 16000   # Hz — output sample rate (Whisper target)
VOX_FRAME_BYTES    = 3200    # 100ms @ 16 kHz (1600 samples × 2 bytes)
VOX_HANG_FRAMES    = 15      # 1.5s hang time — bridges inter-word/phrase pauses
VOX_MIN_CLIP_FRAMES = 10     # 1.0s minimum — clips shorter than this are not sent to Whisper
NOISE_HISTORY_SIZE = 100     # ~10s of idle frames at 100ms each

# ── SSB constants ──────────────────────────────────────────────────────────────
SSB_IQ_RATE = 240000         # 240k ÷ 16k = 15 — exact integer decimation

# ── API ────────────────────────────────────────────────────────────────────────
_API_URL     = os.getenv("VOICEBEACON_API_URL", "https://voicebeacon-api.fly.dev")
_API_TOKEN   = os.getenv("VOICEBEACON_NODE1_TOKEN", "")
_API_TIMEOUT = 10  # seconds

# ── Whisper ────────────────────────────────────────────────────────────────────
# Whisper uses this as preceding audio context (vocabulary priming), not as instructions.
# It should resemble actual radio speech so Whisper recognises phonetics and callsign tokens.
WHISPER_PROMPT = (
    "W1AW de K7THI. Whiskey One Alpha Whiskey, Kilo Seven Tango Hotel India. "
    "Victor Echo Three X-ray Yankee Zulu, November Zero Alpha Bravo Charlie. "
    "Kilo Delta Three Alpha Lima X-ray, Kilo Delta Three Alpha X-ray Lima. "
    "Lima Lima Nine X-ray X-ray. X-ray ray Lima. "
    "Five nine, QSL, 73, CQ CQ de W1AW, stroke portable, over and out."
)
_HALLUCINATIONS  = {"thank you for watching", "thanks for watching", "thank you"}

# ── Callsign regex (ITU format) ────────────────────────────────────────────────
_CALLSIGN_RE = re.compile(
    r'\b'
    r'(?:[A-Z]{1,2}[0-9]|[A-Z][0-9][A-Z]|[0-9][A-Z]{1,2}[0-9])'  # prefix
    r'[A-Z]{1,4}'                                                     # suffix
    r'(?:/[A-Z0-9]+)?'                                                # optional /P /M
    r'\b'
)

# ── GPT callsign-extraction prompt ────────────────────────────────────────────
_EXTRACT_SYSTEM = """\
You extract amateur radio callsigns from voice transmission transcripts.

## Phonetic alphabet (NATO standard)
Alpha=A  Bravo=B  Charlie=C  Delta=D  Echo=E  Foxtrot=F  Golf=G  Hotel=H
India=I  Juliet=J  Kilo=K  Lima=L  Mike=M  November=N  Oscar=O  Papa=P
Quebec=Q  Romeo=R  Sierra=S  Tango=T  Uniform=U  Victor=V  Whiskey=W
X-ray=X  Yankee=Y  Zulu=Z

## Spoken numbers
zero=0  one=1  two=2  three=3  four=4  five=5  six=6  seven=7  eight=8  nine/niner=9

## Patterns to recognise
- Full phonetics:  "Whiskey One Alpha Whiskey" → W1AW
- Mixed:           "W one Alpha Whiskey"        → W1AW
- "de" separator:  "W1AW de K7THI"             → W1AW, K7THI  (both stations)
- "this is" / "here is": extract the callsign that follows
- CQ call:         "CQ CQ de W1AW"             → W1AW
- Portable:        "stroke portable" / "slash P" → append /P
- Mobile:          "stroke mobile"  / "slash M"  → append /M
- Digit before phonetic letter: "Five November Zero" → 5N0 (valid DX prefix)
- Number before letter group: treat leading spoken number as ITU prefix digit

## Output rules
- Return one callsign per line, uppercase, no punctuation, no commentary.
- Valid ITU callsign format: 1–2 letter/digit prefix + 1 digit + 1–4 letters  (e.g. W1AW, VE3XYZ, 9V1YC)
- Only include callsigns you are confident about.  Do NOT guess.
- If no valid callsign can be identified, return exactly one word: NONE
"""


# ── Frequency helpers ──────────────────────────────────────────────────────────

def _parse_freq_hz(freq_str: str) -> int:
    """Parse '145.150M', '14225k', '146520000' → integer Hz."""
    s = freq_str.strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("K"):
        return int(float(s[:-1]) * 1_000)
    return int(s)


# ── Subprocess command builders ────────────────────────────────────────────────

def build_rtl_fm_cmd(freq: str, mode: str, gain: float, device: int) -> list[str]:
    """FM or AM demodulation via rtl_fm → S16LE PCM at 16 kHz."""
    return [
        "rtl_fm",
        "-d", str(device),
        "-M", mode,
        "-f", freq,
        "-s", "160000",  # 160k ÷ 16k = 10 — exact integer decimation
        "-r", str(SAMPLE_RATE),
        "-g", str(gain),
        "-E", "dc",      # IQ DC offset removal (RTL2832 LO leakage)
        "-",
    ]


def build_ssb_cmd(freq: str, mode: str, gain: float, device: int) -> list[str]:
    """USB or LSB demodulation via rtl_sdr | csdr → S16LE PCM at 16 kHz.

    Tunes 10 kHz above the target frequency to avoid the LO DC spike, then
    shifts back in csdr. For LSB, the shift direction is flipped.
    """
    freq_hz   = _parse_freq_hz(freq)
    offset_hz = 10_000
    tune_hz   = freq_hz + offset_hz
    shift     = -offset_hz / SSB_IQ_RATE  # -10000/240000 = -0.041667
    if mode == "lsb":
        shift = -shift

    rtl_cmd  = f"rtl_sdr -d {device} -f {tune_hz} -s {SSB_IQ_RATE} -g {gain} -"
    csdr_cmd = (
        f"csdr convert_u8_f"
        f" | csdr shift_addition_cc {shift:.6f}"
        f" | csdr fir_decimate_cc 15 0.05 HAMMING"
        f" | csdr realpart_cf"
        f" | csdr agc_ff"
        f" | csdr convert_f_s16"
    )
    return ["bash", "-c", f"{rtl_cmd} | {csdr_cmd}"]


# ── Audio utilities ────────────────────────────────────────────────────────────

def pcm_to_wav(raw_pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw_pcm)
    return buf.getvalue()


def rms(raw_pcm: bytes) -> float:
    samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


# ── Callsign extraction ────────────────────────────────────────────────────────

def extract_callsigns(text: str) -> list[str]:
    """Regex-only extraction — fast fallback for when the LLM call fails."""
    return _CALLSIGN_RE.findall(text.upper())


def extract_callsigns_llm(client: OpenAI, text: str) -> list[str]:
    """Use GPT-4o-mini to extract and normalise callsigns from a transcript.

    Handles phonetic alphabet, spoken numbers, 'de' separator, /P /M suffixes,
    CQ calls, mixed phonetic/literal, and DX prefix formats.
    Falls back to regex extraction if the API call fails.
    LLM output is always validated against _CALLSIGN_RE before being returned.
    """
    if not text:
        return []
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": text},
            ],
            max_tokens=80,
            temperature=0,
        )
        raw = (response.choices[0].message.content or "").strip().upper()
    except Exception:
        # API error — degrade gracefully to regex
        return extract_callsigns(text)

    if not raw or raw == "NONE":
        return []

    # Validate every line the LLM returned against the ITU regex.
    # This catches hallucinated "callsigns" that don't match the format.
    callsigns = []
    for line in raw.splitlines():
        candidate = line.strip()
        if candidate and _CALLSIGN_RE.fullmatch(candidate):
            callsigns.append(candidate)
    return callsigns


# ── Whisper transcription ──────────────────────────────────────────────────────

def transcribe(client: OpenAI, wav_bytes: bytes) -> tuple[str, float]:
    """Transcribe WAV audio using Whisper. Returns (text, confidence_raw).

    confidence_raw is derived from per-segment no_speech_prob:
        confidence = 1.0 - mean(no_speech_prob across segments)
    Falls back to 0.7 if no segment data is available.
    Returns ("", 0.0) if hallucination or empty.
    """
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.wav", wav_bytes, "audio/wav"),
        response_format="verbose_json",
        prompt=WHISPER_PROMPT,
    )
    text = (response.text or "").strip()

    norm = lambda s: s.lower().strip(".").strip()
    t, p = norm(text), norm(WHISPER_PROMPT)
    if not t or t == p or t in p or p in t or t in _HALLUCINATIONS:
        return "", 0.0

    segments = getattr(response, "segments", None) or []
    if segments:
        avg_no_speech = sum(s.no_speech_prob for s in segments) / len(segments)
        confidence = round(1.0 - avg_no_speech, 3)
    else:
        confidence = 0.7

    return text, confidence


# ── Calibration ────────────────────────────────────────────────────────────────

def calibrate(proc) -> tuple[deque, float, float]:
    """Measure noise floor for 3 seconds and derive gating thresholds.
    Returns (noise_history deque, noise_ceil, silence_thr).
    """
    print("Calibrating noise floor (do not transmit for 3 seconds)...", flush=True)
    cal_bytes = SAMPLE_RATE * 2 * 3
    cal_buf   = bytearray()
    while len(cal_buf) < cal_bytes:
        d = proc.stdout.read(VOX_FRAME_BYTES)
        if not d:
            break
        cal_buf.extend(d)
    noise_rms   = rms(bytes(cal_buf[:cal_bytes]))
    noise_ceil  = noise_rms * 0.80  # 20% below noise floor — requires meaningful FM capture to trigger
    silence_thr = noise_rms * 0.15
    print(f"  Noise RMS: {noise_rms:.0f}  →  ceiling: {noise_ceil:.0f}  silence: {silence_thr:.0f}\n")
    history = deque([noise_rms] * 20, maxlen=NOISE_HISTORY_SIZE)
    return history, noise_ceil, silence_thr


# ── VoiceBeacon API ────────────────────────────────────────────────────────────

def _api_headers() -> dict:
    return {
        "Authorization": f"Bearer {_API_TOKEN}",
        "Content-Type":  "application/json",
    }


def post_report(freq_hz: int, mode: str, callsign: str,
                transcript: str, confidence: float, duration_ms: int) -> bool:
    """POST a spot report to /v1/ingest/report. Returns True if accepted."""
    if not _API_TOKEN:
        return False
    payload = {
        "frequency_hz":      freq_hz,
        "mode":              mode,
        "reported_callsign": callsign,
        "transcript":        transcript,
        "confidence_raw":    confidence,
        "audio_duration_ms": duration_ms,
    }
    try:
        r = requests.post(
            f"{_API_URL}/v1/ingest/report",
            json=payload, headers=_api_headers(), timeout=_API_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json().get("accepted", False)
        if r.status_code == 401:
            print("[API] Auth failed (401) — check VOICEBEACON_NODE1_TOKEN")
        return False
    except requests.RequestException as e:
        print(f"[API] Request error: {e}")
        return False


def send_heartbeat() -> None:
    """POST to /v1/ingest/heartbeat (best-effort, fire-and-forget)."""
    if not _API_TOKEN:
        return
    try:
        requests.post(
            f"{_API_URL}/v1/ingest/heartbeat",
            json={"software_version": "voicebeacon-node/0.3.0"},
            headers=_api_headers(), timeout=_API_TIMEOUT,
        )
    except requests.RequestException:
        pass


def _start_heartbeat() -> None:
    def _loop():
        while True:
            send_heartbeat()
            time.sleep(60)
    threading.Thread(target=_loop, daemon=True).start()


# ── Dispatch ───────────────────────────────────────────────────────────────────

def _dispatch(pcm_bytes: bytearray, ts: str, save_dir,
              client: OpenAI, freq_hz: int, mode: str,
              no_api: bool) -> str:
    """Convert PCM → WAV, transcribe, extract callsigns, post to API."""
    wav      = pcm_to_wav(bytes(pcm_bytes))
    wav_path = None
    if save_dir:
        wav_path = save_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.wav"
        wav_path.write_bytes(wav)

    duration_ms = int(len(pcm_bytes) / (SAMPLE_RATE * 2) * 1000)
    print(f"[{ts}] Transcribing... ({duration_ms/1000:.1f}s)", end="", flush=True)

    try:
        text, confidence = transcribe(client, wav)
    except Exception as e:
        suffix = f"  → {wav_path}" if wav_path else ""
        print(f"\r[{ts}] [error: {e}]{suffix}")
        return ""

    callsigns = extract_callsigns_llm(client, text) if text else []
    suffix    = f"  → {wav_path}" if wav_path else ""

    if callsigns:
        print(f"\r[{ts}] {text}{suffix}")
        for cs in callsigns:
            if no_api:
                print(f"[{ts}] {cs} [api-disabled]")
            else:
                ok  = post_report(freq_hz, mode, cs, text, confidence, duration_ms)
                tag = "reported" if ok else "local-only"
                print(f"[{ts}] {cs} [{tag}]  conf={confidence:.2f}")
    else:
        print(f"\r[{ts}] {text or '[no speech]'}{suffix}")

    return text


# ── Main ───────────────────────────────────────────────────────────────────────

def parse_args():
    # Env-var defaults let the service run with no CLI args (freq/mode/gain set in .env)
    _env_freq   = os.getenv("VOICEBEACON_FREQ")
    _env_mode   = os.getenv("VOICEBEACON_MODE", "fm")
    _env_gain   = os.getenv("VOICEBEACON_GAIN", "10")
    _env_device = os.getenv("VOICEBEACON_DEVICE", "0")
    _env_debug  = os.getenv("VOICEBEACON_DEBUG", "").lower() in ("1", "true", "yes")

    p = argparse.ArgumentParser(description="VoiceBeacon SDR receiver + Whisper transcription")
    p.add_argument("--freq",   required=(_env_freq is None), default=_env_freq,
                   help="Frequency (e.g. 145.150M, 14225k, 146520000) [env: VOICEBEACON_FREQ]")
    p.add_argument("--mode",   default=_env_mode, choices=["fm", "am", "usb", "lsb"],
                   help="Demodulation mode (default: fm) [env: VOICEBEACON_MODE]")
    p.add_argument("--gain",   type=float, default=float(_env_gain),
                   help="SDR gain in dB (default: 10) [env: VOICEBEACON_GAIN]")
    p.add_argument("--device", type=int, default=int(_env_device),
                   help="RTL-SDR device index (default: 0) [env: VOICEBEACON_DEVICE]")
    p.add_argument("--save-audio", metavar="DIR",
                   help="Save each chunk as a WAV file in this directory")
    p.add_argument("--debug", action="store_true", default=_env_debug,
                   help="Save all transcribed chunks as WAV files in recordings/ [env: VOICEBEACON_DEBUG]")
    p.add_argument("--max-tx-duration", type=float, default=60.0,
                   help="Force-send if TX exceeds this many seconds (default: 60)")
    p.add_argument("--recal-threshold", type=int, default=10,
                   help="Force recalibration after N consecutive no-speech chunks (default: 10)")
    p.add_argument("--silence-threshold", type=float, default=None,
                   help="Manual RMS lower gate (carrier-only, skip)")
    p.add_argument("--noise-floor", type=float, default=None,
                   help="Manual RMS upper gate (FM noise, no signal, skip)")
    p.add_argument("--no-api", action="store_true",
                   help="Disable API reporting (transcribe locally only)")
    return p.parse_args()


def main():
    args        = parse_args()
    freq_hz     = _parse_freq_hz(args.freq)
    max_tx_bytes = int(args.max_tx_duration * SAMPLE_RATE * 2)

    client   = OpenAI()
    save_dir = None
    if args.save_audio:
        save_dir = Path(args.save_audio)
    elif args.debug:
        save_dir = Path(__file__).parent / "recordings"
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    # Choose pipeline
    if args.mode in ("usb", "lsb"):
        cmd = build_ssb_cmd(args.freq, args.mode, args.gain, args.device)
    else:
        cmd = build_rtl_fm_cmd(args.freq, args.mode, args.gain, args.device)

    print(f"[{time.strftime('%H:%M:%S')}] {args.freq} {args.mode.upper()} "
          f"gain={args.gain} max-tx={args.max_tx_duration:.0f}s "
          f"api={'off' if args.no_api else 'on'}")
    if args.mode in ("usb", "lsb"):
        print("  [SSB mode: calibration thresholds may need manual tuning for your noise floor]")
    if save_dir:
        print(f"  Saving audio → {save_dir.resolve()}")
    print()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Initialise thresholds
    manual_thresholds = (args.silence_threshold is not None or args.noise_floor is not None)
    if manual_thresholds:
        silence_thr   = args.silence_threshold or 100.0
        noise_ceil    = args.noise_floor or 2500.0
        noise_history = deque(maxlen=NOISE_HISTORY_SIZE)
        print(f"  Manual thresholds: silence>{silence_thr:.0f}  noise<{noise_ceil:.0f}\n")
    else:
        noise_history, noise_ceil, silence_thr = calibrate(proc)

    # Start heartbeat (fires at startup, then every 60s)
    if not args.no_api:
        _start_heartbeat()

    # ── VOX state machine ──────────────────────────────────────────────────────
    state           = "IDLE"
    voice_buf       = bytearray()
    hang_count      = 0
    tx_start_time   = None
    no_speech_count = 0

    try:
        while True:
            data = proc.stdout.read(VOX_FRAME_BYTES)
            if not data:
                print("[receiver] rtl_fm/rtl_sdr ended.", file=sys.stderr)
                break

            level          = rms(data)
            ts             = time.strftime("%H:%M:%S")
            in_voice_range = silence_thr <= level <= noise_ceil

            if state == "IDLE":
                # Adaptive noise floor: sample only during idle frames
                if not manual_thresholds:
                    noise_history.append(level)
                    if len(noise_history) >= 10:
                        noise_rms   = float(np.mean(noise_history))
                        noise_ceil  = noise_rms * 0.80
                        silence_thr = noise_rms * 0.15

                if in_voice_range:
                    state         = "VOICE_ACTIVE"
                    tx_start_time = time.time()
                    voice_buf     = bytearray(data)
                    hang_count    = 0
                    print(f"[{ts}] [VOX start]  RMS={level:.0f}")

            elif state == "VOICE_ACTIVE":
                voice_buf.extend(data)

                if in_voice_range:
                    hang_count = 0
                else:
                    hang_count += 1

                tx_seconds = time.time() - tx_start_time
                hit_hang   = hang_count >= VOX_HANG_FRAMES
                hit_max    = len(voice_buf) >= max_tx_bytes

                if hit_hang or hit_max:
                    reason = "max-duration" if hit_max else "hang"
                    min_bytes = VOX_MIN_CLIP_FRAMES * VOX_FRAME_BYTES
                    if len(voice_buf) < min_bytes:
                        print(f"[{ts}] [VOX drop {tx_seconds:.1f}s — too short ({len(voice_buf)//3200} frames < {VOX_MIN_CLIP_FRAMES} min)]")
                        state, voice_buf, hang_count, tx_start_time = "IDLE", bytearray(), 0, None
                        continue
                    print(f"[{ts}] [VOX end {tx_seconds:.1f}s {reason}]")
                    text = _dispatch(
                        voice_buf, ts, save_dir, client,
                        freq_hz, args.mode, args.no_api,
                    )

                    if text:
                        no_speech_count = 0
                    else:
                        no_speech_count += 1
                        if no_speech_count >= args.recal_threshold:
                            print(f"[{ts}] [recalibrating — {no_speech_count} silent chunks]")
                            noise_history.clear()
                            no_speech_count = 0

                    state         = "IDLE"
                    voice_buf     = bytearray()
                    hang_count    = 0
                    tx_start_time = None

    except KeyboardInterrupt:
        print("\n[receiver] Interrupted.")
        if state == "VOICE_ACTIVE" and len(voice_buf) > 0:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] [VOX flush on exit]")
            _dispatch(voice_buf, ts, save_dir, client, freq_hz, args.mode, args.no_api)
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
