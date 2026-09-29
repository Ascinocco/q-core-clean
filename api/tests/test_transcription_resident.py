"""Opt-in real resident service test; only synthetic speech, no live API/database."""
import os
from pathlib import Path
import socket
import subprocess
import time

import httpx
import pytest


@pytest.mark.skipif(not os.environ.get('WHISPER_TEST_BINARY'), reason='opt-in resident Whisper fixture')
def test_resident_model_serves_two_requests(client, test_settings, tmp_path):
    binary = Path(os.environ['WHISPER_TEST_BINARY'])
    model = Path(os.environ['WHISPER_TEST_MODEL'])
    phrase = 'Fix the SSH connection, run the unit tests, and create a pull request.'
    aiff, wav = tmp_path/'speech.aiff', tmp_path/'speech.wav'
    subprocess.run(['say','-o',str(aiff),phrase],check=True)
    subprocess.run([test_settings.transcription_ffmpeg,'-v','error','-i',str(aiff),
                    '-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],check=True)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0))
        port = reserve.getsockname()[1]
    started=time.monotonic()
    process=subprocess.Popen([str(binary),'--host','127.0.0.1','--port',str(port),'-m',str(model),
                              '-t','4','-sns','-nt'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(timeout=2,trust_env=False) as health:
            while time.monotonic()-started < 60:
                assert process.poll() is None, 'fixture server exited'
                try:
                    if health.get(f'http://127.0.0.1:{port}/health').status_code==200: break
                except httpx.RequestError: pass
                time.sleep(.1)
            else: pytest.fail('fixture server did not become ready')
        cold=time.monotonic()-started
        test_settings.transcription_enabled=True
        test_settings.whisper_port=port
        durations=[]
        for _ in range(2):
            start=time.monotonic()
            response=client.post('/transcriptions',content=wav.read_bytes(),
                                 headers={'Authorization':'Bearer test-token','Content-Type':'audio/wav'})
            durations.append(time.monotonic()-start)
            assert response.status_code==200
            text=response.json()['text'].lower()
            assert 'ssh' in text and 'unit tests' in text and 'pull request' in text
            assert process.poll() is None
        print(f'Resident fixture: one PID, cold readiness {cold:.3f}s, responses {durations}')
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill();process.wait()
