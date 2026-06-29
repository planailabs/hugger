{
  description = "hugger — a HuggingFace model archiver (web UI + browser extension)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    gitlab-incus-image.url = "git+https://git.mkg20001.io/mkg20001/gitlab-incus-image.git";
    xzar.url = "github:mkg20001/xzar";
    xzar.inputs.nixpkgs.follows = "nixpkgs";

    # Build the app straight from uv.lock so the store gets the exact pinned
    # versions (hf 1.21 / hf-xet 1.5.1) rather than nixpkgs' older ones — the
    # resumable-Xet path needs hf_xet's byte-range streaming API.
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, flake-utils, gitlab-incus-image, xzar
            , pyproject-nix, uv2nix, pyproject-build-systems }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        lib = nixpkgs.lib;
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
      in
      let
        # uv2nix: load uv.lock, prefer prebuilt wheels (so hf-xet's manylinux
        # wheel is patched in, not compiled from Rust), then build a venv that
        # carries the `hugger` entry point.
        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        pyprojectOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        pyprojectOverrides = _final: prev: {
          hugger = prev.hugger.overrideAttrs (old: {
            src = lib.cleanSourceWith {
              src = ./.;
              filter = path: _type:
                let b = baseNameOf path;
                in !(builtins.elem b [ ".venv" "result" ".hugger" ]);
            };
          });
        };
        pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope
          (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            pyprojectOverlay
            pyprojectOverrides
          ]);
        hugger = (pythonSet.mkVirtualEnv "hugger-env" workspace.deps.default).overrideAttrs (old: {
          meta = (old.meta or { }) // {
            mainProgram = "hugger";
            description = "A HuggingFace model archiver (web UI + browser extension)";
          };
        });
        # Same uv2nix env plus the `test` group (selenium) — for running the test
        # suite directly. Browsers/drivers come from nixpkgs alongside it.
        testEnv = pythonSet.mkVirtualEnv "hugger-test-env" workspace.deps.all;
      in
      {
        packages = {
          default = hugger;
        } // nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          # OCI image built with nix's native dockerTools — no Dockerfile/daemon.
          docker = pkgs.dockerTools.buildLayeredImage {
            name = "hugger";
            tag = "latest";
            contents = [ hugger pkgs.cacert pkgs.dockerTools.fakeNss ];
            extraCommands = ''
              mkdir -p tmp data && chmod 1777 tmp
            '';
            config = {
              Entrypoint = [ (pkgs.lib.getExe hugger) ];
              WorkingDir = "/data";
              Env = [
                "HUGGER_HOME=/data"
                "HUGGER_HOST=0.0.0.0"
                "HUGGER_PORT=7860"
                "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              ];
              ExposedPorts = { "7860/tcp" = { }; };
              Volumes = { "/data" = { }; };
            };
          };

          # NixOS-in-Incus image for gitlab CI runners.
          image = (nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [
              "${nixpkgs}/nixos/modules/virtualisation/lxc-container.nix"
              gitlab-incus-image.nixosModules.gitlab-incus-image
              ({ pkgs, ... }: {
                environment.systemPackages = with pkgs; [
                  openssh
                  rsync
                  pkgs.xzar-client
                  pixz
                ];

                nixpkgs.overlays = [
                  xzar.overlays.default
                ];

                programs.git.config.advice.detachedHead = false;
                system.stateVersion = "26.11";

                nix.settings = {
                  substituters = [
                    "https://xzar.plan.ai"
                  ];
                  trusted-public-keys = [
                    "xzar.plan.ai:KUE66pjr6UX5HHCn9kedN1DJ2J5nSlBrKmE7tUjXewE="
                  ];
                };
              })
            ];
          }).config.system.build.gitlab-incus-image;
        };

        # VM integration tests (Linux only — they boot a NixOS guest).
        checks = nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          vm = import ./nixos/test.nix { inherit pkgs self; };
          browser = import ./nixos/browser_test.nix { inherit pkgs self; };
          docker = import ./nixos/docker_test.nix { inherit pkgs self; };
        };

        # `nix develop` — Python deps are managed by uv against this pinned
        # interpreter (uv won't download its own Python).
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.git
            pkgs.skopeo  # for docker-push.sh (copy the image to the registry)
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
            testEnv
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

        # `nix develop .#publish` — tools to build + upload the extension to the
        # Chrome, Firefox, and Edge stores via scripts/publish.sh.
        devShells.publish = pkgs.mkShell {
          packages = [ pkgs.jq pkgs.zip pkgs.curl pkgs.web-ext pkgs.nodejs ];
          shellHook = ''
            echo "hugger publish shell — scripts/publish.sh [all|chrome|firefox|edge] [--build-only]"
          '';
        };

        # `nix run .#test` — run the full test suite (unit + both VM E2E tests).
        apps.test = {
          type = "app";
          program = toString (pkgs.writeShellScript "hugger-test-all" ''
            export PATH="${testEnv}/bin:${pkgs.jq}/bin:${pkgs.curl}/bin:${pkgs.zip}/bin:$PATH"
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
      # Wire the module's package to the uv2nix build for the host's system, so
      # NixOS deployments get the same hf 1.21 / hf-xet 1.5.1 (resumable Xet) as
      # the docker image. mkDefault so users can still override.
      nixosModules.default = { pkgs, lib, ... }: {
        imports = [ ./nixos/module.nix ];
        services.hugger.package = lib.mkDefault self.packages.${pkgs.system}.default;
      };
    };
}
