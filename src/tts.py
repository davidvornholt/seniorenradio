"""Text-to-speech service for Seniorenradio."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .models import TtsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TtsResult:
    """Result of a TTS operation."""

    success: bool
    error: str | None


class TtsSpeaker:
    """Basic TTS wrapper around espeak-ng or pico2wave."""

    _DEFAULT_TTS_TIMEOUT_SECONDS = 10

    def __init__(self, config: TtsConfig) -> None:
        self._config = config
        self._lock = Lock()

    def speak(self, text: str) -> TtsResult:
        """Speak a single text string."""
        if not self._config.enabled:
            return TtsResult(success=False, error="TTS disabled")

        cleaned = text.strip()
        if not cleaned:
            return TtsResult(success=False, error="Empty text")

        if self._config.engine == "pico2wave":
            return self._speak_with_pico2wave(cleaned)

        return self._speak_with_espeak(cleaned)

    def speak_lines(self, lines: tuple[str, ...]) -> TtsResult:
        """Speak multiple lines sequentially, stopping on first failure."""
        if not lines:
            return TtsResult(success=False, error="No lines to speak")

        for line in lines:
            result = self.speak(line)
            if not result.success:
                return TtsResult(success=False, error=result.error)

        return TtsResult(success=True, error=None)

    def _build_args(self, text: str) -> tuple[str, ...]:
        voice_args = (
            ("-v", self._config.voice)
            if self._config.voice is not None and self._config.voice.strip()
            else ()
        )
        return (
            self._config.engine,
            "-s",
            str(self._config.rate),
            "-a",
            str(self._config.volume),
            *voice_args,
            text,
        )

    def _speak_with_espeak(self, text: str) -> TtsResult:
        args = self._build_args(text)

        with self._lock:
            try:
                completed = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self._DEFAULT_TTS_TIMEOUT_SECONDS,
                )
            except FileNotFoundError:
                msg = f"TTS engine not found: {self._config.engine}"
                logger.warning(msg)
                return TtsResult(success=False, error=msg)
            except subprocess.TimeoutExpired:
                msg = (
                    "TTS timed out after "
                    f"{self._DEFAULT_TTS_TIMEOUT_SECONDS}s: {self._config.engine}"
                )
                logger.warning(msg)
                return TtsResult(success=False, error=msg)

        if completed.returncode != 0:
            msg = completed.stderr.strip() or "TTS failed"
            logger.warning("TTS failed: %s", msg)
            return TtsResult(success=False, error=msg)

        return TtsResult(success=True, error=None)

    def _speak_with_pico2wave(self, text: str) -> TtsResult:
        language = self._config.voice.strip() if self._config.voice else "de-DE"

        with tempfile.NamedTemporaryFile(
            prefix="tts_",
            suffix=".wav",
            delete=False,
        ) as temp:
            wav_path = temp.name

        try:
            with self._lock:
                try:
                    pico_completed = subprocess.run(
                        (
                            self._config.engine,
                            "-l",
                            language,
                            "-w",
                            wav_path,
                            text,
                        ),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=self._DEFAULT_TTS_TIMEOUT_SECONDS,
                    )
                except FileNotFoundError:
                    msg = f"TTS engine not found: {self._config.engine}"
                    logger.warning(msg)
                    return TtsResult(success=False, error=msg)
                except subprocess.TimeoutExpired:
                    msg = (
                        "pico2wave timed out after "
                        f"{self._DEFAULT_TTS_TIMEOUT_SECONDS}s"
                    )
                    logger.warning("%s (engine=%s)", msg, self._config.engine)
                    return TtsResult(success=False, error=msg)

                if pico_completed.returncode != 0:
                    msg = pico_completed.stderr.strip() or "pico2wave failed"
                    logger.warning("pico2wave failed: %s", msg)
                    return TtsResult(success=False, error=msg)

                try:
                    aplay_completed = subprocess.run(
                        ("aplay", "-q", wav_path),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=self._DEFAULT_TTS_TIMEOUT_SECONDS,
                    )
                except FileNotFoundError:
                    msg = "aplay not found for pico2wave playback"
                    logger.warning(msg)
                    return TtsResult(success=False, error=msg)
                except subprocess.TimeoutExpired:
                    msg = f"aplay timed out after {self._DEFAULT_TTS_TIMEOUT_SECONDS}s"
                    logger.warning("%s (wav=%s)", msg, wav_path)
                    return TtsResult(success=False, error=msg)

                if aplay_completed.returncode != 0:
                    msg = aplay_completed.stderr.strip() or "aplay failed"
                    logger.warning("aplay failed: %s", msg)
                    return TtsResult(success=False, error=msg)
        finally:
            with contextlib.suppress(OSError):
                Path(wav_path).unlink()

        return TtsResult(success=True, error=None)
