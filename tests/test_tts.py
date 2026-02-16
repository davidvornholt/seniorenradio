"""Tests for TTS service — subprocess mocking for espeak-ng and pico2wave."""

from __future__ import annotations

import subprocess

from pytest_mock import MockerFixture

from src.models import TtsConfig
from src.tts import TtsSpeaker


def make_tts_config(
    enabled: bool = True,
    engine: str = "espeak-ng",
    voice: str | None = None,
    rate: int = 160,
    volume: int = 100,
) -> TtsConfig:
    return TtsConfig(
        enabled=enabled,
        engine=engine,
        voice=voice,
        rate=rate,
        volume=volume,
    )


class TestSpeakDisabled:
    def test_returns_error_when_disabled(self) -> None:
        speaker = TtsSpeaker(config=make_tts_config(enabled=False))
        result = speaker.speak("Hello")
        assert not result.success
        assert result.error is not None
        assert "disabled" in result.error.lower()


class TestSpeakEmpty:
    def test_empty_text_returns_error(self) -> None:
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("")
        assert not result.success
        assert "empty" in (result.error or "").lower()

    def test_whitespace_only_returns_error(self) -> None:
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("   ")
        assert not result.success


class TestSpeakEspeak:
    def test_success(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("Hello world")
        assert result.success

    def test_engine_not_found(self, mocker: MockerFixture) -> None:
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("espeak-ng"))
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("Hello")
        assert not result.success
        assert "not found" in (result.error or "").lower()

    def test_timeout(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="espeak-ng", timeout=10),
        )
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("Hello")
        assert not result.success
        assert "timed out" in (result.error or "").lower()

    def test_nonzero_returncode(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="Synth error"
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak("Hello")
        assert not result.success
        assert "Synth error" in (result.error or "")

    def test_args_include_voice(self, mocker: MockerFixture) -> None:
        mock_run = mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config(voice="de"))
        speaker.speak("Hallo")

        call_args = mock_run.call_args[0][0]
        assert "-v" in call_args
        assert "de" in call_args

    def test_args_omit_voice_when_none(self, mocker: MockerFixture) -> None:
        mock_run = mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config(voice=None))
        speaker.speak("Hello")

        call_args = mock_run.call_args[0][0]
        assert "-v" not in call_args

    def test_args_include_rate_and_volume(self, mocker: MockerFixture) -> None:
        mock_run = mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config(rate=120, volume=150))
        speaker.speak("Test")

        call_args = mock_run.call_args[0][0]
        assert "120" in call_args
        assert "150" in call_args


class TestSpeakPico2wave:
    def test_success(self, mocker: MockerFixture) -> None:
        mock_run = mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        # Mock Path.unlink to avoid filesystem issues
        mocker.patch("pathlib.Path.unlink")

        speaker = TtsSpeaker(config=make_tts_config(engine="pico2wave"))
        result = speaker.speak("Hallo Welt")
        assert result.success
        # pico2wave + aplay = 2 subprocess calls
        assert mock_run.call_count == 2

    def test_aplay_not_found(self, mocker: MockerFixture) -> None:
        # First call (pico2wave) succeeds, second (aplay) fails
        mocker.patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                FileNotFoundError("aplay"),
            ],
        )
        mocker.patch("pathlib.Path.unlink")

        speaker = TtsSpeaker(config=make_tts_config(engine="pico2wave"))
        result = speaker.speak("Hallo")
        assert not result.success
        assert "aplay" in (result.error or "").lower()

    def test_temp_file_cleaned_up_on_failure(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="error"
            ),
        )
        mock_unlink = mocker.patch("pathlib.Path.unlink")

        speaker = TtsSpeaker(config=make_tts_config(engine="pico2wave"))
        speaker.speak("Test")
        mock_unlink.assert_called_once()


class TestSpeakLines:
    def test_all_succeed(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak_lines(("Line one.", "Line two.", "Line three."))
        assert result.success

    def test_stops_on_first_failure(self, mocker: MockerFixture) -> None:
        call_count = 0

        def mock_run_side_effect(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="fail"
                )
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

        mocker.patch("subprocess.run", side_effect=mock_run_side_effect)
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak_lines(("One.", "Two.", "Three."))
        assert not result.success
        # Third line should not have been attempted
        assert call_count == 2

    def test_empty_lines(self) -> None:
        speaker = TtsSpeaker(config=make_tts_config())
        result = speaker.speak_lines(())
        assert not result.success
