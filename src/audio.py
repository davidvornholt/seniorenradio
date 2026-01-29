"""Audio service for Seniorenradio.

MPV-based audio playback for announcements and internet radio streams.
"""

import contextlib
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Protocol

import mpv

from models import AudioConfig, ErrorAnnouncementsConfig, RetryConfig

logger = logging.getLogger(__name__)


class AudioPlayer(Protocol):
    """Protocol for audio playback."""

    def play_announcement(self, file: Path) -> bool:
        """Play a local audio file synchronously."""
        ...

    def play_stream(self, url: str) -> bool:
        """Start playing an internet stream."""
        ...

    def play_retrying_announcement(self) -> None:
        """Play announcement that system is retrying connection."""
        ...

    def play_failed_announcement(self) -> None:
        """Play announcement that connection has failed permanently."""
        ...

    def play_no_internet_announcement(self) -> None:
        """Play announcement that there is no internet connection."""
        ...

    def stop(self) -> None:
        """Stop all playback."""
        ...

    def is_playing(self) -> bool:
        """Check if stream is currently playing."""
        ...

    def cleanup(self) -> None:
        """Clean up resources."""
        ...


class MpvAudioPlayer:
    """MPV-based audio player implementation."""

    def __init__(
        self,
        audio_config: AudioConfig,
        retry_config: RetryConfig,
        error_announcements: ErrorAnnouncementsConfig,
    ) -> None:
        """Initialize MPV audio player.

        Args:
            audio_config: Audio output configuration.
            retry_config: Retry settings for stream failures.
            error_announcements: Error announcement audio files.
        """
        self._audio_config = audio_config
        self._retry_config = retry_config
        self._error_announcements = error_announcements
        self._lock = Lock()
        self._stream_player: mpv.MPV | None = None
        self._is_stream_active = False

    def _create_player(self, for_stream: bool = False) -> mpv.MPV:
        """Create a new MPV player instance.

        Args:
            for_stream: If True, configure for streaming.

        Returns:
            Configured MPV player instance.
        """
        player = mpv.MPV(
            audio_device=self._audio_config.device,
            video=False,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
        )
        player.volume = self._audio_config.volume

        if for_stream:
            player.cache = "yes"
            player.cache_secs = 10
            player.demuxer_max_bytes = "50MiB"

        return player

    def play_announcement(self, file: Path) -> bool:
        """Play a local audio file synchronously.

        Args:
            file: Path to the audio file.

        Returns:
            True if playback succeeded, False otherwise.
        """
        if not file.exists():
            logger.error("Announcement file not found: %s", file)
            return False

        with self._lock:
            return self._play_announcement_internal(file)

    def _play_announcement_internal(self, file: Path) -> bool:
        """Play announcement without acquiring lock (for internal use).

        Args:
            file: Path to the audio file.

        Returns:
            True if playback succeeded, False otherwise.
        """
        if not file.exists():
            logger.error("Announcement file not found: %s", file)
            return False

        try:
            player = self._create_player(for_stream=False)
            player.play(str(file))
            player.wait_for_playback()
            player.terminate()
            return True
        except Exception as e:
            logger.exception("Failed to play announcement: %s", e)
            return False

    def play_stream(self, url: str) -> bool:
        """Start playing an internet stream with retry logic.

        Args:
            url: Stream URL.

        Returns:
            True if stream started successfully, False otherwise.
        """
        with self._lock:
            self._stop_stream_internal()

            for attempt in range(1, self._retry_config.max_attempts + 1):
                logger.info(
                    "Attempting to connect to stream (attempt %d/%d): %s",
                    attempt,
                    self._retry_config.max_attempts,
                    url,
                )

                try:
                    self._stream_player = self._create_player(for_stream=True)
                    self._stream_player.play(url)

                    # Wait briefly to check if stream connects
                    time.sleep(2)

                    if self._stream_player.core_idle is False:
                        self._is_stream_active = True
                        logger.info("Stream connected successfully")
                        return True

                    logger.warning("Stream failed to start on attempt %d", attempt)
                    self._stop_stream_internal()

                except Exception as e:
                    logger.exception("MPV error on attempt %d: %s", attempt, e)
                    self._stop_stream_internal()

                # Play retry announcement if we will retry
                if attempt < self._retry_config.max_attempts:
                    logger.info("Playing retry announcement before next attempt")
                    self._play_announcement_internal(self._error_announcements.retrying)
                    time.sleep(self._retry_config.delay_seconds)

            logger.error(
                "Failed to connect to stream after %d attempts",
                self._retry_config.max_attempts,
            )
            return False

    def play_retrying_announcement(self) -> None:
        """Play announcement that system is retrying connection."""
        self.play_announcement(self._error_announcements.retrying)

    def play_failed_announcement(self) -> None:
        """Play announcement that connection has failed permanently."""
        self.play_announcement(self._error_announcements.failed)

    def play_no_internet_announcement(self) -> None:
        """Play announcement that there is no internet connection."""
        self.play_announcement(self._error_announcements.no_internet)

    def _stop_stream_internal(self) -> None:
        """Stop stream without acquiring lock (internal use)."""
        if self._stream_player is not None:
            with contextlib.suppress(mpv.ShutdownError, Exception):
                self._stream_player.terminate()
            self._stream_player = None
        self._is_stream_active = False

    def stop(self) -> None:
        """Stop all playback."""
        with self._lock:
            self._stop_stream_internal()

    def is_playing(self) -> bool:
        """Check if stream is currently playing.

        Returns:
            True if stream is active.
        """
        return self._is_stream_active

    def cleanup(self) -> None:
        """Clean up all resources."""
        self.stop()
