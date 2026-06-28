"""Thin wrappers over huggingface_hub: search, metadata, download, update check."""
from __future__ import annotations

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from . import store
from .config import ARCHIVE_DIR, cfg

_HF_TOKEN_KEY = "hf_token"


def current_hf_token() -> str | None:
    """Effective HF token: the one set in the UI (DB) wins, else the env var."""
    return store.get_setting(_HF_TOKEN_KEY) or cfg.hf_token


def set_hf_token(token: str | None) -> None:
    if token:
        store.set_setting(_HF_TOKEN_KEY, token)
    else:
        store.delete_setting(_HF_TOKEN_KEY)


def hf_token_source() -> str:
    """For display: where the effective token comes from."""
    if store.get_setting(_HF_TOKEN_KEY):
        return "ui"
    if cfg.hf_token:
        return "env"
    return "none"


def _api() -> HfApi:
    return HfApi(token=current_hf_token())


def search_models(query: str, limit: int = 25) -> list[dict]:
    if not query.strip():
        return []
    models = _api().list_models(
        search=query, limit=limit, sort="downloads", direction=-1
    )
    out = []
    for m in models:
        out.append(
            {
                "id": m.id,
                "downloads": getattr(m, "downloads", None) or 0,
                "likes": getattr(m, "likes", None) or 0,
                "last_modified": str(getattr(m, "last_modified", "") or ""),
            }
        )
    return out


def repo_meta(repo_id: str, revision: str = "main") -> dict:
    """Commit sha + total download size (bytes) for a model repo."""
    info = _api().model_info(repo_id, revision=revision, files_metadata=True)
    total = sum((s.size or 0) for s in (info.siblings or []))
    return {"sha": info.sha, "total_size": total, "n_files": len(info.siblings or [])}


def remote_sha(repo_id: str, revision: str = "main") -> str:
    return _api().model_info(repo_id, revision=revision).sha


def local_path(repo_id: str) -> Path:
    # repo_id "org/name" -> <archives>/org/name, cross-platform.
    return ARCHIVE_DIR.joinpath(*repo_id.split("/"))


def download(repo_id: str, revision: str, dest: Path) -> str:
    """Download the full repo snapshot into `dest`. Returns the path."""
    return snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(dest),
        token=current_hf_token(),
    )
