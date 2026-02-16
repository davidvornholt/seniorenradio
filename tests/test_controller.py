"""Tests for RadioController state machine with all dependencies mocked."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

from src.controller import RadioController
from src.models import RadioState, SwitchPosition
from tests.conftest import make_app_config


@pytest.fixture
def controller_deps(tmp_audio_files: dict[str, Path]) -> dict[str, object]:
    """Create controller with all mocked dependencies."""
    config = make_app_config(tmp_audio_files["audio_dir"])

    audio = MagicMock()
    audio.play_announcement.return_value = True
    audio.play_stream.return_value = True
    audio.play_announcement_with_stream_preload.return_value = True
    audio.is_playing.return_value = False

    network = MagicMock()
    tts = MagicMock()

    controller = RadioController(
        config=config,
        audio_player=audio,
        network_manager=network,
        tts_speaker=tts,
    )

    return {
        "controller": controller,
        "config": config,
        "audio": audio,
        "network": network,
        "tts": tts,
    }


class TestInitialState:
    def test_default_state(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        state = controller.state
        assert state.selected_channel_index == 0
        assert state.switch_position == SwitchPosition.OFF
        assert state.is_stream_active is False


class TestHandleStartup:
    def test_startup_switch_on_dispatches_playback(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        controller.handle_startup(SwitchPosition.ON)
        # Give the worker thread time to execute
        time.sleep(0.3)

        assert controller.state.switch_position == SwitchPosition.ON
        audio.play_announcement_with_stream_preload.assert_called()

    def test_startup_switch_off_plays_selector_off(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        controller.handle_startup(SwitchPosition.OFF)

        assert controller.state.switch_position == SwitchPosition.OFF
        audio.play_selector_off_announcement.assert_called_once()
        audio.play_announcement_with_stream_preload.assert_not_called()


class TestHandleChannelButton:
    def test_changes_channel(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]

        # Set switch to ON first
        controller.handle_switch_change(SwitchPosition.ON)
        time.sleep(0.1)

        controller.handle_channel_button(1)
        time.sleep(0.3)

        assert controller.state.selected_channel_index == 1

    def test_ignored_when_switch_off(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        # Switch is OFF by default
        controller.handle_channel_button(0)

        assert not audio.play_announcement_with_stream_preload.called

    def test_ignored_same_channel_active(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        # Manually set state to ON with active stream on channel 0
        controller._state = RadioState(
            selected_channel_index=0,
            switch_position=SwitchPosition.ON,
            is_stream_active=True,
        )
        audio.reset_mock()

        controller.handle_channel_button(0)

        audio.play_announcement_with_stream_preload.assert_not_called()

    def test_invalid_index_no_crash(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        # Set switch to ON
        controller._state = RadioState(
            selected_channel_index=0,
            switch_position=SwitchPosition.ON,
            is_stream_active=False,
        )

        controller.handle_channel_button(99)
        audio.play_announcement_with_stream_preload.assert_not_called()


class TestHandleSwitchChange:
    def test_switch_on_dispatches_playback(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        controller.handle_switch_change(SwitchPosition.ON)
        time.sleep(0.3)

        assert controller.state.switch_position == SwitchPosition.ON
        audio.play_announcement_with_stream_preload.assert_called()

    def test_switch_off_stops_and_plays_goodbye(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        # Set switch ON first
        controller._state = RadioState(
            selected_channel_index=0,
            switch_position=SwitchPosition.ON,
            is_stream_active=True,
        )

        controller.handle_switch_change(SwitchPosition.OFF)
        time.sleep(0.3)

        assert controller.state.switch_position == SwitchPosition.OFF
        audio.play_goodbye_announcement.assert_called()

    def test_same_position_ignored(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        # Already OFF
        controller.handle_switch_change(SwitchPosition.OFF)

        audio.play_selector_off_announcement.assert_not_called()
        audio.play_goodbye_announcement.assert_not_called()
        audio.play_announcement_with_stream_preload.assert_not_called()


class TestHandleShutdownRequest:
    def test_stops_audio_and_plays_shutdown(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        controller.handle_shutdown_request()

        audio.stop.assert_called()
        audio.play_shutdown_announcement.assert_called_once()

    def test_sets_cancel_event(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        controller.handle_shutdown_request()
        assert controller._worker_cancel.is_set()


class TestHandleDebugRequest:
    def test_debug_disabled_is_noop(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        tts = controller_deps["tts"]
        controller._config = controller._config.__class__(
            **{
                **controller._config.__dict__,
                "debug": controller._config.debug.__class__(
                    **{**controller._config.debug.__dict__, "enabled": False}
                ),
            }
        )

        controller.handle_debug_request()
        tts.speak_lines.assert_not_called()

    def test_debug_speaks_lines(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        tts = controller_deps["tts"]

        controller.handle_debug_request()
        tts.speak_lines.assert_called()


class TestShutdown:
    def test_sets_cancel_and_cleans_up(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        controller.shutdown()

        assert controller._worker_cancel.is_set()
        audio.stop.assert_called()
        audio.cleanup.assert_called_once()

    def test_joins_worker_thread(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]

        # Set switch ON and dispatch work to create worker thread
        controller._state = RadioState(
            selected_channel_index=0,
            switch_position=SwitchPosition.ON,
            is_stream_active=False,
        )
        controller.handle_switch_change(SwitchPosition.OFF)
        time.sleep(0.1)

        controller.shutdown()
        # Worker thread should be joined (not alive)
        if controller._worker_thread is not None:
            assert not controller._worker_thread.is_alive()


class TestDispatch:
    def test_cancels_previous_operation(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        audio = controller_deps["audio"]

        task1_started = Event()
        task1_cancelled = Event()

        def slow_task() -> None:
            task1_started.set()
            task1_cancelled.wait(timeout=5.0)

        controller._dispatch(slow_task)
        task1_started.wait(timeout=2.0)

        # Dispatch a second task — should cancel the first
        controller._dispatch(lambda: None)
        time.sleep(0.1)

        # First dispatch sets cancel before clearing for second
        assert audio.stop.called

    def test_new_task_executes(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]

        result = Event()
        controller._dispatch(result.set)
        result.wait(timeout=2.0)

        assert result.is_set()


class TestGetChannel:
    def test_valid_index(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        channel = controller._get_channel(0)
        assert channel is not None
        assert channel.index == 0

    def test_invalid_index_returns_none(self, controller_deps: dict) -> None:
        controller = controller_deps["controller"]
        assert controller._get_channel(99) is None
        assert controller._get_channel(-1) is None
