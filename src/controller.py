"""Main application controller for Seniorenradio.

Implements the state machine for radio behavior with a worker thread
for non-blocking audio operations.
"""

import logging
import time
from collections.abc import Callable
from functools import partial
from threading import Event, Lock, Thread
from typing import Protocol

from .audio import AudioPlayer
from .models import AppConfig, Channel, RadioState, SwitchPosition
from .network import NetworkManager, SavedWifiNetwork
from .tts import TtsSpeaker

logger = logging.getLogger(__name__)


class ShutdownRequester(Protocol):
    """Protocol for requesting application shutdown."""

    def request_shutdown(self) -> None:
        """Request graceful application shutdown."""
        ...


class RadioController:
    """Main controller implementing radio state machine.

    All long-running audio operations are dispatched to a background
    worker thread so GPIO callbacks return immediately.
    """

    def __init__(
        self,
        config: AppConfig,
        audio_player: AudioPlayer,
        network_manager: NetworkManager,
        tts_speaker: TtsSpeaker,
    ) -> None:
        """Initialize radio controller.

        Args:
            config: Application configuration.
            audio_player: Audio player instance.
            network_manager: Network management instance.
            tts_speaker: TTS speaker instance.
        """
        self._config = config
        self._audio = audio_player
        self._network = network_manager
        self._tts = tts_speaker
        self._lock = Lock()
        self._state = RadioState(
            selected_channel_index=config.default_channel_index,
            switch_position=SwitchPosition.OFF,
            is_stream_active=False,
        )
        self._wifi_selection_deadline: float | None = None
        self._wifi_selection_networks: tuple[SavedWifiNetwork, ...] = ()
        self._worker_thread: Thread | None = None
        self._worker_cancel = Event()

    @property
    def state(self) -> RadioState:
        """Get current radio state."""
        return self._state

    def _get_channel(self, index: int) -> Channel | None:
        """Get channel by index.

        Args:
            index: Channel index.

        Returns:
            Channel if found, None otherwise.
        """
        if 0 <= index < len(self._config.channels):
            return self._config.channels[index]
        return None

    def _dispatch(self, task: Callable[[], None]) -> None:
        """Cancel current work and dispatch a new audio task to the worker.

        Args:
            task: Callable to run on the worker thread.
        """
        # Signal current operation to stop
        self._worker_cancel.set()
        self._audio.stop()

        # Wait briefly for previous worker to notice cancellation
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        self._worker_cancel.clear()
        self._worker_thread = Thread(target=task, daemon=True, name="radio-worker")
        self._worker_thread.start()

    def _play_channel_task(self, channel: Channel) -> None:
        """Worker task: announce and start playing a channel.

        Args:
            channel: Channel to play.
        """
        logger.info("Announcing channel: %s", channel.name)

        success = self._audio.play_announcement_with_stream_preload(
            channel.announcement_file,
            channel.stream_url,
        )

        if self._worker_cancel.is_set():
            return

        with self._lock:
            self._state = self._state.with_stream_active(success)

        if not success:
            logger.error("Failed to start stream for channel: %s", channel.name)
            self._audio.play_failed_announcement()

    def handle_startup(self, initial_switch_position: SwitchPosition) -> None:
        """Handle application startup.

        Args:
            initial_switch_position: Initial position of the selector switch.
        """
        with self._lock:
            self._state = self._state.with_switch(initial_switch_position)

        self._announce_boot_connectivity()

        with self._lock:
            position = self._state.switch_position
            channel_index = self._state.selected_channel_index

        match position:
            case SwitchPosition.ON:
                channel = self._get_channel(channel_index)
                if channel is not None:
                    logger.info("Startup with switch ON, playing default channel")
                    self._dispatch(partial(self._play_channel_task, channel))

            case SwitchPosition.OFF:
                logger.info("Startup with switch OFF, playing info announcement")
                self._audio.play_selector_off_announcement()

    def handle_channel_button(self, channel_index: int) -> None:
        """Handle channel button press.

        Args:
            channel_index: Index of the channel button pressed.
        """
        now = time.monotonic()
        selection = self._take_wifi_selection(channel_index, now)
        if selection is not None:
            self._handle_wifi_selection(selection)
            return

        with self._lock:
            # Ignore if switch is OFF
            if self._state.switch_position == SwitchPosition.OFF:
                logger.debug("Ignoring channel button - switch is OFF")
                return

            # Ignore if same channel already playing
            if (
                self._state.selected_channel_index == channel_index
                and self._state.is_stream_active
            ):
                logger.debug("Ignoring channel button - already playing this channel")
                return

            channel = self._get_channel(channel_index)
            if channel is None:
                logger.warning("Invalid channel index: %d", channel_index)
                return

            # Update selected channel immediately
            self._state = self._state.with_channel(channel_index)

        # Dispatch long audio operation to worker thread (outside lock)
        logger.info(
            "Channel %d button pressed, dispatching playback", channel_index + 1
        )
        self._dispatch(partial(self._play_channel_task, channel))

    def handle_switch_change(self, position: SwitchPosition) -> None:
        """Handle selector switch position change.

        Args:
            position: New switch position.
        """
        with self._lock:
            previous_position = self._state.switch_position
            self._state = self._state.with_switch(position)

            # Ignore if position hasn't actually changed
            if position == previous_position:
                return

            channel_index = self._state.selected_channel_index

        # Dispatch audio operations outside lock
        match position:
            case SwitchPosition.ON:
                channel = self._get_channel(channel_index)
                if channel is not None:
                    logger.info("Switch turned ON, starting playback")
                    self._dispatch(partial(self._play_channel_task, channel))

            case SwitchPosition.OFF:
                logger.info("Switch turned OFF, stopping playback")
                self._dispatch(self._switch_off_task)

    def _switch_off_task(self) -> None:
        """Worker task: stop playback and play goodbye announcement."""
        self._audio.stop()
        self._audio.play_goodbye_announcement()
        with self._lock:
            self._state = self._state.with_stream_active(False)

    def handle_shutdown_request(self) -> None:
        """Handle shutdown request from long-press on channel 1 button.

        Stops current playback and plays shutdown announcement.
        """
        # Cancel any current worker operations
        self._worker_cancel.set()
        self._audio.stop()

        logger.info("Shutdown requested via long-press")
        self._audio.play_shutdown_announcement()

    def handle_debug_request(self) -> None:
        """Handle debug request from long-press on channel 2 button."""
        if not self._config.debug.enabled:
            return

        with self._lock:
            was_playing = self._state.is_stream_active
            channel = self._get_channel(self._state.selected_channel_index)

            if self._config.debug.interrupt_audio and was_playing:
                self._audio.stop()
                self._state = self._state.with_stream_active(False)

        lines = self._build_debug_lines()
        self._tts.speak_lines(lines)

        selection_networks = self._prepare_wifi_selection()
        if selection_networks:
            self._tts.speak_lines(self._build_wifi_selection_prompt(selection_networks))

        if was_playing and channel is not None:
            with self._lock:
                live_switch_position = self._state.switch_position
            if live_switch_position == SwitchPosition.ON:
                success = self._audio.play_stream(channel.stream_url)
                with self._lock:
                    if self._state.switch_position == SwitchPosition.ON:
                        self._state = self._state.with_stream_active(success)

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        logger.info("Shutting down radio controller")
        self._worker_cancel.set()
        self._audio.stop()

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)

        self._audio.cleanup()

    def _announce_boot_connectivity(self) -> None:
        status = self._network.check_connectivity()
        announcement = (
            self._config.boot_announcements.connected
            if status.is_connected
            else self._config.boot_announcements.no_internet
        )
        self._audio.play_announcement(announcement)

    def _build_debug_lines(self) -> tuple[str, ...]:
        status = self._network.check_connectivity()
        active_wifi_result = self._network.get_active_wifi()
        saved_result = self._network.list_saved_wifi()

        base_lines = ["Debug info."]
        connectivity_line = (
            "Internet connected." if status.is_connected else "No internet connection."
        )
        base_lines.append(connectivity_line)

        if active_wifi_result.is_ok():
            active_wifi = active_wifi_result.value
            if active_wifi is None:
                base_lines.append("WiFi not connected.")
            else:
                signal_line = (
                    f"WiFi signal {active_wifi.signal} percent."
                    if active_wifi.signal is not None
                    else "WiFi signal unknown."
                )
                base_lines.extend(
                    [
                        f"WiFi SSID {active_wifi.ssid or 'unknown'}.",
                        f"Device {active_wifi.device or 'unknown'}.",
                        signal_line,
                    ]
                )
                if active_wifi.ipv4_address:
                    base_lines.append(f"IP address {active_wifi.ipv4_address}.")
        else:
            base_lines.append("WiFi info unavailable.")

        if saved_result.is_ok() and saved_result.value:
            networks = saved_result.value
            total = len(networks)
            base_lines.append(f"Saved networks {total}.")
            max_items = self._config.debug.max_networks
            limited = networks[:max_items]
            entries = tuple(
                f"{index + 1}. {net.name or 'unknown'}, {net.security}."
                for index, net in enumerate(limited)
            )
            base_lines.extend(entries)
            if total > max_items:
                remaining = total - max_items
                base_lines.append(f"And {remaining} more.")
        elif saved_result.is_ok():
            base_lines.append("No saved WiFi networks.")
        else:
            base_lines.append("Saved WiFi list unavailable.")

        return tuple(base_lines)

    def _prepare_wifi_selection(self) -> tuple[SavedWifiNetwork, ...]:
        if not self._config.debug.enabled:
            return ()

        saved_result = self._network.list_saved_wifi()
        if not saved_result.is_ok() or not saved_result.value:
            return ()

        max_items = min(
            self._config.debug.max_networks,
            len(self._config.channels),
        )
        selection = saved_result.value[:max_items]

        if not selection:
            return ()

        deadline = time.monotonic() + self._config.debug.selection_timeout_seconds
        with self._lock:
            self._wifi_selection_deadline = deadline
            self._wifi_selection_networks = selection

        return selection

    def _build_wifi_selection_prompt(
        self, selection: tuple[SavedWifiNetwork, ...]
    ) -> tuple[str, ...]:
        if not selection:
            return ()

        prompt = ("Press a channel button to connect:",)
        entries = tuple(
            f"Channel {index + 1} for {network.name or network.ssid or 'unknown'}."
            for index, network in enumerate(selection)
        )
        return (*prompt, *entries)

    def _take_wifi_selection(
        self, channel_index: int, now: float
    ) -> SavedWifiNetwork | None:
        with self._lock:
            deadline = self._wifi_selection_deadline
            selection = self._wifi_selection_networks

            if deadline is None or now > deadline:
                self._wifi_selection_deadline = None
                self._wifi_selection_networks = ()
                return None

            if channel_index >= len(selection):
                return None

            chosen = selection[channel_index]
            self._wifi_selection_deadline = None
            self._wifi_selection_networks = ()
            return chosen

    def _handle_wifi_selection(self, network: SavedWifiNetwork) -> None:
        if not network.name:
            self._tts.speak("WiFi connection name missing.")
            return

        self._tts.speak(f"Connecting to {network.name}")
        result = self._network.connect_to_saved_wifi(network.name)
        if result.is_ok():
            self._tts.speak("WiFi connected.")
        else:
            self._tts.speak("WiFi connection failed.")
