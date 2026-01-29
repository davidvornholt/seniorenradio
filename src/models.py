"""Domain models for Seniorenradio.

Immutable data structures representing configuration and application state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path


class SwitchPosition(Enum):
    """Selector switch position."""

    OFF = auto()
    ON = auto()


@dataclass(frozen=True)
class Channel:
    """A radio channel configuration."""

    index: int
    name: str
    stream_url: str
    announcement_file: Path


@dataclass(frozen=True)
class GpioConfig:
    """GPIO pin configuration."""

    channel_pins: tuple[int, ...]
    switch_pin: int
    debounce_ms: int


@dataclass(frozen=True)
class AudioConfig:
    """Audio output configuration."""

    device: str
    volume: int


@dataclass(frozen=True)
class RetryConfig:
    """Stream retry configuration."""

    max_attempts: int
    delay_seconds: float


@dataclass(frozen=True)
class ErrorAnnouncementsConfig:
    """Error announcement audio files."""

    retrying: Path  # "Trying to reconnect..."
    failed: Path  # "Connection failed, giving up"
    no_internet: Path  # "No internet connection"


@dataclass(frozen=True)
class AppConfig:
    """Complete application configuration."""

    audio: AudioConfig
    gpio: GpioConfig
    retry: RetryConfig
    channels: tuple[Channel, ...]
    default_channel_index: int
    audio_dir: Path
    error_announcements: ErrorAnnouncementsConfig
    goodbye_announcement: Path
    selector_off_announcement: Path
    shutdown_announcement: Path


@dataclass(frozen=True)
class RadioState:
    """Current radio state."""

    selected_channel_index: int
    switch_position: SwitchPosition
    is_stream_active: bool

    def with_channel(self, index: int) -> RadioState:
        """Return new state with updated channel."""
        return RadioState(
            selected_channel_index=index,
            switch_position=self.switch_position,
            is_stream_active=self.is_stream_active,
        )

    def with_switch(self, position: SwitchPosition) -> RadioState:
        """Return new state with updated switch position."""
        return RadioState(
            selected_channel_index=self.selected_channel_index,
            switch_position=position,
            is_stream_active=self.is_stream_active,
        )

    def with_stream_active(self, active: bool) -> RadioState:
        """Return new state with updated stream status."""
        return RadioState(
            selected_channel_index=self.selected_channel_index,
            switch_position=self.switch_position,
            is_stream_active=active,
        )
