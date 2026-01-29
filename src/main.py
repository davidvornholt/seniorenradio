"""Seniorenradio - Main entry point.

A simple internet radio application for older adults on Raspberry Pi.
"""

import argparse
import logging
import signal
import sys
from pathlib import Path
from types import FrameType

from audio import MpvAudioPlayer
from config import DEFAULT_CONFIG_PATH, load_config
from controller import RadioController
from gpio import GpioController, RpiGpioAdapter

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
    return parser.parse_args()


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
        error_announcements=config.error_announcements,
    )

    radio_controller = RadioController(
        config=config,
        audio_player=audio_player,
    )

    gpio = RpiGpioAdapter()
    gpio_controller = GpioController(
        config=config.gpio,
        gpio=gpio,
        on_channel_button=radio_controller.handle_channel_button,
        on_switch_change=radio_controller.handle_switch_change,
    )

    # Setup signal handlers for graceful shutdown
    shutdown_requested = False

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            logger.info("Shutdown signal received (%s)", signal.Signals(signum).name)

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
