"""Subprocess entry point for a single model download.

Run as: python -m hugger._dlworker <repo_id> <revision> <dest_dir>

The set of files to fetch is read from <dest_dir>/.hugger.json ("selected"), so a
selective download resumes correctly. Aggregate byte progress (classic and Xet)
is written to <dest_dir>/.hugger.progress via a custom tqdm so the parent process
can show smooth, transfer-accurate progress. Running in its own process also lets
the job manager pause it by terminating the process.
"""
import sys
import time
from pathlib import Path

from tqdm.auto import tqdm as base_tqdm

from . import _xet_download, hub, metadata


class _ProgressTqdm(base_tqdm):
    """The aggregate `bytes_progress` bar in snapshot_download. Every byte (from
    every file/thread, classic or Xet) flows through update(); we mirror n/total
    to a small file, throttled."""
    path = None
    _last = 0.0

    def update(self, n=1):
        r = super().update(n)
        self._flush()
        return r

    def close(self):
        self._flush(force=True)
        return super().close()

    def _flush(self, force=False):
        now = time.monotonic()
        if not force and now - _ProgressTqdm._last < 0.2:
            return
        _ProgressTqdm._last = now
        try:
            with open(_ProgressTqdm.path, "w") as f:
                f.write(f"{int(self.n)} {int(self.total or 0)}")
        except OSError:
            pass


def main() -> int:
    repo_id, revision, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    meta = metadata.read(dest)
    allow = meta.get("selected") if meta else None
    pf = metadata.progress_file(dest)
    pf.parent.mkdir(parents=True, exist_ok=True)
    try:
        pf.unlink()  # drop any stale value before a fresh run
    except OSError:
        pass
    _ProgressTqdm.path = str(pf)
    if _xet_download.enabled():
        # Xet files stream through one shared group (resume + cross-file chunk
        # reuse); the rest fall back to the classic path. Progress is tracked by
        # the parent from the filesystem (completed files + `*.xetpart`/
        # `*.incomplete`), so no tqdm hook is needed here.
        rels = allow or [f["path"] for f in (meta.get("files") or [])]
        classic = _xet_download.download_all(repo_id, revision, rels, dest, hub.current_hf_token())
        for rel in classic:
            hub.download_one(repo_id, revision, Path(dest), rel)
        return 0
    hub.download(repo_id, revision, Path(dest), allow_patterns=allow, tqdm_class=_ProgressTqdm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
