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
      in
      {
        packages.default = pkgs.python3Packages.callPackage ./nixos/package.nix { };

        # VM integration test (Linux only — it boots a NixOS guest).
        checks = nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          vm = import ./nixos/test.nix { inherit pkgs self; };
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
