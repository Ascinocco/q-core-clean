import asyncio
import io
import math
from pathlib import Path
import struct
import wave

import httpx
import pytest

from api import transcription as subject


def recording(seconds=.2, amplitude=8000):
    data = io.BytesIO()
    with wave.open(data, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b''.join(struct.pack('<h', int(amplitude * math.sin(i / 10)))
                                 for i in range(int(seconds * 16000))))
    return data.getvalue()


@pytest.fixture
def enabled(test_settings):
    # The default is Homebrew's path; on Linux (CI, the NixOS server) use PATH.
    import os
    import shutil
    if not os.access(test_settings.transcription_ffmpeg, os.X_OK):
        found = shutil.which('ffmpeg')
        if found is None:
            pytest.skip('ffmpeg is not installed')
        test_settings.transcription_ffmpeg = found
    test_settings.transcription_enabled = True
    test_settings.transcription_token = 'phone-test-token'
    return test_settings


def post(client, body=None, token='phone-test-token', media='audio/wav'):
    return client.post('/transcriptions', content=recording() if body is None else body,
                       headers={'Authorization': 'Bearer '+token, 'Content-Type': media})


def test_auth_disabled_and_scoped_phone_token(client, test_settings):
    assert post(client, token='wrong').status_code == 401
    assert post(client, token='test-token').status_code == 503
    test_settings.transcription_token = 'phone-test-token'
    assert client.get('/entities', headers={'Authorization':'Bearer phone-test-token'}).status_code == 401


def test_transcribes_normalized_audio_and_cleans_temporary_files(client, enabled, monkeypatch):
    seen = []
    async def infer(path, settings):
        seen.append(path)
        with wave.open(str(path)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        return 'Edit the SSH connection.'
    monkeypatch.setattr(subject, 'infer', infer)
    response = post(client)
    assert response.status_code == 200
    assert response.json() == {'text':'Edit the SSH connection.', 'duration_seconds':.2}
    assert not seen[0].parent.exists()


def test_rejects_silent_malformed_empty_and_wrong_media(client, enabled):
    for data, expected in [(recording(amplitude=0), 'no_speech'), (b'invalid', 'invalid_audio'), (b'', 'invalid_audio')]:
        response = post(client, data)
        assert response.status_code == 422
        assert response.json()['error']['code'] == expected
    assert post(client, media='text/plain').status_code == 415


def test_bounds_upload_and_duration(client, enabled, monkeypatch):
    monkeypatch.setattr(subject, 'MAX_BYTES', 32)
    assert post(client, b'x'*33).status_code == 413
    monkeypatch.setattr(subject, 'MAX_BYTES', 100000)
    monkeypatch.setattr(subject, 'MAX_SECONDS', 1)
    response = post(client, recording(seconds=1.5))
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'recording_too_long'


def test_busy_fails_fast_and_gate_is_reusable(client, enabled, monkeypatch):
    assert subject._GATE.acquire(False)
    try: assert post(client).status_code == 429
    finally: subject._GATE.release()
    async def infer(*args): return 'Ready'
    monkeypatch.setattr(subject, 'infer', infer)
    assert post(client).status_code == 200


def test_upstream_error_cleans_files_without_content_in_response(client, enabled, monkeypatch, caplog):
    paths = []
    async def infer(path, settings):
        paths.append(path)
        raise subject.failure(503, 'transcription_unavailable', 'The Whisper service is unavailable')
    monkeypatch.setattr(subject, 'infer', infer)
    response = post(client)
    assert response.status_code == 503
    assert not paths[0].parent.exists()
    assert 'normalized.wav' not in response.text
    assert 'phone-test-token' not in caplog.text


@pytest.mark.parametrize('status,payload,expected', [
    (503, {'error':'private server detail'}, 'transcription_unavailable'),
    (200, {'text':None}, 'invalid_transcription'),
    (200, {'text':''}, 'no_speech'),
    (200, {'text':'x'*70000}, 'invalid_transcription'),
])
def test_upstream_contract_and_bounded_response(tmp_path, enabled, monkeypatch, status, payload, expected):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=payload))
    monkeypatch.setattr(subject.httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    wav = tmp_path/'audio.wav'
    wav.write_bytes(recording())
    with pytest.raises(subject.HTTPException) as error:
        asyncio.run(subject.infer(wav, enabled))
    assert error.value.detail['error']['code'] == expected


def test_upstream_is_loopback_and_upload_filename_is_constant(tmp_path, enabled, monkeypatch):
    original = httpx.AsyncClient
    def respond(request):
        assert str(request.url) == 'http://127.0.0.1:18542/inference'
        assert b'filename="recording.wav"' in request.content
        assert str(tmp_path).encode() not in request.content
        return httpx.Response(200,json={'text':'  Correct wording.  '})
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(subject.httpx,'AsyncClient',lambda **kw:original(transport=transport, **kw))
    wav = tmp_path/'audio.wav'
    wav.write_bytes(recording())
    assert asyncio.run(subject.infer(wav,enabled)) == 'Correct wording.'


def test_timeout_keeps_capacity_until_backend_finishes(tmp_path, enabled, monkeypatch):
    from starlette.requests import Request
    monkeypatch.setattr(subject, 'RESPONSE_TIMEOUT', .02)
    async def run():
        release = asyncio.Event()
        seen = []
        async def infer(path, settings):
            seen.append(path)
            await release.wait()
            return 'Completed later'
        monkeypatch.setattr(subject, 'infer', infer)
        payload = recording()
        async def receive():
            return {'type':'http.request','body':payload,'more_body':False}
        request = Request({'type':'http','headers':[(b'content-type',b'audio/wav')]},receive)
        with pytest.raises(subject.HTTPException) as error:
            await subject.transcribe(request, enabled)
        assert error.value.status_code == 504
        assert seen[0].exists()
        assert not subject._GATE.acquire(False)
        release.set()
        await asyncio.gather(*list(subject._IN_FLIGHT))
        assert not seen[0].parent.exists()
        assert subject._GATE.acquire(False)
        subject._GATE.release()
    asyncio.run(run())


def test_cancel_keeps_capacity_and_cleans_after_backend(tmp_path, enabled, monkeypatch):
    from starlette.requests import Request
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        paths = []
        async def infer(path, settings):
            paths.append(path); started.set()
            await release.wait()
            return 'Never inserted automatically'
        monkeypatch.setattr(subject, 'infer', infer)
        async def receive(): return {'type':'http.request','body':recording(),'more_body':False}
        request = Request({'type':'http','headers':[(b'content-type',b'audio/wav')]},receive)
        caller = asyncio.create_task(subject.transcribe(request, enabled))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError): await caller
        assert not subject._GATE.acquire(False)
        release.set()
        await asyncio.gather(*list(subject._IN_FLIGHT))
        assert not paths[0].parent.exists()
        assert subject._GATE.acquire(False)
        subject._GATE.release()
    asyncio.run(run())


def test_disconnected_upload_releases_capacity(enabled):
    from starlette.requests import Request
    async def run():
        async def receive(): return {'type':'http.disconnect'}
        request = Request({'type':'http','headers':[(b'content-type',b'audio/wav')]},receive)
        with pytest.raises(subject.HTTPException) as error: await subject.transcribe(request,enabled)
        assert error.value.status_code == 400
        assert subject._GATE.acquire(False)
        subject._GATE.release()
    asyncio.run(run())


def test_m4a_normalization(client, enabled, monkeypatch, tmp_path):
    import subprocess
    wav, m4a = tmp_path/'source.wav', tmp_path/'source.m4a'
    wav.write_bytes(recording())
    subprocess.run([enabled.transcription_ffmpeg,'-v','error','-i',str(wav),'-c:a','aac',str(m4a)],check=True)
    async def infer(*args): return 'M4A works'
    monkeypatch.setattr(subject,'infer',infer)
    response=post(client,m4a.read_bytes(),media='audio/mp4')
    assert response.status_code==200
    assert response.json()['text']=='M4A works'
