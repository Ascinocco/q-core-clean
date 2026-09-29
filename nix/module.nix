# services.q-core: the API (with the MCP server it serves), the loopback
# Whisper server and the nightly database backup, on a NixOS host.
#
# Exposure is unchanged from the decisions log's "q-core split"
# entry: TCP on 127.0.0.1 only, plus the Serve socket in a 0700 runtime
# directory that only tailscaled (root) and the service user can reach.
# Tailscale Serve itself, its access rules, the sops secrets and restic live in
# the host's system flake, not here.
self:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.q-core;
  inherit (lib) mkOption mkEnableOption mkIf types optionalAttrs;

  env = {
    Q_CORE_DB_PATH = "${cfg.dataDir}/q-core.db";
    Q_CORE_DOCUMENTS_DIR = "${cfg.dataDir}/documents";
    Q_CORE_JYRA_DIR = "${cfg.dataDir}/jyra";
    Q_CORE_EVAL_SUMMARIES_DIR = "${cfg.dataDir}/evals/published";
    Q_CORE_FORECAST_DIR = "${cfg.dataDir}/forecast";
    Q_CORE_LOGS_DIR = "${cfg.dataDir}/logs";
    Q_CORE_PRIVACY_PROFILE_PATH = "${cfg.dataDir}/privacy/redaction.json";
    Q_CORE_ATTACHMENT_ROOTS = builtins.toJSON [ "${cfg.dataDir}/attachments-inbox" ];
    Q_CORE_SECRETS_DIR = "${cfg.dataDir}/secrets";
    Q_CORE_DATA_DIR = cfg.dataDir;
    Q_CORE_GOOGLE_TOKEN_STORE = "file";
    Q_CORE_INTAKE_DIR = cfg.intakeDir;
    Q_CORE_INBOX_DIR = cfg.inboxDir;
    Q_CORE_PORT = toString cfg.port;
    Q_CORE_SERVE_SOCKET = "/run/q-core/serve.sock";
    Q_CORE_UI_ALLOWED_LOGINS = builtins.toJSON cfg.uiAllowedLogins;
    Q_CORE_TIMEZONE = cfg.timezone;
    Q_CORE_TRANSCRIPTION_ENABLED = lib.boolToString cfg.whisper.enable;
    Q_CORE_WHISPER_PORT = toString cfg.whisper.port;
    Q_CORE_TRANSCRIPTION_FFMPEG = "${pkgs.ffmpeg-headless}/bin/ffmpeg";
    PYTHONDONTWRITEBYTECODE = "1";
  } // optionalAttrs (cfg.telemetry.otlpEndpoint != null) {
    # api/telemetry.py: traces and logs over OTLP/HTTP, allowlisted fields only.
    OTEL_EXPORTER_OTLP_ENDPOINT = cfg.telemetry.otlpEndpoint;
    OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf";
    OTEL_SERVICE_NAME = "q-core";
    OTEL_RESOURCE_ATTRIBUTES = "service.version=${self.shortRev or self.dirtyShortRev or "unknown"},deployment.environment=${cfg.telemetry.environment}";
  } // optionalAttrs (cfg.environmentFile != null) {
    Q_CORE_ENVIRONMENT_FILE = toString cfg.environmentFile;
  } // optionalAttrs (cfg.serveHostname != null) {
    Q_CORE_SERVE_HOSTNAME = cfg.serveHostname;
  };

  exports = lib.concatStringsSep "\n"
    (lib.mapAttrsToList (name: value: "export ${name}=${lib.escapeShellArg value}") env);

  # What a person (via sudo, as the service user) or the plugin's helper runs:
  # the service's environment and secrets, and only these subcommands.
  cli = pkgs.writeShellScriptBin "q-core-cli" ''
    set -eu
    if [ "$(${pkgs.coreutils}/bin/id -un)" != ${lib.escapeShellArg cfg.user} ]; then
      echo '{"error":"usage","message":"run as the service user: sudo -u ${cfg.user} q-core-cli ..."}' >&2
      exit 2
    fi
    ${exports}
    ${lib.optionalString (cfg.environmentFile != null) ''
      . ${./env-reader.sh}
      read_env_file ${lib.escapeShellArg cfg.environmentFile}
    ''}
    cd ${cfg.appDir}
    command=''${1-}
    [ $# -gt 0 ] && shift
    case "$command" in
      backup) exec .venv/bin/python -m api.backup "$@" ;;
      intake-receive) exec .venv/bin/python -m api.intake_receive "$@" ;;
      tokens) exec .venv/bin/python -m api.tokens "$@" ;;
      *) echo '{"error":"usage","message":"q-core-cli backup | intake-receive NAME ... | tokens ..."}' >&2; exit 2 ;;
    esac
  '';

  hardening = {
    User = cfg.user;
    Group = cfg.group;
    UMask = "0077";
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
  };
in
{
  options.services.q-core = {
    enable = mkEnableOption "q-core, the personal-services API and MCP server";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.q-core;
      defaultText = lib.literalExpression "q-core.packages.\${system}.q-core";
      description = "The app: source plus its offline virtualenv.";
    };

    user = mkOption { type = types.str; default = "q-core"; };
    group = mkOption { type = types.str; default = "q-core"; };

    stateDir = mkOption {
      type = types.path;
      default = "/srv/q-core";
      description = ''
        Holds `app` (a symlink to the current build, replaced on every
        activation), `data`, `intake` and `inbox`. On the server it's on the
        snapshotted ZFS work dataset.
      '';
    };

    dataDir = mkOption {
      type = types.path;
      default = "${cfg.stateDir}/data";
      defaultText = lib.literalExpression ''"''${stateDir}/data"'';
      description = "Database, documents, secrets, logs and backups. Mode 0700.";
    };

    appDir = mkOption {
      type = types.path;
      default = "${cfg.stateDir}/app";
      defaultText = lib.literalExpression ''"''${stateDir}/app"'';
      readOnly = true;
      description = "Where the current build is linked; contains `.venv/bin/python`.";
    };

    intakeDir = mkOption {
      type = types.path;
      default = "${cfg.stateDir}/intake";
      defaultText = lib.literalExpression ''"''${stateDir}/intake"'';
    };

    inboxDir = mkOption {
      type = types.path;
      default = "${cfg.stateDir}/inbox";
      defaultText = lib.literalExpression ''"''${stateDir}/inbox"'';
    };

    port = mkOption {
      type = types.port;
      default = 8420;
      description = "The loopback TCP port. Nothing here binds anything but 127.0.0.1.";
    };

    serveHostname = mkOption {
      type = types.nullOr (types.strMatching "^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+ts\\.net$");
      default = null;
      example = "server.tailnet-name.ts.net";
      description = "The Tailscale Serve hostname; also sets the Google OAuth redirect.";
    };

    uiAllowedLogins = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "alice@github" ];
      description = ''
        Tailscale logins allowed to open the browser pages through Serve.
        Empty: no identity is accepted, tokens only.
      '';
    };

    timezone = mkOption { type = types.str; default = "UTC"; };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/rendered/q-core.env";
      description = ''
        KEY=value lines read by the API, the backup and q-core-cli, never put
        in the Nix store: Q_CORE_API_TOKEN (required) and optionally
        Q_CORE_TRANSCRIPTION_TOKEN, Q_CORE_GOOGLE_OAUTH_CLIENT_ID and
        Q_CORE_GOOGLE_OAUTH_CLIENT_SECRET.
      '';
    };

    onFailure = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "notify-failure@%n.service" ];
      description = "Units started when any q-core unit fails (the host's alerting).";
    };

    cliUsers = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "alice" ];
      description = ''
        Login users allowed `sudo -n -u q-core q-core-cli ...` without a
        password: the plugin's backup and intake push, and token management.
      '';
    };

    telemetry = {
      otlpEndpoint = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:4318";
        description = ''
          OTLP/HTTP endpoint for traces and logs (the server: its local Alloy).
          Null, the default: no telemetry. Only allowlisted fields leave the
          process; see api/telemetry.py and runbooks/observability.md.
        '';
      };
      environment = mkOption {
        type = types.str;
        default = "prod";
        description = "The `deployment.environment` resource attribute.";
      };
    };

    whisper = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Run whisper-server on loopback and enable /transcriptions.";
      };
      port = mkOption { type = types.port; default = 18542; };
      model = mkOption {
        type = types.path;
        default = pkgs.fetchurl {
          url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin";
          sha256 = "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d";
        };
        defaultText = "ggml-small.en.bin, fetched by hash";
      };
      threads = mkOption { type = types.ints.positive; default = 4; };
    };

    backup = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          A nightly WAL-safe copy into dataDir/backups. It keeps every copy
          from the last 7 days (on-demand pre-import copies included) and the
          newest of each of the last 4 ISO weeks. Host backups (restic) should
          include that directory and exclude the live database files.
        '';
      };
      calendar = mkOption { type = types.str; default = "*-*-* 02:30:00"; };
    };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
    };
    users.groups.${cfg.group} = { };

    environment.systemPackages = [ cli ];

    security.sudo.extraRules = mkIf (cfg.cliUsers != [ ]) [{
      users = cfg.cliUsers;
      runAs = cfg.user;
      commands = [{
        command = "/run/current-system/sw/bin/q-core-cli";
        options = [ "NOPASSWD" ];
      }];
    }];

    systemd.tmpfiles.settings."10-q-core" = {
      ${cfg.stateDir}.d = { user = cfg.user; group = cfg.group; mode = "0755"; };
      ${cfg.appDir}."L+" = { argument = "${cfg.package}"; };
      ${cfg.dataDir}.d = { user = cfg.user; group = cfg.group; mode = "0700"; };
      "${cfg.dataDir}/attachments-inbox".d = { user = cfg.user; group = cfg.group; mode = "0700"; };
      ${cfg.intakeDir}.d = { user = cfg.user; group = cfg.group; mode = "0700"; };
      ${cfg.inboxDir}.d = { user = cfg.user; group = cfg.group; mode = "0700"; };
    };

    systemd.services.q-core-api = {
      description = "q-core API and MCP server";
      onFailure = cfg.onFailure;
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "systemd-tmpfiles-setup.service" ]
        ++ lib.optional cfg.whisper.enable "q-core-whisper.service";
      wants = lib.optional cfg.whisper.enable "q-core-whisper.service";
      environment = env;
      # A new build is a new store path behind the same app symlink.
      restartTriggers = [ cfg.package ];
      serviceConfig = hardening // {
        ExecStart = "${cfg.appDir}/.venv/bin/python -m api.run";
        WorkingDirectory = cfg.appDir;
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        # /run/q-core holds serve.sock; api/run.py refuses anything but 0700.
        RuntimeDirectory = "q-core";
        RuntimeDirectoryMode = "0700";
        ReadWritePaths = [ cfg.dataDir cfg.intakeDir cfg.inboxDir ];
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    systemd.services.q-core-whisper = mkIf cfg.whisper.enable {
      description = "q-core Whisper server (loopback only)";
      onFailure = cfg.onFailure;
      wantedBy = [ "multi-user.target" ];
      serviceConfig = hardening // {
        ExecStart = lib.escapeShellArgs [
          "${pkgs.whisper-cpp}/bin/whisper-server"
          "--host" "127.0.0.1"
          "--port" (toString cfg.whisper.port)
          "-m" cfg.whisper.model
          "-t" (toString cfg.whisper.threads)
          "-sns" "-nt"
        ];
        # Transcripts would otherwise reach the journal.
        StandardOutput = "null";
        StandardError = "null";
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    systemd.services.q-core-backup = mkIf cfg.backup.enable {
      description = "q-core nightly database backup";
      onFailure = cfg.onFailure;
      after = [ "q-core-api.service" ];
      environment = env;
      serviceConfig = hardening // {
        Type = "oneshot";
        ExecStart = "${cfg.appDir}/.venv/bin/python -m api.backup --rotate";
        WorkingDirectory = cfg.appDir;
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    systemd.timers.q-core-backup = mkIf cfg.backup.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.backup.calendar;
        Persistent = true;
      };
    };
  };
}
