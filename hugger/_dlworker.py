"""Subprocess entry point for a single model download.

Run as: python -m hugger._dlworker <repo_id> <revision> <dest_dir>

Running the download in its own process lets the job manager pause it by
terminating the process; huggingface_hub leaves a resumable partial on disk, so
a later run (unpause / restart) continues where it left off.
"""
import sys
from pathlib import Path

from . import hub


def main() -> int:
    repo_id, revision, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    hub.download(repo_id, revision, Path(dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
