# The module on a booted NixOS VM: `nix flake check` runs this.
{ pkgs, self }:
pkgs.testers.runNixOSTest {
  name = "q-core";
  nodes.server = { pkgs, ... }: {
    imports = [ self.nixosModules.q-core ];
    virtualisation.memorySize = 2048;
    virtualisation.diskSize = 4096;
    # Without KVM (a local arm64 container) the console device can take longer
    # than systemd's 90 s default to appear. Harmless under KVM in CI.
    boot.kernelParams = [ "systemd.default_device_timeout_sec=900" ];
    users.users.alice = { isNormalUser = true; };
    users.users.bob = { isNormalUser = true; };
    # A test-only secret. On a real host this file is rendered by sops.
    environment.etc."q-core-test.env" = {
      # The second line is shell-hostile on purpose: q-core-cli must read it
      # literally, as systemd does, not source it.
      # The later lines are malformed on purpose: q-core-cli skips them with a
      # warning (nix/env-reader.sh) and still works.
      text = "Q_CORE_API_TOKEN=test-service-token-not-a-secret\nQ_CORE_TEST_NOTE=two words $x `id`\nNOEQUALS\nBAD KEY=2\n   Q_CORE_INDENTED=yes\n";
      mode = "0400";
      user = "q-core";
    };
    environment.systemPackages = [ pkgs.curl pkgs.jq pkgs.iproute2 ];
    systemd.services.fake-otlp = {
      wantedBy = [ "multi-user.target" ];
      before = [ "q-core-api.service" ];
      serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 ${./fake-otlp.py}";
    };
    # Stands in for the host's alerting: records which unit failed.
    systemd.services."failure-marker@" = {
      serviceConfig.Type = "oneshot";
      scriptArgs = "%i";
      script = ''echo "$1" >> /run/q-core-failures'';
    };
    services.q-core = {
      enable = true;
      environmentFile = "/etc/q-core-test.env";
      cliUsers = [ "alice" ];
      uiAllowedLogins = [ "alice@example" ];
      serveHostname = "q-core.tail1234.ts.net";
      onFailure = [ "failure-marker@%n.service" ];
      telemetry.otlpEndpoint = "http://127.0.0.1:4318";
      whisper.model = pkgs.fetchurl {
        url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin";
        hash = "sha256-kh5M+Ghv3Zk9zQgaXaW2w2W/3hFi5ysI11rHUomSCx8=";
      };
      whisper.threads = 1;
    };
  };

  testScript = { nodes, ... }: ''
    import json

    helper = "${../plugin/scripts/q-core-server.sh}"
    token = "test-service-token-not-a-secret"

    def health_ok():
        server.wait_for_unit("q-core-api.service")
        server.wait_for_open_port(8420, "127.0.0.1")
        server.succeed(f"curl -sf -H 'Authorization: Bearer {token}' http://127.0.0.1:8420/health")

    start_all()

    with subtest("the API answers only with a token"):
        health_ok()
        status = server.succeed("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8420/health")
        assert status == "401", status

    with subtest("nothing listens beyond loopback"):
        server.wait_for_unit("q-core-whisper.service")
        server.wait_for_open_port(18542, "127.0.0.1")
        listening = server.succeed("ss -ltnH '( sport = :8420 or sport = :18542 )'")
        for line in listening.strip().splitlines():
            assert "127.0.0.1:" in line.split()[3], line

    with subtest("the Serve socket is private to the service user"):
        server.succeed("test \"$(stat -c '%U %a' /run/q-core)\" = 'q-core 700'")
        server.succeed("test \"$(stat -c '%U %a' /run/q-core/serve.sock)\" = 'q-core 600'")
        server.fail("sudo -u alice curl -s --unix-socket /run/q-core/serve.sock http://q/health")
        server.succeed("curl -s -o /dev/null --unix-socket /run/q-core/serve.sock http://q/health")

    with subtest("a token create through the socket, as Tailscale Serve sends it"):
        # Serve proxies unix: targets with Host: localhost and the client's
        # host in X-Forwarded-Host (ticket T-46). Root stands in for
        # tailscaled, the only other process that can reach the socket.
        serve = ("-H 'Host: localhost' -H 'X-Forwarded-Host: q-core.tail1234.ts.net' "
                 "-H 'Origin: https://q-core.tail1234.ts.net' -H 'Tailscale-User-Login: alice@example' "
                 "-H 'X-Q-Core-Request: tokens' -H 'Content-Type: application/json'")
        created = server.succeed(
            f"curl -sf --unix-socket /run/q-core/serve.sock {serve} "
            "-d '{\"name\": \"vm-probe\", \"scope\": \"transcription\"}' http://localhost/ui/tokens/create"
        )
        assert json.loads(created)["token"], "no token returned"
        evil = serve.replace("q-core.tail1234.ts.net", "evil.example")
        code = server.succeed(
            f"curl -s -o /dev/null -w '%{{http_code}}' --unix-socket /run/q-core/serve.sock {evil} "
            "-d '{\"name\": \"x\"}' http://localhost/ui/tokens/create"
        )
        assert code in ("401", "403"), code

    with subtest("the data directory is closed and the app is a store path"):
        server.succeed("test \"$(stat -c '%U %a' /srv/q-core/data)\" = 'q-core 700'")
        server.succeed("readlink /srv/q-core/app | grep -q '^/nix/store/'")
        # The database is created by the first request that uses it.
        server.succeed(f"curl -sf -H 'Authorization: Bearer {token}' http://127.0.0.1:8420/entities")
        server.succeed("test \"$(stat -c '%U %a' /srv/q-core/data/q-core.db)\" = 'q-core 600'")

    with subtest("start-up is logged as JSON in the data directory"):
        line = server.succeed("grep -m1 'listening on' /srv/q-core/data/logs/api.log")
        assert json.loads(line)["message"].startswith("listening on 127.0.0.1:8420"), line

    with subtest("a restart comes back"):
        server.succeed("systemctl restart q-core-api.service q-core-whisper.service")
        health_ok()
        server.wait_for_open_port(18542, "127.0.0.1")

    with subtest("traces and logs reach the OTLP endpoint"):
        server.succeed(f"curl -sf -H 'Authorization: Bearer {token}' http://127.0.0.1:8420/entities")
        server.wait_until_succeeds("grep -qx /v1/traces /run/otlp-paths", timeout=60)
        server.wait_until_succeeds("grep -qx /v1/logs /run/otlp-paths", timeout=60)
        line = server.succeed("grep -m1 trace_id /srv/q-core/data/logs/api.log")
        assert len(json.loads(line)["trace_id"]) == 32, line

    with subtest("whisper answers on loopback"):
        server.succeed("curl -s -o /dev/null http://127.0.0.1:18542/")

    with subtest("a cliUser backs up through sudo, via the plugin helper"):
        out = server.succeed(f"sudo -u alice bash {helper} backup")
        result = json.loads(out)
        assert result["integrity"] == "ok", out
        assert result["path"].startswith("/srv/q-core/data/backups/"), out
        # The env file is read before the subcommand is checked, so a refused
        # one shows the warnings without making another backup.
        warnings = server.succeed("sudo -u alice sudo -n -u q-core q-core-cli nothing 2>&1 >/dev/null || true")
        assert warnings.count("skipped an environment-file line") == 2, warnings
        server.fail("sudo -u bob sudo -n -u q-core q-core-cli backup")
        server.fail("sudo -u alice sudo -n -u q-core q-core-cli shell")

    with subtest("the nightly timer is armed and its run rotates"):
        server.succeed("systemctl list-timers q-core-backup.timer | grep -q q-core-backup")
        # Ten invented old copies, 2025-01-01..10. Kept: today's two (within a
        # week of the newest) and the newest of the next ISO weeks, 01-10 and
        # 01-05. So eight go.
        server.succeed(
            "for d in 01 02 03 04 05 06 07 08 09 10; do "
            "sudo -u q-core touch /srv/q-core/data/backups/q-core-2025-01-''${d}T02-30-00Z.db; done"
        )
        server.succeed("systemctl start q-core-backup.service")
        server.succeed("journalctl -u q-core-backup.service | grep -q '\"removed\": 8'")
        kept = server.succeed("ls /srv/q-core/data/backups | grep '^q-core-2025' | tr '\\n' ' '").split()
        assert kept == ["q-core-2025-01-05T02-30-00Z.db", "q-core-2025-01-10T02-30-00Z.db"], kept

    with subtest("a failed unit triggers onFailure"):
        # A backup with no database to copy fails.
        server.succeed("mv /srv/q-core/data/q-core.db /srv/q-core/data/q-core.db.moved")
        server.fail("systemctl start q-core-backup.service")
        server.wait_until_succeeds("grep -q q-core-backup /run/q-core-failures")
        server.succeed("mv /srv/q-core/data/q-core.db.moved /srv/q-core/data/q-core.db")
        server.succeed("systemctl reset-failed q-core-backup.service")

    with subtest("it all comes back after a reboot"):
        server.shutdown()
        server.start()
        health_ok()
        server.wait_for_unit("q-core-whisper.service")
        server.succeed("test -S /run/q-core/serve.sock")
        count = server.succeed("ls /srv/q-core/data/backups | wc -l").strip()
        assert count == "4", count  # today's two and the two weekly ones
  '';
}
