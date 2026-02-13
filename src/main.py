"""Seniorenradio - Main entry point.

A simple internet radio application for older adults on Raspberry Pi.
"""

import argparse
import logging
import platform
import signal
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from types import FrameType

from .audio import MpvAudioPlayer
from .config import DEFAULT_CONFIG_PATH, load_config
from .controller import RadioController
from .gpio import GpioController, RpiGpioAdapter
from .gpio_mock import KeyboardGpioAdapter
from .network import NetworkManager
from .tts import TtsSpeaker

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool) -> None:
    """Configure logging.

    Args:
        verbose: Enable debug logging if True.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments.
    """
    default_gpio_backend = get_default_gpio_backend()
    parser = argparse.ArgumentParser(
        prog="seniorenradio",
        description="A simple internet radio for older adults",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--gpio",
        choices=["rpi", "mock"],
        default=default_gpio_backend,
        help="GPIO backend to use (rpi or mock)",
    )
    return parser.parse_args()


def get_default_gpio_backend() -> str:
    """Select the default GPIO backend based on the current platform."""
    if is_raspberry_pi():
        return "rpi"
    return "mock"


@lru_cache(maxsize=1)
def is_raspberry_pi() -> bool:
    """Detect whether the current machine is a Raspberry Pi."""
    if platform.system() != "Linux":
        return False

    model_path = Path("/sys/firmware/devicetree/base/model")
    if not model_path.exists():
        return False

    try:
        model_text = model_path.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except OSError:
        return False

    return "Raspberry Pi" in model_text


def main() -> int:
    """Run the Seniorenradio application.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    args = parse_args()
    setup_logging(args.verbose)

    logger.info("Starting Seniorenradio")

    # Load configuration
    try:
        config = load_config(args.config)
        logger.info("Configuration loaded from %s", args.config)
    except FileNotFoundError as e:
        logger.exception("Configuration error: %s", e)
        return 1
    except ValueError as e:
        logger.exception("Invalid configuration: %s", e)
        return 1

    # Validate audio files exist
    for channel in config.channels:
        if not channel.announcement_file.exists():
            logger.warning(
                "Announcement file not found for channel '%s': %s",
                channel.name,
                channel.announcement_file,
            )

    # Validate error announcement files
    error_files = [
        ("retrying", config.error_announcements.retrying),
        ("failed", config.error_announcements.failed),
        ("no_internet", config.error_announcements.no_internet),
    ]
    for name, path in error_files:
        if not path.exists():
            logger.warning("Error announcement file '%s' not found: %s", name, path)

    # Initialize components
    audio_player = MpvAudioPlayer(
        audio_config=config.audio,
        retry_config=config.retry,
        watchdog_config=config.watchdog,
        error_announcements=config.error_announcements,
        goodbye_announcement=config.goodbye_announcement,
        selector_off_announcement=config.selector_off_announcement,
        shutdown_announcement=config.shutdown_announcement,
    )

    network_manager = NetworkManager(
        nmcli_path=config.wifi.nmcli_path,
        command_timeout_seconds=config.wifi.command_timeout_seconds,
        connect_timeout_seconds=config.wifi.connect_timeout_seconds,
        internet_check_hosts=config.watchdog.internet_check_hosts,
        internet_check_port=config.watchdog.internet_check_port,
        internet_check_timeout_seconds=config.watchdog.internet_check_timeout_seconds,
    )

    tts_speaker = TtsSpeaker(config=config.tts)

    radio_controller = RadioController(
        config=config,
        audio_player=audio_player,
        network_manager=network_manager,
        tts_speaker=tts_speaker,
    )

    gpio_mode = args.gpio
    is_pi = is_raspberry_pi()
    if gpio_mode == "rpi" and not is_pi:
        logger.warning("RPi GPIO selected but Raspberry Pi not detected; using mock.")
        gpio_mode = "mock"
    if gpio_mode == "mock":
        gpio = KeyboardGpioAdapter(
            channel_pins=config.gpio.channel_pins,
            switch_pin=config.gpio.switch_pin,
        )
        logger.info(
            "GPIO mock controls: 1-5 = channels, s = toggle switch, hold 1 for shutdown"
        )
    else:
        gpio = RpiGpioAdapter()

    # Define shutdown callback for long-press
    def handle_shutdown_request() -> None:
        nonlocal shutdown_requested
        radio_controller.handle_shutdown_request()
        shutdown_requested = True
        if gpio_mode == "mock":
            logger.info("Mock GPIO: shutdown requested (no system shutdown)")
            return
        # Execute system shutdown after announcement
        logger.info("Executing system shutdown")
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)

    gpio_controller = GpioController(
        config=config.gpio,
        gpio=gpio,
        on_channel_button=radio_controller.handle_channel_button,
        on_switch_change=radio_controller.handle_switch_change,
        on_shutdown_requested=handle_shutdown_request,
        on_debug_requested=radio_controller.handle_debug_request,
        debug_long_press_seconds=config.debug.long_press_seconds,
    )

    # Setup signal handlers for graceful shutdown
    shutdown_requested = False

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start GPIO controller
    gpio_controller.start()

    # Handle startup based on switch position
    initial_switch_position = gpio_controller.get_switch_position()
    radio_controller.handle_startup(initial_switch_position)

    logger.info("Seniorenradio is running. Press Ctrl+C to stop.")

    # Main loop - wait for shutdown signal
    try:
        while not shutdown_requested:
            signal.pause()
    except KeyboardInterrupt:
        pass

    # Graceful shutdown
    logger.info("Shutting down Seniorenradio")
    gpio_controller.stop()
    radio_controller.shutdown()
    logger.info("Goodbye!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
