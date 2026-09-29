#!/usr/bin/env python3
"""Install and operate the optional per-user, loopback-only resident Whisper service."""
import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import urllib.request

REVISION = 'a44e07845931421bb6f3447ce0010ed9dc76a118'
MODEL_SHA256 = 'c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d'
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin'
LABEL = 'com.q-core.whisper'


def run(*args):
    subprocess.run(args, check=True)


def model_valid(path):
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == MODEL_SHA256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'run', 'start', 'stop', 'status'])
    parser.add_argument('--home', type=Path, default=Path.home()/'Library/Application Support/q-core/whisper')
    parser.add_argument('--port', type=int, default=18542)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    home = args.home.expanduser().resolve()
    source, model = home/'source', home/'ggml-small.en.bin'
    binary = source/'build/bin/whisper-server'
    plist = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    domain = f'gui/{os.getuid()}'
    if args.action == 'install':
        for command in ['git', 'cmake', 'ffmpeg']:
            if shutil.which(command) is None:
                parser.error(f'{command} is required (install with Homebrew)')
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not source.exists():
            run('git', 'init', str(source))
        # Refuse to discard local edits or silently switch an unrelated checkout.
        if subprocess.check_output(['git','-C',str(source),'status','--porcelain']).strip():
            parser.error('Whisper source has local changes; preserve or remove them before installing')
        run('git','-C',str(source),'fetch','--depth','1','https://github.com/ggml-org/whisper.cpp.git',REVISION)
        run('git','-C',str(source),'checkout','--detach',REVISION)
        run('cmake','-S',str(source),'-B',str(source/'build'),'-DCMAKE_BUILD_TYPE=Release','-DWHISPER_BUILD_SERVER=ON')
        run('cmake','--build',str(source/'build'),'--config','Release','--target','whisper-server','-j','4')
        if not model_valid(model):
            partial = model.with_suffix('.download')
            try:
                urllib.request.urlretrieve(MODEL_URL, partial)
                if not model_valid(partial):
                    raise RuntimeError('Downloaded model checksum did not match the pinned model')
                partial.replace(model)
            finally:
                partial.unlink(missing_ok=True)
        print('Installed pinned whisper.cpp and small.en. Run start, then status.')
    elif args.action == 'run':
        if not binary.is_file() or not model_valid(model):
            parser.error('Install the pinned binary and model first')
        # whisper-server may print recognized words. Drop both streams before
        # starting it; only status/health is exposed to the operator.
        with open(os.devnull, 'wb') as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        os.execv(str(binary), [str(binary), '--host', '127.0.0.1', '--port', str(args.port),
                              '-m', str(model), '-t', '4', '-sns', '-nt'])
    elif args.action == 'start':
        if sys.platform != 'darwin':
            parser.error('launchd requires macOS; use run on other systems')
        if not binary.is_file() or not model_valid(model):
            parser.error('Run install first')
        plist.parent.mkdir(parents=True, exist_ok=True)
        content = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(Path(__file__).resolve()),
                   'run', '--home', str(home), '--port', str(args.port)],
                   'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
                   'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
        if plist.exists() and plistlib.loads(plist.read_bytes()) != content:
            parser.error('Existing service configuration differs; stop and review the plist before replacing it')
        plist.write_bytes(plistlib.dumps(content))
        plist.chmod(0o600)
        check = subprocess.run(['launchctl','print',f'{domain}/{LABEL}'],capture_output=True)
        if check.returncode:
            run('launchctl','bootstrap',domain,str(plist))
        print('Service registered. Use status; first Metal initialization can take tens of seconds.')
    elif args.action == 'stop':
        run('launchctl','bootout',f'{domain}/{LABEL}')
        print('Stopped. The installed model is retained.')
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f'http://127.0.0.1:{args.port}/health',timeout=3) as response:
                ready = response.status == 200
        except OSError:
            ready = False
        print('ready' if ready else 'unavailable or loading')
        if not ready:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
