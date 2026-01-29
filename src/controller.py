"""Main application controller for Seniorenradio.

Implements the state machine for radio behavior.
"""

import logging
from threading import Lock

from .audio import AudioPlayer
from .models import AppConfig, Channel, RadioState, SwitchPosition

logger = logging.getLogger(__name__)


class RadioController:
    """Main controller implementing radio state machine."""

    def __init__(
        self,
        config: AppConfig,
        audio_player: AudioPlayer,
    ) -> None:
        """Initialize radio controller.

        Args:
            config: Application configuration.
            audio_player: Audio player instance.
        """
        self._config = config
        self._audio = audio_player
        self._lock = Lock()
        self._state = RadioState(
            selected_channel_index=config.default_channel_index,
            switch_position=SwitchPosition.OFF,
            is_stream_active=False,
        )

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

    def _announce_and_play_channel(self, channel: Channel) -> bool:
        """Announce channel name and start stream.

        Args:
            channel: Channel to play.

        Returns:
            True if stream started successfully.
        """
        logger.info("Announcing channel: %s", channel.name)
        self._audio.play_announcement(channel.announcement_file)

        logger.info("Starting stream: %s", channel.stream_url)
        success = self._audio.play_stream(channel.stream_url)

        if not success:
            logger.error("Failed to start stream for channel: %s", channel.name)
            self._audio.play_failed_announcement()

        return success

    def handle_startup(self, initial_switch_position: SwitchPosition) -> None:
        """Handle application startup.

        Args:
            initial_switch_position: Initial position of the selector switch.
        """
        with self._lock:
            self._state = self._state.with_switch(initial_switch_position)

            if initial_switch_position == SwitchPosition.ON:
                channel = self._get_channel(self._state.selected_channel_index)
                if channel is not None:
                    logger.info("Startup with switch ON, playing default channel")
                    success = self._announce_and_play_channel(channel)
                    self._state = self._state.with_stream_active(success)

    def handle_channel_button(self, channel_index: int) -> None:
        """Handle channel button press.

        Args:
            channel_index: Index of the channel button pressed.
        """
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

            # Stop current stream
            self._audio.stop()

            # Update selected channel
            self._state = self._state.with_channel(channel_index)

            # Announce and play new channel
            success = self._announce_and_play_channel(channel)
            self._state = self._state.with_stream_active(success)

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

            match position:
                case SwitchPosition.ON:
                    channel = self._get_channel(self._state.selected_channel_index)
                    if channel is not None:
                        logger.info("Switch turned ON, starting playback")
                        success = self._announce_and_play_channel(channel)
                        self._state = self._state.with_stream_active(success)

                case SwitchPosition.OFF:
                    logger.info("Switch turned OFF, stopping playback")
                    self._audio.stop()
                    self._state = self._state.with_stream_active(False)

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        logger.info("Shutting down radio controller")
        self._audio.stop()
        self._audio.cleanup()
