# VoiceBeacon Node — Setup Guide

## Prerequisites

- Raspberry Pi (any model with USB)
- RTL-SDR dongle (RTL2832U-based)
- Antenna for your target band
- Internet connection (for OpenAI Whisper API)
- OpenAI API key — [platform.openai.com](https://platform.openai.com)
- VoiceBeacon node token — issued by a VoiceBeacon admin

---

## Quick Setup

Clone the repo and run the setup script:

```bash
git clone https://github.com/your-org/VoiceBeacon.git
cd VoiceBeacon
bash setup.sh
```

The script will:
1. Install system packages (`rtl-sdr`, `sox`, `csdr`)
2. Install Python packages from `requirements.txt`
3. Ask which SDR hardware you have
4. Ask for your frequency, mode, and gain
5. Ask for your OpenAI and VoiceBeacon API credentials
6. Write a `.env` file with your settings
7. Optionally create a systemd service to run on boot

---

## Manual Setup

### 1. Install system packages

```bash
sudo apt-get install -y rtl-sdr sox python3-pip
```

For **SSB (USB/LSB)** modes, `csdr` is also required. It's not in the Raspberry Pi OS apt repos, so build it from source:

```bash
sudo apt-get install -y cmake libfftw3-dev git
git clone --depth 1 https://github.com/ha7ilm/csdr.git
cmake -S csdr -B csdr/build -DCMAKE_BUILD_TYPE=Release
make -C csdr/build -j$(nproc) && sudo make -C csdr/build install
```

`setup.sh` does this automatically when you select `usb` or `lsb` mode.

For **RTL-SDR Blog V4** dongles, you may need the V4 driver:
```bash
sudo apt-get purge rtl-sdr && sudo apt-get install rtl-sdr-blog
# or see: https://www.rtl-sdr.com/V4/
```

### 2. Install Python packages

```bash
pip3 install -r requirements.txt --break-system-packages
```

### 3. Create `.env`

```env
OPENAI_API_KEY=sk-...
VOICEBEACON_NODE1_TOKEN=...
VOICEBEACON_API_URL=https://voicebeacon-api.fly.dev

# Receiver defaults (overridable with CLI flags)
VOICEBEACON_FREQ=145.150M
VOICEBEACON_MODE=fm
VOICEBEACON_GAIN=10
VOICEBEACON_DEVICE=0
```

### 4. Verify SDR is detected

```bash
rtl_test -t
```

Expected: `Found X device(s)`. If you see `usb_open error -3`:
```bash
sudo rmmod dvb_usb_rtl28xxu
```

### 5. Run the receiver

```bash
python3 receiver.py
# Or override .env settings with CLI flags:
python3 receiver.py --freq 145.150M --mode fm --gain 10
```

Don't transmit for the first 3 seconds — auto-calibration is running.

---

## Running as a Service

The setup script handles this automatically. To do it manually:

```bash
sudo nano /etc/systemd/system/voicebeacon.service
```

```ini
[Unit]
Description=VoiceBeacon SDR Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/VoiceBeacon
EnvironmentFile=/home/pi/VoiceBeacon/.env
ExecStart=/usr/bin/python3 /home/pi/VoiceBeacon/receiver.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable voicebeacon
sudo systemctl start voicebeacon
sudo journalctl -u voicebeacon -f   # follow logs
```

---

## Changing Settings

Edit `.env` and restart:

```bash
nano /home/pi/VoiceBeacon/.env
sudo systemctl restart voicebeacon
```

Or use CLI flags (override `.env`):

```bash
python3 receiver.py --freq 146.520M --mode fm --gain 20 --no-api
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `rtl_test` shows no devices | Check USB; try `lsusb \| grep -i realtek` |
| `usb_open error -3` | `sudo rmmod dvb_usb_rtl28xxu` |
| RTL-SDR Blog V4 not working | Install V4 driver — https://www.rtl-sdr.com/V4/ |
| `[no signal]` even when transmitting | Gain too high; try `--gain 5` |
| Audio sounds like static | LNA saturating; lower gain |
| `[no speech]` on every chunk | Run `python3 diagnose.py --freq <freq>` to check audio |
| Service fails to start | Check `sudo journalctl -u voicebeacon` |
| API shows `[local-only]` | Token missing or node not yet approved |

---

## Gain Selection

| Scenario | Recommended gain |
|---|---|
| Handheld in same room | 5–10 dB |
| Station 10–100m away | 15–25 dB |
| Station 1–10 km away | 25–35 dB |
| Weak/distant HF signals | 35–45 dB |

Run `python3 diagnose.py --freq <freq>` for an automated SNR-vs-gain sweep at your specific setup.
