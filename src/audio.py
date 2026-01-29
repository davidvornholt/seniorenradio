"""Audio service for Seniorenradio.

MPV-based audio playback for announcements and internet radio streams.
Uses playlist prefetching for seamless transition from announcement to stream.
"""

import contextlib
import logging
import time
from pathlib import Path
from threading import Event, Lock
from typing import Protocol

import mpv

from .models import AudioConfig, ErrorAnnouncementsConfig, RetryConfig

logger = logging.getLogger(__name__)


class AudioPlayer(Protocol):
    """Protocol for audio playback."""

    def play_announcement(self, file: Path) -> bool:
        """Play a local audio file synchronously."""
        ...

    def play_stream(self, url: str) -> bool:
        """Start playing an internet stream."""
        ...

    def play_announcement_with_stream_preload(
        self, announcement_file: Path, stream_url: str
    ) -> bool:
        """Play announcement while preloading stream, then seamlessly start stream."""
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

    def play_goodbye_announcement(self) -> None:
        """Play goodbye announcement when radio is turned off."""
        ...

    def cleanup(self) -> None:
        """Clean up resources."""
        ...


class MpvAudioPlayer:
    """MPV-based audio player using playlist prefetching for seamless playback."""

    def __init__(
        self,
        audio_config: AudioConfig,
        retry_config: RetryConfig,
        error_announcements: ErrorAnnouncementsConfig,
        goodbye_announcement: Path,
    ) -> None:
        """Initialize MPV audio player.

        Args:
            audio_config: Audio output configuration.
            retry_config: Retry settings for stream failures.
            error_announcements: Error announcement audio files.
            goodbye_announcement: Goodbye audio file for switch-off.
        """
        self._audio_config = audio_config
        self._retry_config = retry_config
        self._error_announcements = error_announcements
        self._goodbye_announcement = goodbye_announcement
        self._lock = Lock()
        self._player: mpv.MPV | None = None
        self._is_stream_active = False
        self._stream_started = Event()
        self._playback_error = Event()

    def _create_player(self, prefetch: bool = False) -> mpv.MPV:
        """Create a new MPV player instance.

        Args:
            prefetch: If True, enable playlist prefetching for streaming.

        Returns:
            Configured MPV player instance.
        """
        kwargs = {
            "audio_device": self._audio_config.device,
            "video": False,
            "terminal": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
        }

        if prefetch:
            # Enable prefetching and caching for seamless playback
            kwargs["prefetch_playlist"] = True
            kwargs["cache"] = "yes"
            kwargs["cache_secs"] = 10
            kwargs["demuxer_max_bytes"] = "50MiB"

        player = mpv.MPV(**kwargs)
        player.volume = self._audio_config.volume

        return player

    def _create_standalone_player(self) -> mpv.MPV:
        """Create a standalone player for simple announcements."""
        player = mpv.MPV(
            audio_device=self._audio_config.device,
            video=False,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
        )
        player.volume = self._audio_config.volume
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
            player = self._create_standalone_player()
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
            self._stop_internal()

            for attempt in range(1, self._retry_config.max_attempts + 1):
                logger.info(
                    "Attempting to connect to stream (attempt %d/%d): %s",
                    attempt,
                    self._retry_config.max_attempts,
                    url,
                )

                try:
                    self._player = self._create_player(prefetch=True)
                    self._player.play(url)

                    # Wait for stream to start
                    for _ in range(50):
                        time.sleep(0.1)
                        if self._is_player_playing():
                            playback_time = self._player.playback_time
                            self._is_stream_active = True
                            logger.info(
                                "Stream connected successfully (playback_time: %s)",
                                playback_time,
                            )
                            return True

                    logger.warning("Stream failed to start on attempt %d", attempt)
                    self._stop_internal()

                except Exception as e:
                    logger.exception("MPV error on attempt %d: %s", attempt, e)
                    self._stop_internal()

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

    def _is_player_playing(self) -> bool:
        """Check if the player is connected and playing.

        Returns:
            True if playing.
        """
        if self._player is None:
            return False

        try:
            playback_time = self._player.playback_time
            return playback_time is not None
        except Exception:
            return False

    def play_announcement_with_stream_preload(
        self, announcement_file: Path, stream_url: str
    ) -> bool:
        """Play announcement while preloading stream using playlist prefetching.

        Uses a single MPV player with a playlist containing the announcement
        and stream URL. MPV prefetches the stream while playing the announcement,
        enabling seamless transition.

        Args:
            announcement_file: Path to announcement audio file.
            stream_url: URL of stream to play.

        Returns:
            True if stream is playing after announcement, False otherwise.
        """
        if not announcement_file.exists():
            logger.error("Announcement file not found: %s", announcement_file)
            return self.play_stream(stream_url)

        with self._lock:
            self._stop_internal()
            self._stream_started.clear()
            self._playback_error.clear()

            logger.info(
                "Starting playlist: [%s] -> [%s]",
                announcement_file.name,
                stream_url,
            )

            try:
                # Create player with prefetch enabled
                self._player = self._create_player(prefetch=True)

                # Set up event handlers
                @self._player.property_observer("playlist-pos")
                def on_playlist_pos_change(_name: str, value: int | None) -> None:
                    if value == 1:  # Moved to stream (second playlist item)
                        logger.info("Transitioned to stream")
                        self._stream_started.set()

                @self._player.event_callback("end-file")
                def on_end_file(event: mpv.MpvEvent) -> None:
                    # MpvEvent uses attribute access, not dict-style
                    reason = getattr(event, "reason", None) if event else None
                    if reason == "error":
                        logger.error("Playback error: %s", event)
                        self._playback_error.set()

                # Start with announcement, append stream to playlist
                self._player.play(str(announcement_file))
                self._player.playlist_append(stream_url)

                logger.info("Announcement playing, stream prefetching...")

            except Exception as e:
                logger.exception("Failed to start playlist playback: %s", e)
                self._stop_internal()
                return False

        # Wait for transition to stream (outside lock to allow callbacks)
        # Typical announcement is 3-5 seconds, add some buffer
        if self._stream_started.wait(timeout=15.0):
            if self._playback_error.is_set():
                logger.warning("Stream error during prefetch, retrying normally")
                return self.play_stream(stream_url)

            # Poll for stream to start playing (up to 3 seconds)
            # Short announcements may not give enough prefetch time
            for i in range(30):
                time.sleep(0.1)
                with self._lock:
                    if self._is_player_playing():
                        self._is_stream_active = True
                        logger.info(
                            "Stream playing after seamless transition (waited %.1fs)",
                            (i + 1) * 0.3,
                        )
                        return True

            logger.warning("Stream not playing after transition, retrying")
            return self.play_stream(stream_url)

        logger.warning("Timeout waiting for stream transition, retrying normally")
        return self.play_stream(stream_url)

    def play_retrying_announcement(self) -> None:
        """Play announcement that system is retrying connection."""
        self.play_announcement(self._error_announcements.retrying)

    def play_failed_announcement(self) -> None:
        """Play announcement that connection has failed permanently."""
        self.play_announcement(self._error_announcements.failed)

    def play_no_internet_announcement(self) -> None:
        """Play announcement that there is no internet connection."""
        self.play_announcement(self._error_announcements.no_internet)

    def play_goodbye_announcement(self) -> None:
        """Play goodbye announcement when radio is turned off."""
        self.play_announcement(self._goodbye_announcement)

    def _stop_internal(self) -> None:
        """Stop playback without acquiring lock (internal use)."""
        if self._player is not None:
            with contextlib.suppress(mpv.ShutdownError, Exception):
                self._player.terminate()
            self._player = None
        self._is_stream_active = False

    def stop(self) -> None:
        """Stop all playback."""
        with self._lock:
            self._stop_internal()

    def is_playing(self) -> bool:
        """Check if stream is currently playing.

        Returns:
            True if stream is active.
        """
        return self._is_stream_active

    def cleanup(self) -> None:
        """Clean up all resources."""
        self.stop()
