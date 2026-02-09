"""Audio service for Seniorenradio.

MPV-based audio playback for announcements and internet radio streams.
Uses playlist prefetching for seamless transition from announcement to stream.
"""

import contextlib
import logging
import socket
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol

import mpv

from .models import (
    AudioConfig,
    ErrorAnnouncementsConfig,
    RetryConfig,
    StreamWatchdogConfig,
)

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

    def play_selector_off_announcement(self) -> None:
        """Play announcement when radio starts with selector switch off."""
        ...

    def play_shutdown_announcement(self) -> None:
        """Play announcement before system shutdown."""
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
        watchdog_config: StreamWatchdogConfig,
        error_announcements: ErrorAnnouncementsConfig,
        goodbye_announcement: Path,
        selector_off_announcement: Path,
        shutdown_announcement: Path,
    ) -> None:
        """Initialize MPV audio player.

        Args:
            audio_config: Audio output configuration.
            retry_config: Retry settings for stream failures.
            watchdog_config: Watchdog settings for stream dropouts.
            error_announcements: Error announcement audio files.
            goodbye_announcement: Goodbye audio file for switch-off.
            selector_off_announcement: Audio file for when starting with switch off.
            shutdown_announcement: Audio file for system shutdown.
        """
        self._audio_config = audio_config
        self._retry_config = retry_config
        self._watchdog_config = watchdog_config
        self._error_announcements = error_announcements
        self._goodbye_announcement = goodbye_announcement
        self._selector_off_announcement = selector_off_announcement
        self._shutdown_announcement = shutdown_announcement
        self._lock = Lock()
        self._player: mpv.MPV | None = None
        self._is_stream_active = False
        self._stream_started = Event()
        self._playback_error = Event()
        self._playback_error_reason: str | None = None
        self._current_stream_url: str | None = None
        self._desired_stream_url: str | None = None
        self._last_reconnect_attempt = 0.0
        self._watchdog_stop = Event()
        self._watchdog_thread: Thread | None = None

    def _create_player(self, prefetch: bool = False) -> mpv.MPV:
        """Create a new MPV player instance.

        Args:
            prefetch: If True, enable playlist prefetching for streaming.

        Returns:
            Configured MPV player instance.
        """
        kwargs: dict[str, str | bool | int | float] = {
            "video": False,
            "terminal": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "ao": self._audio_config.backend,
        }

        # Only set explicit audio device if not "default"
        if self._audio_config.device != "default":
            kwargs["audio_device"] = (
                f"{self._audio_config.backend}/{self._audio_config.device}"
            )

        buffer_config = self._audio_config.buffer
        if buffer_config.enabled:
            kwargs["cache"] = "yes"
            kwargs["cache_secs"] = buffer_config.cache_seconds
            kwargs["demuxer_max_bytes"] = buffer_config.demuxer_max_bytes
            kwargs["network_timeout"] = int(buffer_config.network_timeout_seconds)

        if prefetch:
            kwargs["prefetch_playlist"] = True

        player = mpv.MPV(**kwargs)
        player.volume = self._audio_config.volume
        self._attach_stream_event_handlers(player)

        return player

    def _create_standalone_player(self) -> mpv.MPV:
        """Create a standalone player for simple announcements."""
        kwargs: dict[str, str | bool] = {
            "video": False,
            "terminal": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "ao": self._audio_config.backend,
        }

        # Only set explicit audio device if not "default"
        if self._audio_config.device != "default":
            kwargs["audio_device"] = (
                f"{self._audio_config.backend}/{self._audio_config.device}"
            )

        player = mpv.MPV(**kwargs)
        player.volume = self._audio_config.volume
        return player

    def _attach_stream_event_handlers(self, player: mpv.MPV) -> None:
        """Attach MPV event handlers for stream error detection."""

        @player.event_callback("end-file")
        def on_end_file(event: mpv.MpvEvent) -> None:
            reason = getattr(event, "reason", None) if event else None
            if reason in {"error", "eof"}:
                logger.warning("MPV end-file event: %s", reason)
                self._playback_error_reason = reason
                self._playback_error.set()

    def _start_watchdog(self) -> None:
        if not self._watchdog_config.enabled:
            return

        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return

        self._watchdog_stop.clear()
        self._watchdog_thread = Thread(
            target=self._watchdog_loop,
            name="stream-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1.0)
        self._watchdog_thread = None

    def _has_internet(self) -> bool:
        if not self._watchdog_config.internet_check_enabled:
            return True
        for host in self._watchdog_config.internet_check_hosts:
            try:
                with socket.create_connection(
                    (host, self._watchdog_config.internet_check_port),
                    timeout=self._watchdog_config.internet_check_timeout_seconds,
                ):
                    return True
            except OSError:
                continue
        return False

    def _watchdog_loop(self) -> None:
        last_playback_time: float | None = None
        last_progress_time = time.monotonic()

        while not self._watchdog_stop.wait(
            self._watchdog_config.check_interval_seconds
        ):
            with self._lock:
                stream_active = self._is_stream_active
                player = self._player
                current_url = self._current_stream_url
                desired_url = self._desired_stream_url
                playback_error = self._playback_error.is_set()

            if not stream_active or player is None or current_url is None:
                last_playback_time = None
                last_progress_time = time.monotonic()

                if desired_url is not None:
                    now = time.monotonic()
                    if (
                        now - self._last_reconnect_attempt
                        >= self._watchdog_config.reconnect_delay_seconds
                    ):
                        self._last_reconnect_attempt = now
                        logger.info("Stream inactive, attempting reconnect")
                        self._handle_stream_stall(desired_url)
                continue

            stalled = False
            if playback_error:
                stalled = True
            else:
                try:
                    playback_time = player.playback_time
                except Exception:
                    playback_time = None

                if playback_time is None:
                    stalled = (
                        time.monotonic() - last_progress_time
                    ) >= self._watchdog_config.stall_seconds
                else:
                    if (
                        last_playback_time is None
                        or playback_time > last_playback_time + 0.25
                    ):
                        last_playback_time = playback_time
                        last_progress_time = time.monotonic()
                    else:
                        stalled = (
                            time.monotonic() - last_progress_time
                        ) >= self._watchdog_config.stall_seconds

            if stalled:
                logger.warning("Stream stall detected, attempting reconnect")
                self._playback_error.clear()
                self._playback_error_reason = None
                self._handle_stream_stall(current_url)

    def _handle_stream_stall(self, stream_url: str) -> None:
        self._stop_stream_for_reconnect()

        if not self._has_internet():
            logger.warning("No internet connection detected")
            self.play_no_internet_announcement()
            self._watchdog_stop.wait(self._watchdog_config.reconnect_delay_seconds)
            return

        self.play_retrying_announcement()
        success = self._play_stream(stream_url, announce_retry=False)
        if not success:
            self.play_failed_announcement()

    def _stop_stream_for_reconnect(self) -> None:
        """Stop current stream without clearing desired stream state."""
        with self._lock:
            if self._player is not None:
                with contextlib.suppress(mpv.ShutdownError, Exception):
                    self._player.terminate()
                self._player = None
            self._is_stream_active = False
            self._current_stream_url = None

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
        return self._play_stream(url, announce_retry=True)

    def _play_stream(self, url: str, announce_retry: bool) -> bool:
        return self._play_stream_locked(url, announce_retry=announce_retry)

    def _play_stream_locked(self, url: str, announce_retry: bool) -> bool:
        """Start playing a stream with minimal lock contention."""
        with self._lock:
            self._stop_internal()
            self._desired_stream_url = url
            self._playback_error.clear()
            self._playback_error_reason = None

        for attempt in range(1, self._retry_config.max_attempts + 1):
            logger.info(
                "Attempting to connect to stream (attempt %d/%d): %s",
                attempt,
                self._retry_config.max_attempts,
                url,
            )

            try:
                player = self._create_player(prefetch=True)
                player.play(url)
                with self._lock:
                    self._player = player

                # Wait for stream to start
                for _ in range(50):
                    time.sleep(0.1)
                    with self._lock:
                        current_player = self._player
                    if current_player is not player:
                        break
                    if self._is_player_playing_instance(player):
                        playback_time = player.playback_time
                        with self._lock:
                            if self._player is not player:
                                break
                            self._is_stream_active = True
                            self._current_stream_url = url
                            self._start_watchdog()
                        logger.info(
                            "Stream connected successfully (playback_time: %s)",
                            playback_time,
                        )
                        return True

                logger.warning("Stream failed to start on attempt %d", attempt)
                with self._lock:
                    self._stop_internal()

            except Exception as e:
                logger.exception("MPV error on attempt %d: %s", attempt, e)
                with self._lock:
                    self._stop_internal()

            # Play retry announcement if we will retry
            if attempt < self._retry_config.max_attempts and announce_retry:
                logger.info("Playing retry announcement before next attempt")
                self._play_announcement_internal(self._error_announcements.retrying)
                time.sleep(self._retry_config.delay_seconds)

        logger.error(
            "Failed to connect to stream after %d attempts",
            self._retry_config.max_attempts,
        )
        with self._lock:
            self._current_stream_url = None
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

    def _is_player_playing_instance(self, player: mpv.MPV) -> bool:
        try:
            playback_time = player.playback_time
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
            self._desired_stream_url = stream_url

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
            for i in range(50):
                time.sleep(0.1)
                with self._lock:
                    if self._is_player_playing():
                        self._is_stream_active = True
                        self._current_stream_url = stream_url
                        self._start_watchdog()
                        logger.info(
                            "Stream playing after seamless transition (waited %.1fs)",
                            (i + 1) * 0.1,
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

    def play_selector_off_announcement(self) -> None:
        """Play announcement when radio starts with selector switch off."""
        self.play_announcement(self._selector_off_announcement)

    def play_shutdown_announcement(self) -> None:
        """Play announcement before system shutdown."""
        self.play_announcement(self._shutdown_announcement)

    def _stop_internal(self) -> None:
        """Stop playback without acquiring lock (internal use)."""
        if self._player is not None:
            with contextlib.suppress(mpv.ShutdownError, Exception):
                self._player.terminate()
            self._player = None
        self._is_stream_active = False
        self._current_stream_url = None
        self._desired_stream_url = None
        self._stop_watchdog()

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
        """Clean up resources."""
        self.stop()
