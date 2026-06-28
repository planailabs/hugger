{
  description = "hugger — a HuggingFace model archiver (web UI + browser extension)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        # Python with hugger's runtime deps + selenium, for running the test suite
        # directly (no uv/venv needed).
        testPython = pkgs.python3.withPackages (ps: with ps; [
          fasthtml huggingface-hub yoyo-migrations argon2-cffi uvicorn selenium
        ]);
      in
      {
        packages.default = pkgs.python3Packages.callPackage ./nixos/package.nix { };

        # VM integration tests (Linux only — they boot a NixOS guest).
        checks = nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          vm = import ./nixos/test.nix { inherit pkgs self; };
          browser = import ./nixos/browser_test.nix { inherit pkgs self; };
        };

        # `nix develop` — Python deps are managed by uv against this pinned
        # interpreter (uv won't download its own Python).
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.git
            # build inputs for any sdist-only wheels (argon2-cffi etc.)
            pkgs.gcc
            pkgs.libffi
          ];
          env = {
            UV_PYTHON = "${python}/bin/python";
            UV_PYTHON_DOWNLOADS = "never";
          };
          shellHook = ''
            echo "hugger dev shell — Python ${python.version} + uv $(uv --version | cut -d' ' -f2)"
            echo "  uv venv && uv pip install -e .   # install"
            echo "  uv run hugger                    # run server"
            echo "  uv run python tests/test_core.py && uv run python tests/test_api.py"
          '';
        };

        # `nix develop .#test` — everything the test suite needs: python+selenium
        # and both browsers + their drivers. `test-all` runs the whole suite.
        devShells.test = pkgs.mkShell {
          packages = [
            testPython
            pkgs.git
            pkgs.jq
            pkgs.curl
            pkgs.zip
          ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [
            pkgs.chromium
            pkgs.chromedriver
            pkgs.firefox
            pkgs.geckodriver
          ];
          shellHook = ''
            export CHROME_BIN="${pkgs.chromium}/bin/chromium"
            export CHROMEDRIVER="${pkgs.chromedriver}/bin/chromedriver"
            export FIREFOX_BIN="${pkgs.firefox}/bin/firefox"
            export GECKODRIVER="${pkgs.geckodriver}/bin/geckodriver"
            echo "hugger test shell — python+selenium, chromium & firefox ready"
            echo "  python tests/test_core.py && python tests/test_api.py   # unit + HTTP"
            echo "  ./scripts/test-all.sh                                    # everything incl. VM E2E"
          '';
        };

        # `nix run .#test` — run the full test suite (unit + both VM E2E tests).
        apps.test = {
          type = "app";
          program = toString (pkgs.writeShellScript "hugger-test-all" ''
            export PATH="${testPython}/bin:${pkgs.jq}/bin:${pkgs.curl}/bin:${pkgs.zip}/bin:$PATH"
            cd "''${HUGGER_SRC:-.}"
            exec ${pkgs.bash}/bin/bash ./scripts/test-all.sh
          '');
        };

        # `nix run` — boots the server via uv (creates/uses .venv on first run).
        apps.default = {
          type = "app";
          program = toString (pkgs.writeShellScript "hugger-run" ''
            export PATH="${pkgs.uv}/bin:${python}/bin:$PATH"
            export UV_PYTHON="${python}/bin/python"
            export UV_PYTHON_DOWNLOADS=never
            cd "''${HUGGER_SRC:-.}"
            exec uv run hugger
          '');
        };
      }) // {
      nixosModules.default = import ./nixos/module.nix;
    };
}
