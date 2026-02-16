"""Keyboard-based GPIO mock for local testing."""

from __future__ import annotations

import logging
import select
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable

logger = logging.getLogger(__name__)

type TermiosSettings = list[int | list[int | bytes]]


class KeyboardGpioAdapter:
    """GPIO adapter that maps keyboard input to GPIO events."""

    _HOLD_WINDOW_SECONDS = 1.0

    def __init__(self, channel_pins: tuple[int, ...], switch_pin: int) -> None:
        self._pin_state: dict[int, bool] = {}
        self._pull_up: dict[int, bool] = {}
        self._callbacks: dict[int, Callable[[int], None]] = {}
        self._edges: dict[int, str] = {}
        self._bouncetime_ms: dict[int, int] = {}
        self._last_event_time: dict[int, float] = {}
        self._channel_pins = channel_pins
        self._switch_pin = switch_pin
        self._button_pins: set[int] = set(channel_pins)
        self._press_until: dict[int, float] = {}
        self._lock = threading.Lock()
        self._running = True
        self._stdin_fd: int | None = None
        self._term_settings: TermiosSettings | None = None
        self._term_restored = False
        self._cleaned_up = False

        if sys.stdin.isatty():
            self._stdin_fd = sys.stdin.fileno()
            self._term_settings = termios.tcgetattr(self._stdin_fd)
            tty.setcbreak(self._stdin_fd)
        else:
            logger.warning("GPIO mock requires a TTY for keyboard input")

        self._thread = threading.Thread(target=self._keyboard_loop, daemon=True)
        self._thread.start()

        logger.info(
            "GPIO mock mode enabled (keys: 1-%d = channels, s = toggle switch)",
            len(self._channel_pins),
        )

    def setup_input(self, pin: int, pull_up: bool) -> None:
        """Configure a pin as input."""
        with self._lock:
            if pin not in self._pin_state:
                self._pin_state[pin] = bool(pull_up)
            self._pull_up[pin] = pull_up

    def read(self, pin: int) -> bool:
        """Read pin state (True = HIGH)."""
        now = time.monotonic()
        callback: Callable[[int], None] | None = None
        with self._lock:
            until = self._press_until.get(pin)
            if until is not None:
                if now < until:
                    return False
                self._press_until.pop(pin, None)
                if pin in self._button_pins:
                    old_state = self._pin_state.get(pin, True)
                    new_state = self._pull_up.get(pin, True)
                    self._pin_state[pin] = new_state
                    callback = self._get_callback_for_transition(
                        pin,
                        old_state,
                        new_state,
                        now,
                    )
            state = self._pin_state.get(pin, True)

        if callback is not None:
            callback(pin)

        return state

    def add_event_detect(
        self,
        pin: int,
        edge: str,
        callback: Callable[[int], None],
        bouncetime: int,
    ) -> None:
        """Add edge detection with callback."""
        edge_name = edge.upper()
        if edge_name not in {"RISING", "FALLING", "BOTH"}:
            edge_name = "BOTH"
        with self._lock:
            self._callbacks[pin] = callback
            self._edges[pin] = edge_name
            self._bouncetime_ms[pin] = max(0, bouncetime)
            self._button_pins.add(pin)

    def cleanup(self) -> None:
        """Clean up GPIO resources."""
        if self._cleaned_up:
            return
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if (
            self._stdin_fd is not None
            and self._term_settings is not None
            and not self._term_restored
        ):
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._term_settings)
            self._term_restored = True
        self._stdin_fd = None
        self._term_settings = None
        self._cleaned_up = True

    def _keyboard_loop(self) -> None:
        while self._running:
            ch = self._read_char(timeout=0.1)
            if ch is None:
                continue

            if ch.isdigit():
                channel_number = int(ch)
                if 1 <= channel_number <= len(self._channel_pins):
                    channel_index = channel_number - 1
                    self._handle_channel_key(channel_index)
                continue

            if ch.lower() == "s":
                self._toggle_switch()

    def _read_char(self, timeout: float) -> str | None:
        if self._stdin_fd is None:
            time.sleep(timeout)
            return None

        ready, _, _ = select.select([self._stdin_fd], [], [], timeout)
        if not ready:
            return None
        data = sys.stdin.read(1)
        return data if data else None

    def _handle_channel_key(self, channel_index: int) -> None:
        with self._lock:
            pin = None
            if channel_index < len(self._channel_pins):
                pin = self._channel_pins[channel_index]
        if pin is None:
            return

        self._press_button(pin)

    def _press_button(self, pin: int) -> None:
        now = time.monotonic()
        callback: Callable[[int], None] | None = None
        with self._lock:
            until = self._press_until.get(pin)
            if until is None or now >= until:
                old_state = self._pin_state.get(pin, True)
                self._pin_state[pin] = False
                callback = self._get_callback_for_transition(
                    pin,
                    old_state,
                    False,
                    now,
                )
            self._press_until[pin] = now + self._HOLD_WINDOW_SECONDS

        if callback is not None:
            callback(pin)

    def _toggle_switch(self) -> None:
        callback: Callable[[int], None] | None = None
        with self._lock:
            current = self._pin_state.get(self._switch_pin, True)
            new_state = not current
            self._pin_state[self._switch_pin] = new_state
            callback = self._get_callback_for_transition(
                self._switch_pin,
                current,
                new_state,
                time.monotonic(),
            )

        if callback is not None:
            callback(self._switch_pin)

    def _get_callback_for_transition(
        self,
        pin: int,
        old_state: bool,
        new_state: bool,
        now: float,
    ) -> Callable[[int], None] | None:
        if old_state == new_state:
            return None
        edge = self._edges.get(pin, "BOTH")
        if edge == "RISING" and not (not old_state and new_state):
            return None
        if edge == "FALLING" and not (old_state and not new_state):
            return None
        callback = self._callbacks.get(pin)
        if callback is None:
            return None
        bouncetime_ms = self._bouncetime_ms.get(pin, 0)
        last_time = self._last_event_time.get(pin)
        if last_time is not None and (now - last_time) * 1000 < bouncetime_ms:
            return None
        self._last_event_time[pin] = now
        return callback
