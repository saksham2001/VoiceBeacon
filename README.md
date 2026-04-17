# VoiceBeacon Node

A Raspberry Pi SDR node for the [VoiceBeacon](https://voicebeacon.fly.dev) Reverse Beacon Network. Tunes to a frequency, demodulates FM/AM/SSB voice audio, transcribes it with OpenAI Whisper, extracts callsigns, and reports spots to the VoiceBeacon API.

VoiceBeacon is the world's first Reverse Beacon Network for voice modes. Distributed nodes like this one listen passively and report what they hear — the backend clusters reports across nodes and publishes verified spots in real time.

---

## How a Node Works

```
RTL-SDR dongle
  ├─ FM/AM:  rtl_fm subprocess → S16LE PCM at 16 kHz → stdout
  └─ SSB:    rtl_sdr | csdr pipeline → S16LE PCM at 16 kHz → stdout

  receiver.py
    ├─ 3-second noise floor calibration at startup
    ├─ VOX state machine (100ms frames, 300ms hang time)
    │    IDLE → detect voice onset → VOICE_ACTIVE → detect end → dispatch
    ├─ Adaptive rolling noise floor (updates every 100ms during idle)
    ├─ OpenAI Whisper API (whisper-1, verbose_json for confidence)
    ├─ ITU callsign extraction from transcript
    └─ POST /v1/ingest/report → VoiceBeacon API
```

For FM/AM, the FM capture effect naturally suppresses noise when a carrier is present. The RMS gates distinguish carrier-only periods (below `silence_thr`) from voice modulation (between `silence_thr` and `noise_ceil`). SSB has no capture effect — calibration thresholds adapt to the flat noise floor.

---

## Hardware Requirements

- Raspberry Pi (any model with USB)
- RTL-SDR dongle (RTL2832U-based) — tested with:
  - RTL-SDR Blog V4 (R828D tuner)
  - Nooelec NESDR SMArt v5 (R820T2 tuner)
- Antenna appropriate for the target band (e.g. ~51 cm element for 2m)

---

## Software Requirements

**System packages:**
```bash
sudo apt-get install -y rtl-sdr sox
# For SSB (USB/LSB) support:
sudo apt-get update && sudo apt-get install -y csdr
```

**Python packages:**
```bash
pip3 install -r requirements.txt --break-system-packages
```

**Python 3.11+ required** (uses `int | None` union syntax).

---

## Setup

See **[SETUP.md](SETUP.md)** for the full setup guide.

**Quick start:**
```bash
bash setup.sh
```

The script installs dependencies, asks for your SDR hardware, frequency, credentials, and optionally sets up a systemd service to run on boot.

---

## Usage

```bash
python3 receiver.py --freq 145.150M --mode fm
```

On startup, the receiver calibrates the noise floor for 3 seconds (don't transmit). After calibration it prints the derived thresholds and starts listening.

### Options

| Flag | Default | Description |
|---|---|---|
| `--freq` | required | Frequency to tune (e.g. `145.150M`, `14225k`, `14225000`) |
| `--mode` | `fm` | Demodulation mode: `fm`, `am`, `usb`, `lsb` |
| `--gain` | `10` | SDR gain in dB. Lower is better for nearby/strong signals |
| `--device` | `0` | RTL-SDR device index (if multiple dongles connected) |
| `--save-audio` | off | Directory to save each chunk as a WAV file for debugging |
| `--max-tx-duration` | `60` | Force-send if transmission exceeds this many seconds (stuck PTT guard) |
| `--recal-threshold` | `10` | Force recalibration after this many consecutive no-speech chunks |
| `--silence-threshold` | auto | Manual RMS lower gate (skips if below — carrier only) |
| `--noise-floor` | auto | Manual RMS upper gate (skips if above — FM noise) |
| `--no-api` | off | Disable API posting (transcribe locally only, useful for testing) |

### Example: save audio for debugging

```bash
python3 receiver.py --freq 145.150M --mode fm --gain 10 --save-audio recordings/
# play back with:
sox recordings/20260404_180932.wav -d
```

Expected output when someone transmits:
```
[18:42:01] [VOX start]  RMS=450
[18:42:05] [VOX end 4.3s hang]
[18:42:05] W1AW de KD9XYZ  → recordings/20260404_184201.wav
[18:42:05] W1AW [reported]  conf=0.91
[18:42:05] KD9XYZ [reported]  conf=0.91
```

### Manual threshold override (skip calibration)

```bash
python3 receiver.py --freq 145.150M --mode fm \
  --silence-threshold 50 --noise-floor 800
```

---

## Gain Selection

The RTL-SDR's LNA can saturate on strong nearby signals even when the ADC isn't clipping, producing distorted audio that sounds like static. Use the minimum gain that gives clean audio:

| Scenario | Recommended gain |
|---|---|
| Handheld in same room | 5–15 dB |
| Station 10–50m away | 20–30 dB |
| Weak/distant signals | 35–45 dB |

Run `python3 diagnose.py` to measure SNR vs gain for your specific setup.

---

## Diagnostic Tool

`diagnose.py` is used to identify signal problems without `rtl_fm`:

```bash
python3 diagnose.py --freq 145.500M
```

It captures raw IQ directly from the SDR, compares a baseline spectrum to a transmission spectrum to auto-detect the exact signal frequency (in case your radio is slightly off), FM-demodulates in Python (bypassing `rtl_fm`), saves a WAV, and reports the correct `receiver.py` command to use.

---

## Key Technical Decisions

**`-s 160000` not `200000`** — `160000 ÷ 16000 = 10` is exact integer decimation. `200000 ÷ 16000 = 12.5` causes `rtl_fm` to use a slightly different internal rate, pitch-shifting the audio.

**No `-E deemp`** — FM de-emphasis is for broadcast FM (pre-emphasis on transmit). Amateur NFM has no pre-emphasis; applying de-emphasis distorts audio.

**Gain 10 dB default** — higher gains cause LNA compression on nearby signals, producing distorted audio that looks fine at the ADC (no clipping) but is unrecoverable.

**Auto-calibration** — the FM noise floor varies by gain, frequency, and local environment. A 3-second calibration at startup sets thresholds correctly for any combination.

---

## Roadmap

**Receiver**
- [x] FM/AM receive + Whisper transcription
- [x] Auto-calibrated noise floor gating
- [x] VOX-style chunking (detect TX start/end, 300ms hang time)
- [x] Adaptive rolling calibration (adjusts to changing noise floor)
- [x] Maximum transmission duration cap (stuck PTT guard)
- [x] SSB (USB/LSB) via `rtl_sdr | csdr` pipeline
- [x] ITU callsign extraction from Whisper transcripts
- [x] POST spots to VoiceBeacon API with Whisper confidence score
- [x] Periodic heartbeat to keep node active

**Setup & ops**
- [x] `setup.sh` — guided setup script for new Raspberry Pi nodes
- [x] Systemd service for unattended operation (created by `setup.sh`)
- [x] Frequency auto-detection diagnostic (`diagnose.py`)
- [ ] OTA update — `git pull` + service restart triggered remotely or on a schedule
- [ ] Selectable transcription model/provider (Whisper local via `faster-whisper`, or `whisper.cpp` for on-device)

**Remote management** (via VoiceBeacon web app)
- [ ] Change listening frequency and mode from the web interface
- [ ] Adjust squelch thresholds remotely
- [ ] Start/stop the receiver remotely
- [ ] View live transcripts and spots per node in real time
- [ ] Node status dashboard (uptime, last heartbeat, SNR, spot count)
