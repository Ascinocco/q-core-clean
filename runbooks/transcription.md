# Local dictation for a mobile SSH client

`POST /transcriptions` is an optional, authenticated raw-audio endpoint. It returns
`{"text":"…","duration_seconds":7.4}`; it never sends a prompt to Claude.
The mobile client must present the result for editing and require explicit Send.

## Install and operate

On the target Mac, install `cmake` and `ffmpeg` with Homebrew. From an accepted,
stable q-core checkout and its Python environment:

```sh
python scripts/whisper_service.py install
python scripts/whisper_service.py start
python scripts/whisper_service.py status
python scripts/whisper_service.py stop
```

Install pins whisper.cpp revision `a44e07845931421bb6f3447ce0010ed9dc76a118`
and downloads `ggml-small.en.bin`, verified against SHA-256
`c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d`.
The source, native build and model live outside Git under
`~/Library/Application Support/q-core/whisper`. Both upstream code and the
OpenAI Whisper model are MIT-licensed; retain their license notices.

`start` registers the per-user `com.q-core.whisper` launch agent. It keeps the
model resident, restarts after a crash, and starts at login. `stop` unloads it;
it retains the plist/model, so it can load at the next login. For persistent
disabling, stop then remove `~/Library/LaunchAgents/com.q-core.whisper.plist`.
Use `run` for foreground operation. Avoid registering a disposable task-worktree
path; launchd invokes the exact script and Python paths used at registration.
Changes to those paths/configuration require stop and explicit plist replacement.

Only `127.0.0.1:18542` is bound. Whisper's own inference/model-load handlers are
internal and unauthenticated; do not forward that port to a phone or bind it to
Tailscale. stdout/stderr go to `/dev/null`, since upstream can print transcripts.
`status` checks readiness without content. Initial Metal compilation can take
20 seconds or longer; subsequent model loads are typically much faster.

Set in the private q-core environment (never commit or print these values):

- `Q_CORE_TRANSCRIPTION_ENABLED=true`
- `Q_CORE_TRANSCRIPTION_TOKEN`: a fresh random bearer secret dedicated to the phone
- `Q_CORE_WHISPER_PORT=18542`
- `Q_CORE_TRANSCRIPTION_FFMPEG=/opt/homebrew/bin/ffmpeg` (adjust for Intel Mac)

Apply using the normal authorized API deployment procedure. The endpoint is
disabled by default and no API/service restart occurs merely by importing code.
Use a single API worker: capacity control is process-local. The main API token
also authorizes transcription, but give the phone only the scoped token. This
scoped token does not authorize financial, entity, ticket or MCP routes. Keep it
in iOS Keychain. Rotate by replacing it on Mac and phone, then restarting the API.

## Phone boundary and contract

The authenticated SSH connection carries a direct-tcpip channel to the Mac's
`127.0.0.1:8420`. The app requests only `/transcriptions` with its scoped token.
q-core and Whisper remain loopback-bound. Tailscale supplies reachability to SSH,
not public q-core routing. SSH authentication and normal terminal use remain
independent of this optional endpoint. See the deliberate architecture exception
in the decisions log; this does not authorize a public bind, webhook or tailnet
proxy for financial routes. An SSH login already has the Mac account's powers;
a scoped bearer token is an additional API boundary, not an SSH sandbox.

Send `Authorization: Bearer …`, and `Content-Type: audio/mp4` for an iOS M4A
recording or `audio/wav`. Maximum encoded body is 12 MiB; decoded duration is
120 seconds. Upload deadline is 30 seconds, normalization 20 seconds, response
wait after normalization 90 seconds. ffmpeg normalizes to 16 kHz mono PCM,
disables external MOV references and restricts input protocols. Temporary files
are private and removed after success/failure; audio and transcripts are not
written to service logs or persisted in the database.

Only one recording is processed at once. A phone cancellation or response timeout
does **not** cancel upstream Whisper inference: the API retains that slot and its
temporary normalized audio until the backend request ends, then cleans both. This
prevents abandoned calls from accumulating hidden inference requests. If Whisper
hangs, further calls stay busy; restart Whisper to close the old connection and
release the slot. Stopping the API during work can leave inference finishing in
Whisper; stop/restart Whisper too before restarting an interrupted API worker.
Do not run several API workers against this single-user service.

Errors use the normal `error.code` / `error.message` envelope: `unauthorized`
(401), `unsupported_audio` (415), `recording_too_large` / `recording_too_long`
(413), `invalid_audio` / `no_speech` (422), `transcription_busy` (429),
`transcription_unavailable` (503), `transcription_timeout` (504),
`invalid_transcription` (502), `recording_timeout` (408), and interrupted upload
(400). The app should preserve its text and allow retry; no failure is Send.

## Model choice and limitations

The default model is **small.en**, kept resident so a typical short recording
transcribes in well under a few seconds on recent hardware. No model
transcribes every technical name correctly, and whisper models can
hallucinate words on silence: the API rejects recordings with no measurable
audio before inference. This is an amplitude guard, not a speech detector;
noise can still produce words, so the editable-preview/explicit-Send contract
is essential.

## Verification

```sh
python -m pytest api/tests/test_transcription.py -q
WHISPER_TEST_BINARY=/path/to/whisper-server \
WHISPER_TEST_MODEL=/path/to/ggml-small.en.bin \
python -m pytest api/tests/test_transcription_resident.py -q -s
```

The opt-in integration test starts its own loopback service, synthesizes a coding
prompt, passes it twice through the real API and ffmpeg, verifies meaningful text
and the unchanged live process, prints cold/warm timing, and terminates its own
server. It uses temporary audio and fixture authentication, no live database or
credentials. Normal suites skip it explicitly when native assets are absent.
