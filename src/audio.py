"""Audio service for Seniorenradio.

MPV-based audio playback for announcements and internet radio streams.
"""

import contextlib
import logging
import time
from pathlib import Path
from threading import Lock, Thread
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
        """Play announcement while preloading stream, then unmute stream."""
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
    """MPV-based audio player implementation."""

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

                    # Wait for stream to start buffering/playing
                    # Check multiple times over a few seconds
                    for _ in range(10):
                        time.sleep(0.5)
                        if self._is_stream_ready():
                            playback_time = self._stream_player.playback_time
                            self._is_stream_active = True
                            logger.info(
                                "Stream connected successfully (playback_time: %s)",
                                playback_time,
                            )
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

    def _is_stream_ready(self) -> bool:
        """Check if the stream player has buffered enough data to play.

        Uses buffer-based detection which works even when muted (volume=0).
        The previous core_idle check failed because muted streams don't
        actively decode audio.

        Returns:
            True if the stream has buffered and is ready to play.
        """
        if self._stream_player is None:
            return False

        try:
            # Check if we have playback time > 0 (stream is actively playing)
            playback_time = self._stream_player.playback_time
            if playback_time is not None and playback_time > 0:
                return True

            # Check time-pos (current position in stream)
            time_pos = self._stream_player.time_pos
            if time_pos is not None and time_pos > 0:
                return True

            # Check demuxer cache - how many seconds are buffered
            # This works even when muted/volume=0
            # We need at least 0.5 seconds buffered to consider it ready
            cache_time = self._stream_player.demuxer_cache_time
            return cache_time is not None and cache_time > 0.5
        except Exception:
            return False

    def play_announcement_with_stream_preload(
        self, announcement_file: Path, stream_url: str
    ) -> bool:
        """Play announcement while preloading stream in background.

        Starts the stream FIRST to begin buffering, then plays announcement
        while the stream buffers. This ensures maximum buffering time.

        Args:
            announcement_file: Path to announcement audio file.
            stream_url: URL of stream to preload.

        Returns:
            True if stream is playing after announcement, False otherwise.
        """
        # Start the stream (muted) to begin network connection and buffering
        with self._lock:
            self._stop_stream_internal()

            # Start stream with mute for prebuffering
            # Using mute=True instead of volume=0 ensures MPV still decodes/buffers
            logger.info("Preloading stream (muted): %s", stream_url)
            self._stream_player = self._create_player(for_stream=True)
            self._stream_player.mute = True
            self._stream_player.play(stream_url)

        # Start announcement immediately (stream buffers in parallel)
        logger.info("Playing announcement: %s", announcement_file.name)
        announcement_thread = Thread(
            target=self._play_announcement_internal,
            args=(announcement_file,),
            daemon=True,
        )
        announcement_thread.start()

        # Wait for announcement to finish
        # Stream is buffering in the background during this time
        announcement_thread.join()
        logger.info("Announcement finished, stream should be buffered")

        with self._lock:
            # Check if stream player still exists
            if self._stream_player is None:
                logger.error("Stream player was terminated during announcement")
                return False

            # Log stream state for diagnostics
            try:
                cache_time = self._stream_player.demuxer_cache_time
                playback_time = self._stream_player.playback_time
                logger.info(
                    "Stream state: cache_time=%s, playback_time=%s",
                    cache_time,
                    playback_time,
                )
            except Exception:
                logger.warning("Could not read stream state")

            # Unmute - the stream should have buffered during announcement
            logger.info("Unmuting stream")
            self._stream_player.mute = False

            # Quick check if stream is already playing
            if self._is_stream_ready():
                playback_time = self._stream_player.playback_time
                logger.info("Stream ready (playback_time: %s)", playback_time)
                self._is_stream_active = True
                return True

            # Brief verification - stream may need a moment to start audio output
            # Keep this short for snappy feel (max 1.5 seconds)
            for _ in range(5):
                time.sleep(0.3)
                if self._is_stream_ready():
                    playback_time = self._stream_player.playback_time
                    self._is_stream_active = True
                    logger.info("Stream connected (playback_time: %s)", playback_time)
                    return True

            logger.warning("Stream did not start during preload, trying normal connect")
            self._stop_stream_internal()

        # Fall back to normal stream connection with retries
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
