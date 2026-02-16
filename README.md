# Seniorenradio

An easy-to-use and intuitive internet radio application for seniors, designed to run on Raspberry Pi with minimal resources (512 MB RAM).

## Features

- **5 configurable radio channels** via physical buttons
- **Simple on/off control** via selector switch
- **Voice announcements** when switching channels
- **Automatic retry** on stream connection failure
- **Safe shutdown** via long-press (5 seconds) on channel 1 button
- **Low resource usage** optimized for Raspberry Pi

## Hardware Requirements

- Raspberry Pi (any model with GPIO, 512 MB RAM minimum)
- 5 momentary push buttons (for channel selection)
- 1 selector switch (for start/stop)
- Audio output (3.5mm jack or USB audio device)
- Internet connection

### GPIO Wiring

| Component | GPIO Pin (BCM) |
|-----------|----------------|
| Channel 1 Button | GPIO 17 |
| Channel 2 Button | GPIO 22 |
| Channel 3 Button | GPIO 23 |
| Channel 4 Button | GPIO 24 |
| Channel 5 Button | GPIO 25 |
| Start/Stop Switch | GPIO 27 |

All buttons should be wired between GPIO and GND (internal pull-up resistors are enabled).

## Installation

### Prerequisites

```bash
# Install MPV player and build dependencies
sudo apt-get update
sudo apt-get install -y mpv libmpv-dev libmpv2 swig liblgpio-dev python3-dev

# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> [!NOTE]
> `swig` is required to build the `lgpio` GPIO library from source on Debian Trixie and newer.

```bash
# Add your user to the gpio group (required for GPIO access without root)
sudo usermod -aG gpio $USER
# Log out and back in for this to take effect
```

### Application Setup

```bash
# Clone the repository
git clone https://github.com/davidvornholt/seniorenradio.git
cd seniorenradio

# Install dependencies
uv sync

# Copy example configuration
cp config/config.example.yaml config/config.yaml

# Edit configuration
nano config/config.yaml

# Add your announcement audio files to audio/
# See audio/README.md for details
```

### Running

```bash
# Run directly (uses config/config.yaml by default)
uv run python -m src.main

# Or with verbose logging
uv run python -m src.main --verbose

# Or with custom config path
uv run python -m src.main --config /path/to/config.yaml

# Run with keyboard-based GPIO mock (no Raspberry Pi required)
uv run python -m src.main --gpio mock
```

GPIO mock controls:

- Keys `1`-`5`: Channel buttons 1-5
- Key `s`: Toggle selector switch ON/OFF
- Hold key `1`: Trigger shutdown request (mock mode logs only, no system shutdown)

### Running as a Service

Create a systemd service for automatic startup. Run these commands from inside the repository directory:

```bash
# Create the service file (auto-detects user and directory)
sudo tee /etc/systemd/system/seniorenradio.service > /dev/null <<EOF
[Unit]
Description=Seniorenradio Internet Radio
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$HOME/.local/bin/uv run python -m src.main
Restart=always
RestartSec=10
Environment="HOME=$HOME"

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable seniorenradio
sudo systemctl start seniorenradio
```

To verify the service is running:

```bash
# Check status
sudo systemctl status seniorenradio

# View logs (follow mode)
journalctl -u seniorenradio -f
```

## Configuration

Edit `config/config.yaml` to customize:

- **Audio backend**: `alsa` or `pipewire`
- **Audio device**: 3.5mm jack (`default`) or USB audio (`hw:1,0` / `plughw:1,0`)
- **Volume level**: 0-100
- **Radio channels**: Name, stream URL, announcement file
- **Retry settings**: Connection attempts and delay
- **Buffer settings**: Cache seconds and max buffer size; increase for high-latency or bursty networks, decrease for memory-constrained devices.
- **Watchdog settings**: Stream health checks and reconnect timing; tighten for unreliable networks and relax for stable, low-latency connections.
- **audio_dir**: Path to audio files (relative to config file, or absolute)
- **invert_switch**: Swap ON/OFF positions of selector switch (useful if wiring causes opposite behavior)

See `config/config.example.yaml` for a complete example.

## Audio Files

Place your announcement audio files in the `audio/` directory:

- `channel_1.mp3` through `channel_5.mp3`: Channel announcements
- `error_retrying.mp3`: Played when retrying connection
- `error_failed.mp3`: Played when all retries failed
- `error_no_internet.mp3`: Played when no internet connection
- `goodbye.mp3`: Played when radio is turned off
- `selector_off.mp3`: Played when radio starts with selector switch off
- `shutdown.mp3`: Played before system shutdown (triggered by holding channel 1 for 5 seconds)

See `audio/README.md` for recording tips and example scripts.

## Troubleshooting

### No audio output

1. Check volume: `alsamixer`
2. List audio devices: `aplay -l`
3. Test audio: `speaker-test -t wav -c 2`
4. Update config with correct device name

### Stream not connecting

1. Check internet connection: `ping google.de`
2. Test stream URL: `mpv --no-video "STREAM_URL"`
3. Check logs: `journalctl -u seniorenradio -f`

### Buttons not responding

1. Check GPIO wiring
2. Enable verbose logging: `--verbose`
3. Verify button connections to GND

## License

MIT License
