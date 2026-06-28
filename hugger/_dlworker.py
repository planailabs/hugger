"""Subprocess entry point for a single model download.

Run as: python -m hugger._dlworker <repo_id> <revision> <dest_dir>

The set of files to fetch is read from <dest_dir>/.hugger.json ("selected"), so a
selective download resumes correctly. Running in its own process lets the job
manager pause it by terminating the process; huggingface_hub leaves a resumable
partial on disk, so a later run continues where it left off.
"""
import sys
from pathlib import Path

from . import hub, metadata


def main() -> int:
    repo_id, revision, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    meta = metadata.read(dest)
    allow = meta.get("selected") if meta else None
    hub.download(repo_id, revision, Path(dest), allow_patterns=allow)
    return 0


if __name__ == "__main__":
    sys.exit(main())
