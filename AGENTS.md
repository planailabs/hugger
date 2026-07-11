# AGENTS.md — project rules for hugger

## CRITICAL: never disable HuggingFace Xet

Do **not** force Xet off anywhere in hugger's code or shipped config — i.e. do
not set `HF_HUB_DISABLE_XET=1` in the download subprocess env (`hugger/jobs.py`),
the worker (`hugger/_dlworker.py`), the Docker image (`flake.nix`), the NixOS
module, or CI. Xet is the fast default transfer and **must stay enabled**.

Download progress is synced from the hf library's own tqdm bytes bar
(`_dlworker.py` writes `.hugger.progress`; `metadata.read_progress` reads it and
also counts on-disk `.incomplete` files), so smooth progress does **not** require
forcing the classic path. If you change download/progress code, keep Xet on.

Operators may still set `HF_HUB_DISABLE_XET` themselves at runtime (env) — the
rule is only that hugger's own code/config must not force it off.

## Patched hf_xet (transfer counters)

Linux nix builds compile hf_xet 1.5.1 from source with
`nix/patches/hf-xet-transfer-progress.patch` (see `nix/hf-xet-patched.nix`),
exposing `transfer_bytes{,_completed}` on `ItemProgressReport` — hugger's
net-rate display (`.hugger.xfer` → `Job.net_rate`) reads them. All consuming
code guards with `getattr`/`hasattr`, so a stock wheel (dev venv after
`uv sync`, darwin) degrades gracefully: no net rate, everything else works.
The patch is intended for upstream (huggingface/xet-core); drop the override
and patch once a release ships the fields.

## Misc

- Tests: `python tests/test_core.py`, `tests/test_xet_ranges.py`, `tests/test_api.py`,
  `tests/test_hub_live.py`; VM/E2E via `nix build .#checks.x86_64-linux.{vm,browser,docker}`.
- Schema changes go through a new `hugger/migrations/NNNN_*.sql` (yoyo), never by
  editing existing migrations or the DB by hand.
