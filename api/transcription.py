"""Short dictation through a resident loopback Whisper server; never persists text."""
import asyncio
from array import array
from contextlib import suppress
from pathlib import Path
import sys
import tempfile
import threading
import wave

from fastapi import APIRouter, Depends, Header, HTTPException, Request
import httpx
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from api.config import Settings, get_settings

MAX_BYTES = 12 * 1024 * 1024
MAX_SECONDS = 120
_GATE = threading.BoundedSemaphore(1)
_IN_FLIGHT: set[asyncio.Task] = set()
RESPONSE_TIMEOUT = 90


def failure(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"error": {"code": code, "message": message}})


def require_transcription_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    # A dedicated phone token cannot authorize the financial or management routes:
    # the configured transcription token and `transcription`-scope client tokens
    # reach only this router, while `full` tokens and the service token reach it too.
    from api.auth import constant_time_equal, token_accepted

    configured = settings.transcription_token
    if configured and authorization and constant_time_equal(authorization, "Bearer " + configured):
        return
    if not token_accepted(settings, authorization, ("full", "transcription")):
        raise failure(401, "unauthorized", "Missing or invalid transcription token")


router = APIRouter(tags=["transcription"],
                   dependencies=[Depends(require_transcription_token)])


class Transcript(BaseModel):
    text: str
    duration_seconds: float


async def normalize(source: Path, target: Path, media_type: str, settings: Settings) -> float:
    from api.telemetry import tracer

    with tracer.start_as_current_span("transcription.normalize", attributes={
        "q_core.audio.media_type": media_type, "q_core.audio.bytes": source.stat().st_size,
    }) as span:
        duration = await _normalize(source, target, media_type, settings)
        span.set_attribute("q_core.audio.seconds", round(duration, 1))
        return duration


async def infer(wav: Path, settings: Settings) -> str:
    """Whisper inference in a `transcription.infer` span; never the text."""
    from api.telemetry import tracer

    with tracer.start_as_current_span("transcription.infer"):
        return await _infer(wav, settings)


async def _normalize(source: Path, target: Path, media_type: str, settings: Settings) -> float:
    args = [settings.transcription_ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe", "-f", "wav" if media_type == "audio/wav" else "mov"]
    if media_type == "audio/mp4":
        args += ["-enable_drefs", "0"]
    args += ["-i", str(source), "-map", "0:a:0", "-vn", "-t", str(MAX_SECONDS + 1),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-y", str(target)]
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise failure(503, "transcription_unavailable", "Audio conversion is not installed") from None
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            with suppress(ProcessLookupError): process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise failure(422, "invalid_audio", "The recording could not be decoded")
    try:
        with wave.open(str(target), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            samples = array("h", wav.readframes(wav.getnframes()))
            if sys.byteorder != "little": samples.byteswap()
    except (wave.Error, EOFError, OSError):
        raise failure(422, "invalid_audio", "The recording could not be decoded") from None
    if duration > MAX_SECONDS:
        raise failure(413, "recording_too_long", "Recordings must be 120 seconds or shorter")
    if duration < .1 or not samples or max(abs(x) for x in samples) < 64:
        raise failure(422, "no_speech", "No audible speech was detected; record again")
    return duration


async def _infer(wav: Path, settings: Settings) -> str:
    # Configured as a port, not an arbitrary URL: raw audio stays on loopback.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=3, write=20, pool=3), trust_env=False) as client:
            with wav.open("rb") as recording:
                async with client.stream("POST", f"http://127.0.0.1:{settings.whisper_port}/inference",
                    files={"file": ("recording.wav", recording, "audio/wav")},
                    data={"response_format": "json", "language": "en", "temperature": "0.0"}) as response:
                    if response.status_code != 200:
                        raise failure(503, "transcription_unavailable", "Whisper could not process this recording")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 65536:
                            raise failure(502, "invalid_transcription", "Whisper returned an invalid response")
        import json
        result = json.loads(chunks)
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError()
        text = text.strip()
        if not text:
            raise failure(422, "no_speech", "No speech was recognized; record again")
        return text
    except httpx.TimeoutException:
        raise failure(504, "transcription_timeout", "Transcription timed out; try a shorter recording") from None
    except httpx.RequestError:
        raise failure(503, "transcription_unavailable", "The Whisper service is unavailable") from None
    except (ValueError, TypeError):
        raise failure(502, "invalid_transcription", "Whisper returned an invalid response") from None


async def finish(wav: Path, duration: float, settings: Settings,
                 temporary: tempfile.TemporaryDirectory) -> Transcript:
    # Keep capacity and audio owned by the real backend request even if the
    # phone disconnects or its response deadline expires. Whisper cannot cancel
    # inference remotely; admitting another request would build a hidden queue.
    try:
        text = await infer(wav, settings)
        return Transcript(text=text, duration_seconds=round(duration, 3))
    finally:
        temporary.cleanup()
        _GATE.release()


def finished(task: asyncio.Task) -> None:
    _IN_FLIGHT.discard(task)
    if not task.cancelled():
        task.exception()  # consume abandoned failure without logging its content


@router.post("/transcriptions", response_model=Transcript)
async def transcribe(request: Request, settings: Settings = Depends(get_settings)) -> Transcript:
    """Accept raw audio/wav or audio/mp4 body; return editable text, never submit it."""
    if not settings.transcription_enabled:
        raise failure(503, "transcription_unavailable", "Transcription is not configured")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {"audio/wav", "audio/mp4"}:
        raise failure(415, "unsupported_audio", "Send a WAV or M4A recording")
    if not _GATE.acquire(blocking=False):
        raise failure(429, "transcription_busy", "Another recording is being transcribed; try again shortly")
    temporary = None
    delegated = False
    try:
        temporary = tempfile.TemporaryDirectory(prefix="q-core-dictation-")
        source, wav = Path(temporary.name)/"upload", Path(temporary.name)/"normalized.wav"
        size = 0
        async with asyncio.timeout(30):
            with source.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise failure(413, "recording_too_large", "Recording exceeds the 12 MiB limit")
                    output.write(chunk)
        if not size:
            raise failure(422, "invalid_audio", "The recording is empty")
        duration = await normalize(source, wav, media_type, settings)
        source.unlink()
        job = asyncio.create_task(finish(wav, duration, settings, temporary))
        _IN_FLIGHT.add(job)
        job.add_done_callback(finished)
        delegated = True
        try:
            return await asyncio.wait_for(asyncio.shield(job), timeout=RESPONSE_TIMEOUT)
        except TimeoutError:
            raise failure(504, "transcription_timeout",
                          "Transcription is still finishing; try again shortly") from None
    except ClientDisconnect:
        raise failure(400, "recording_interrupted", "The recording upload was interrupted") from None
    except TimeoutError:
        raise failure(408, "recording_timeout", "Uploading or decoding the recording took too long") from None
    finally:
        if not delegated:
            if temporary is not None:
                temporary.cleanup()
            _GATE.release()
