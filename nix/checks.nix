# nix/checks.nix — Build-time verification tests
{ inputs, ... }: {
  perSystem = { pkgs, system, ... }:
    let
      hermes-agent = inputs.self.packages.${system}.default;
    in {
      checks = {
        # Verify binaries exist and are executable
        package-contents = pkgs.runCommand "hermes-package-contents" { } ''
          set -e
          echo "=== Checking binaries ==="
          test -x ${hermes-agent}/bin/hermes || (echo "FAIL: hermes binary missing"; exit 1)
          test -x ${hermes-agent}/bin/hermes-agent || (echo "FAIL: hermes-agent binary missing"; exit 1)
          echo "PASS: All binaries present"

          echo "=== Checking version ==="
          ${hermes-agent}/bin/hermes version 2>&1 | grep -qi "hermes" || (echo "FAIL: version check"; exit 1)
          echo "PASS: Version check"

          echo "=== All checks passed ==="
          mkdir -p $out
          echo "ok" > $out/result
        '';

        # Verify CLI subcommands are accessible
        cli-commands = pkgs.runCommand "hermes-cli-commands" { } ''
          set -e
          export HOME=$(mktemp -d)

          echo "=== Checking hermes --help ==="
          ${hermes-agent}/bin/hermes --help 2>&1 | grep -q "gateway" || (echo "FAIL: gateway subcommand missing"; exit 1)
          ${hermes-agent}/bin/hermes --help 2>&1 | grep -q "config" || (echo "FAIL: config subcommand missing"; exit 1)
          echo "PASS: All subcommands accessible"

          echo "=== All CLI checks passed ==="
          mkdir -p $out
          echo "ok" > $out/result
        '';

        # Verify HERMES_MANAGED guard works
        managed-guard = pkgs.runCommand "hermes-managed-guard" { } ''
          set -e
          export HOME=$(mktemp -d)

          echo "=== Checking HERMES_MANAGED guard ==="
          OUTPUT=$(HERMES_MANAGED=true ${hermes-agent}/bin/hermes config set model foo 2>&1 || true)
          echo "$OUTPUT" | grep -q "managed by NixOS" || (echo "FAIL: managed guard not working"; echo "$OUTPUT"; exit 1)
          echo "PASS: Managed guard blocks config mutation"

          echo "=== All guard checks passed ==="
          mkdir -p $out
          echo "ok" > $out/result
        '';
      };
    };
}
