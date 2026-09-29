# Deploy on NixOS

q-core runs on the home server (NixOS 26.05) through this repository's
flake. Nix builds the app, and the host's system flake imports the module and
supplies the host-specific parts. Decision background: the decisions log's
"q-core split" entry, decision 6.

## What the flake provides

- `packages.<system>.q-core`: the app. It holds `api/`, `q_core_mcp/`, `db/`
  and `financial_evals/`, plus `.venv`, an offline virtualenv built from
  `requirements.lock`. It's built for x86_64-linux and aarch64-linux.
- `nixosModules.q-core`: the `services.q-core` options below.
- `checks.<system>.vm`: a NixOS VM test of the module (see Validation).

## What the module runs

| Unit | What | Notes |
|---|---|---|
| `q-core-api` | `python -m api.run` as user `q-core` | TCP `127.0.0.1:8420`, plus the Serve socket `/run/q-core/serve.sock` in a 0700 runtime dir. `ProtectSystem=strict`; it writes only the data, intake and inbox directories. Restarts when the build changes. |
| `q-core-whisper` | `whisper-server` from nixpkgs | `127.0.0.1:18542` only. The model is fetched by hash (`ggml-small.en`). stdout/stderr go to null so transcripts stay out of the journal. |
| `q-core-backup` (+ timer) | `python -m api.backup --rotate` at 02:30 | A WAL-safe copy into `data/backups/`. It keeps every copy from the 7 days before the newest (pre-import copies included) and the newest of each of the last 4 ISO weeks. `Persistent=true` runs a missed night at the next boot. |

Paths (`stateDir`, default `/srv/q-core`, on the server's snapshotted work dataset):

- `app`: a symlink to the current build in the Nix store, replaced on every
  activation. It's read-only; nothing is written there.
- `data/` (0700): the database, documents, Jyra attachments, `secrets/`
  (the Google refresh token), `logs/`, `backups/`, `privacy/`, `forecast/`
  and `attachments-inbox/`.
- `intake/`, `inbox/` (0700).

Nothing binds beyond loopback. Publishing to the tailnet is Tailscale Serve,
configured in the host's system flake against the socket above.

## Options the host sets

```nix
services.q-core = {
  enable = true;
  serveHostname = "<server>.<tailnet>.ts.net";      # Serve URL; the Google redirect uses it
  environmentFile = config.sops.templates."q-core.env".path;
  cliUsers = [ "admin" ];
  uiAllowedLogins = [ "<your tailscale login>" ];   # browser pages via Serve; empty = tokens only
  onFailure = [ "notify-failure@%n.service" ];         # the host's alerting
  telemetry.otlpEndpoint = "http://127.0.0.1:4318";    # the server's Alloy; traces and logs, allowlisted
};
```

Settings use the `Q_CORE_` prefix. `environmentFile` holds secrets, never the Nix store: `Q_CORE_API_TOKEN`
(required), `Q_CORE_TRANSCRIPTION_TOKEN`, `Q_CORE_GOOGLE_OAUTH_CLIENT_ID`
and `Q_CORE_GOOGLE_OAUTH_CLIENT_SECRET`. Everything else is set by the module
(the `env` block in `nix/module.nix`). Other options: `port`, `timezone`,
`whisper.{enable,port,model,threads}` and `backup.{enable,calendar}`.

## Running commands as the service

The API's settings come from the unit's environment, which a login shell
doesn't have. `q-core-cli` carries it, and runs only as the service user:

```bash
sudo -u q-core q-core-cli backup
sudo -u q-core q-core-cli tokens create laptop --scope full
sudo -u q-core q-core-cli intake-receive NAME --expect-files N --expect-bytes B   # from the plugin
```

`cliUsers` get this without a password (`sudo -n`), which the plugin's
`q-core-server.sh` relies on. When `q-core-cli` is on the PATH, it uses
`sudo -n -u q-core q-core-cli` for `backup` and `push`. Otherwise it uses the
checkout's `.venv`, as on a Mac.

## Backups on the host

Restic (configured on the host) should include `data/` but **exclude the
live `q-core.db`, `-wal` and `-shm`**. A copied live database can be torn;
`data/backups/` holds consistent copies. The timer runs at 02:30, so schedule
restic after it. The Google refresh token in `data/secrets/` is included
(The owner's decision, 2026-09-25; restic is client-side encrypted).

## Deploying a change

1. Merge to q-core `main`.
2. In the host's system flake, run `nix flake update q-core`, then `nixos-rebuild switch`.
   Activation relinks `app` and restarts `q-core-api`. Migrations run at
   startup, as they do on the Mac.

Roll back with `nixos-rebuild switch --rollback` (or the previous
generation). A migration that already ran is not undone. Restore from
`data/backups/` if needed.

## Changing Python dependencies

`api/requirements.txt` states floors. `requirements.lock` pins exact versions
with hashes for every platform:

```bash
uv pip compile --universal --generate-hashes --python-version 3.13 \
  api/requirements.txt -o requirements.lock
```

Then update `nix/wheelhouse-hashes.nix`. The wheelhouse is a fixed-output
derivation (it downloads), so its hash changes with the lock:

1. Set each system's entry to `"sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="`.
2. Run `nix build .#packages.<system>.q-core` on each system.
3. Copy the `got:` hash into the file.

CI builds x86_64-linux, so a stale x86_64 hash fails there. The aarch64 hash
is checked only by building on an aarch64 machine, for example the `nixos/nix`
arm64 container on an Apple-silicon Mac. Only binary wheels are
used (`--only-binary=:all:`). A dependency without a Linux wheel for cp313
fails the build rather than compiling from source.

For local development, sync your venv from the same lock:
`uv pip sync --require-hashes requirements.lock`, then add
`api/requirements-dev.txt`.

## Validation

`nix flake check` builds the package and runs `nix/test.nix` in a VM. The test
checks:

- `/health` answers with the token and gives 401 without it;
- only loopback listeners exist;
- the socket directory is 0700, another user gets EACCES and root (as
  tailscaled) connects;
- the data directory is 0700 and the app is a store path;
- start-up is logged as JSON in `data/logs/api.log` (the app logs there, not
  to the journal; `journalctl -u q-core-api` has only systemd's lines and
  anything before logging starts);
- a restart comes back;
- traces and logs reach an OTLP endpoint (a fake receiver), and `api.log`
  lines carry the trace id;
- Whisper answers (with the tiny model);
- a `cliUsers` member backs up through the plugin helper, while a non-member
  and an unknown subcommand are refused;
- the backup timer is armed and a run rotates;
- everything is back after a reboot.

GitHub Actions runs it on x86_64 with KVM (`.github/workflows/nix.yml`).
Locally on an Apple-silicon Mac, it runs in a `nixos/nix` arm64 container
without KVM. That's slow (QEMU emulation) but works.
