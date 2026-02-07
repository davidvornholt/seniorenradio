"""GPIO controller for Seniorenradio.

Handles button inputs with software debouncing and selector switch monitoring.
"""

import logging
import time
from collections.abc import Callable
from threading import Lock, Thread
from typing import Protocol

from .models import GpioConfig, SwitchPosition

logger = logging.getLogger(__name__)

# Type aliases for callbacks
ChannelCallback = Callable[[int], None]
SwitchCallback = Callable[[SwitchPosition], None]
ShutdownCallback = Callable[[], None]

# Long-press threshold in seconds
LONG_PRESS_THRESHOLD_SECONDS = 5.0


class GpioInterface(Protocol):
    """Protocol for GPIO operations (allows testing without hardware)."""

    def setup_input(self, pin: int, pull_up: bool) -> None:
        """Configure a pin as input."""
        ...

    def read(self, pin: int) -> bool:
        """Read pin state (True = HIGH)."""
        ...

    def add_event_detect(
        self,
        pin: int,
        edge: str,
        callback: Callable[[int], None],
        bouncetime: int,
    ) -> None:
        """Add edge detection with callback."""
        ...

    def cleanup(self) -> None:
        """Clean up GPIO resources."""
        ...


class RpiGpioAdapter:
    """Adapter for RPi.GPIO library."""

    def __init__(self) -> None:
        """Initialize RPi.GPIO in BCM mode."""
        import RPi.GPIO as GPIO

        self._gpio = GPIO
        self._gpio.setmode(GPIO.BCM)
        self._gpio.setwarnings(False)

    def setup_input(self, pin: int, pull_up: bool) -> None:
        """Configure a pin as input with pull-up/down resistor."""
        pud = self._gpio.PUD_UP if pull_up else self._gpio.PUD_DOWN
        self._gpio.setup(pin, self._gpio.IN, pull_up_down=pud)

    def read(self, pin: int) -> bool:
        """Read pin state."""
        return bool(self._gpio.input(pin))

    def add_event_detect(
        self,
        pin: int,
        edge: str,
        callback: Callable[[int], None],
        bouncetime: int,
    ) -> None:
        """Add edge detection with callback."""
        edge_type = self._gpio.FALLING if edge == "falling" else self._gpio.RISING
        self._gpio.add_event_detect(
            pin,
            edge_type,
            callback=callback,
            bouncetime=bouncetime,
        )

    def cleanup(self) -> None:
        """Clean up GPIO resources."""
        self._gpio.cleanup()


class GpioController:
    """GPIO controller for buttons and selector switch."""

    def __init__(
        self,
        config: GpioConfig,
        gpio: GpioInterface,
        on_channel_button: ChannelCallback,
        on_switch_change: SwitchCallback,
        on_shutdown_requested: ShutdownCallback | None = None,
    ) -> None:
        """Initialize GPIO controller.

        Args:
            config: GPIO pin configuration.
            gpio: GPIO interface implementation.
            on_channel_button: Callback for channel button presses.
            on_switch_change: Callback for switch position changes.
            on_shutdown_requested: Callback for shutdown request via long-press.
        """
        self._config = config
        self._gpio = gpio
        self._on_channel_button = on_channel_button
        self._on_switch_change = on_switch_change
        self._on_shutdown_requested = on_shutdown_requested
        self._lock = Lock()
        self._last_button_time: dict[int, float] = {}
        self._last_switch_state: bool | None = None
        self._running = False
        self._switch_monitor_thread: Thread | None = None
        self._channel1_press_start: float | None = None
        self._long_press_monitor_thread: Thread | None = None
        self._shutdown_triggered = False

    def start(self) -> None:
        """Initialize GPIO pins and start monitoring."""
        logger.info("Initializing GPIO controller")

        # Setup channel buttons with pull-up resistors
        for i, pin in enumerate(self._config.channel_pins):
            self._gpio.setup_input(pin, pull_up=True)
            self._last_button_time[pin] = 0
            self._gpio.add_event_detect(
                pin,
                edge="falling",
                callback=self._handle_button_press,
                bouncetime=self._config.debounce_ms,
            )
            logger.info("Channel %d button configured on GPIO %d", i + 1, pin)

        # Setup selector switch
        self._gpio.setup_input(self._config.switch_pin, pull_up=True)
        self._last_switch_state = self._gpio.read(self._config.switch_pin)
        logger.info(
            "Selector switch configured on GPIO %d (initial state: %s)",
            self._config.switch_pin,
            "ON" if self._last_switch_state else "OFF",
        )

        # Start switch monitoring thread
        self._running = True
        self._switch_monitor_thread = Thread(
            target=self._monitor_switch,
            daemon=True,
        )
        self._switch_monitor_thread.start()

        # Start long-press monitor if shutdown callback is provided
        if self._on_shutdown_requested is not None:
            self._long_press_monitor_thread = Thread(
                target=self._monitor_long_press,
                daemon=True,
            )
            self._long_press_monitor_thread.start()

    def _handle_button_press(self, pin: int) -> None:
        """Handle button press event with debouncing.

        Args:
            pin: GPIO pin that triggered the event.
        """
        current_time = time.time()
        debounce_seconds = self._config.debounce_ms / 1000.0

        with self._lock:
            last_time = self._last_button_time.get(pin, 0)
            if current_time - last_time < debounce_seconds:
                return  # Ignore bouncing

            self._last_button_time[pin] = current_time

        # Find channel index for this pin
        try:
            channel_index = self._config.channel_pins.index(pin)
            logger.info("Channel %d button pressed (GPIO %d)", channel_index + 1, pin)

            # Track press start for channel 1 (index 0) long-press detection
            if channel_index == 0 and self._on_shutdown_requested is not None:
                with self._lock:
                    if not self._shutdown_triggered:
                        self._channel1_press_start = current_time
                        logger.debug(
                            "Channel 1 press started, monitoring for long-press"
                        )

            self._on_channel_button(channel_index)
        except ValueError:
            logger.warning("Unknown button pin: %d", pin)

    def _monitor_long_press(self) -> None:
        """Monitor channel 1 button for long-press to trigger shutdown."""
        channel1_pin = self._config.channel_pins[0]

        while self._running:
            with self._lock:
                press_start = self._channel1_press_start

            if press_start is not None:
                # Check if button is still held
                button_still_pressed = not self._gpio.read(channel1_pin)

                if button_still_pressed:
                    elapsed = time.time() - press_start
                    if elapsed >= LONG_PRESS_THRESHOLD_SECONDS:
                        with self._lock:
                            if not self._shutdown_triggered:
                                self._shutdown_triggered = True
                                self._channel1_press_start = None

                        logger.info(
                            "Long-press detected (%.1f seconds), requesting shutdown",
                            elapsed,
                        )
                        if self._on_shutdown_requested is not None:
                            self._on_shutdown_requested()
                else:
                    # Button was released before threshold
                    with self._lock:
                        self._channel1_press_start = None

            time.sleep(0.1)  # Poll every 100ms

    def _monitor_switch(self) -> None:
        """Monitor selector switch for state changes."""
        while self._running:
            current_state = self._gpio.read(self._config.switch_pin)

            if current_state != self._last_switch_state:
                with self._lock:
                    self._last_switch_state = current_state

                effective_state = (
                    not current_state if self._config.invert_switch else current_state
                )
                position = SwitchPosition.ON if effective_state else SwitchPosition.OFF
                logger.info("Selector switch changed to %s", position.name)
                self._on_switch_change(position)

            time.sleep(0.05)  # Poll every 50ms

    def get_switch_position(self) -> SwitchPosition:
        """Get current switch position.

        Returns:
            Current switch position.
        """
        state = self._gpio.read(self._config.switch_pin)
        effective_state = not state if self._config.invert_switch else state
        return SwitchPosition.ON if effective_state else SwitchPosition.OFF

    def stop(self) -> None:
        """Stop GPIO monitoring and clean up."""
        logger.info("Stopping GPIO controller")
        self._running = False
        if self._switch_monitor_thread is not None:
            self._switch_monitor_thread.join(timeout=1.0)
        if self._long_press_monitor_thread is not None:
            self._long_press_monitor_thread.join(timeout=1.0)
        self._gpio.cleanup()
