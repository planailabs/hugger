# hf_xet 1.7.0 built from source with nix/patches/hf-xet-transfer-progress.patch,
# which exposes per-stream network transfer counters (transfer_bytes /
# transfer_bytes_completed) on ItemProgressReport — hugger uses them to show a
# true wire rate. Drop this (and the flake's hf-xet src override) once an
# upstream hf_xet release ships the equivalent change.
#
# Output: a directory holding the single built wheel; `passthru.wheelName` is
# its deterministic filename (maturin --compatibility linux + abi3-py38).
#
# This is a hand-rolled stdenv cargo build (not buildRustPackage), so nixpkgs'
# cargoSetupHook would still apply its "use system libraries" policy to every
# crate: since nixpkgs 26.11 it exports AWS_LC_SYS_USE_SYSTEM=1 unconditionally
# (cargoUseSystemLibs preConfigure hook), which aws-lc-sys 0.45 then resolves
# with pkg-config. That only works for derivations that add `aws-lc` to
# buildInputs (what default-crate-overrides does for buildRustPackage): here it
# aborts with "AWS_LC_SYS_USE_SYSTEM=1 requires a system-installed AWS-LC".
# Opt out instead and let aws-lc-sys build its bundled AWS-LC statically, so the
# wheel stays self-contained — a wheel's Rust cdylib cannot carry Nix rpaths for
# a shared libcrypto-awslc, and it is installed by uv2nix from the packed wheel.
{ lib, stdenv, fetchFromGitHub, rustPlatform, cargo, rustc, maturin, python
, cmake }:

let
  version = "1.7.0";
  wheelName = "hf_xet-${version}-cp38-abi3-linux_${stdenv.hostPlatform.parsed.cpu.name}.whl";
in
stdenv.mkDerivation {
  pname = "hf-xet-wheel";
  inherit version;

  src = fetchFromGitHub {
    owner = "huggingface";
    repo = "xet-core";
    rev = "v${version}";
    hash = "sha256-7j4vClUaEKri3Uu8hv8IEqJCDar0xR8RBxPJbP1sZFg=";
  };

  patches = [ ./patches/hf-xet-transfer-progress.patch ];

  # hf_xet/ is its own cargo workspace (excluded from the repo root one) with
  # its own committed Cargo.lock — that's the one to vendor.
  cargoRoot = "hf_xet";
  cargoDeps = rustPlatform.importCargoLock {
    # Copy of hf_xet/Cargo.lock at v1.7.0 (kept in-repo so vendoring needs no
    # import-from-derivation).
    lockFile = ./hf-xet-Cargo.lock;
  };

  nativeBuildInputs = [ rustPlatform.cargoSetupHook cargo rustc maturin python cmake ];

  # Keep the wheel self-contained: build the bundled AWS-LC instead of linking
  # the system one that cargoSetupHook's AWS_LC_SYS_USE_SYSTEM=1 asks for (see
  # the header comment).
  dontCargoUseSystemLibs = true;
  # `cmake` is only needed by aws-lc-sys' build script at build time, not as the
  # stdenv CMake hook's configure phase (this tree is built by maturin).
  dontUseCmakeConfigure = true;

  buildPhase = ''
    runHook preBuild
    # cd so cargo picks up the vendored-registry config cargoSetupHook wrote
    # under $cargoRoot/.cargo/ (cargo config discovery is cwd-based).
    (cd hf_xet && maturin build --offline --release --compatibility linux \
      -m Cargo.toml -i ${python.interpreter})
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cp hf_xet/target/wheels/${wheelName} $out/
    runHook postInstall
  '';

  passthru = { inherit wheelName; };

  meta = with lib; {
    description = "hf_xet wheel with transfer-progress fields exposed on ItemProgressReport";
    homepage = "https://github.com/huggingface/xet-core";
    license = licenses.asl20;
    platforms = platforms.linux;
  };
}
