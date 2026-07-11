# hf_xet 1.5.1 built from source with nix/patches/hf-xet-transfer-progress.patch,
# which exposes per-stream network transfer counters (transfer_bytes /
# transfer_bytes_completed) on ItemProgressReport — hugger uses them to show a
# true wire rate. Drop this (and the flake's hf-xet src override) once an
# upstream hf_xet release ships the equivalent change.
#
# Output: a directory holding the single built wheel; `passthru.wheelName` is
# its deterministic filename (maturin --compatibility linux + abi3-py37).
{ lib, stdenv, fetchFromGitHub, rustPlatform, cargo, rustc, maturin, python }:

let
  version = "1.5.1";
  wheelName = "hf_xet-${version}-cp37-abi3-linux_${stdenv.hostPlatform.parsed.cpu.name}.whl";
in
stdenv.mkDerivation {
  pname = "hf-xet-wheel";
  inherit version;

  src = fetchFromGitHub {
    owner = "huggingface";
    repo = "xet-core";
    rev = "v${version}";
    hash = "sha256-TqSErydAOaHzCN7qglO/aqMF8BWYXvEv09adhxTwny0=";
  };

  patches = [ ./patches/hf-xet-transfer-progress.patch ];

  # hf_xet/ is its own cargo workspace (excluded from the repo root one) with
  # its own committed Cargo.lock — that's the one to vendor.
  cargoRoot = "hf_xet";
  cargoDeps = rustPlatform.importCargoLock {
    # Copy of hf_xet/Cargo.lock at v1.5.1 (kept in-repo so vendoring needs no
    # import-from-derivation).
    lockFile = ./hf-xet-Cargo.lock;
  };

  nativeBuildInputs = [ rustPlatform.cargoSetupHook cargo rustc maturin python ];

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
