#!/usr/bin/env python3
"""A minimal fake HuggingFace Hub — just enough of the API for hugger's tests.

Implements the endpoints huggingface_hub actually calls for `model_info`,
`list_models`, and `snapshot_download`:

  GET  /api/models                              -> search list
  GET  /api/models/{repo}[/revision/{rev}]      -> model_info (with siblings+sizes)
  HEAD /{repo}/resolve/{rev}/{file}             -> file metadata headers
  GET  /{repo}/resolve/{rev}/{file}             -> file bytes

Env:
  FAKE_HUB_HOST (default 0.0.0.0), FAKE_HUB_PORT (default 443)
  FAKE_HUB_CERT, FAKE_HUB_KEY   -> if both set, serve over TLS
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

COMMIT = "1" * 40  # fixed fake commit sha

# repo_id -> {filename: bytes}
REPOS: dict[str, dict[str, bytes]] = {
    "test-org/tiny-model": {
        "config.json": json.dumps(
            {"model_type": "tiny", "hidden_size": 8}, indent=2
        ).encode(),
        "README.md": b"# tiny-model\n\nA fake model served by the test hub.\n",
        "weights.bin": bytes(range(256)) * 4,  # 1024 deterministic bytes
    }
}


def _etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest() + '"'


def _model_info(repo: str) -> dict:
    files = REPOS[repo]
    return {
        "id": repo,
        "modelId": repo,
        "sha": COMMIT,
        "lastModified": "2024-01-01T00:00:00.000Z",
        "createdAt": "2024-01-01T00:00:00.000Z",
        "private": False,
        "gated": False,
        "disabled": False,
        "downloads": 123,
        "likes": 7,
        "tags": [],
        "siblings": [{"rfilename": n, "size": len(b)} for n, b in files.items()],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quieter logs
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve(self, path: str, head: bool):
        # /{repo}/resolve/{rev}/{filename}
        repo, _, rest = path.partition("/resolve/")
        repo = repo.strip("/")
        _, _, filename = rest.partition("/")
        filename = unquote(filename)
        files = REPOS.get(repo)
        if not files or filename not in files:
            self.send_error(404, "file not found")
            return
        data = files[filename]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", _etag(data))
        self.send_header("X-Repo-Commit", COMMIT)
        self.send_header("X-Linked-Size", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if "/resolve/" in path:
            self._resolve(path, head=True)
        else:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if "/resolve/" in path:
            self._resolve(path, head=False)
            return
        if path == "/api/models":
            self._json([_model_info(r) for r in REPOS])
            return
        if path.startswith("/api/models/"):
            spec = path[len("/api/models/"):]
            repo = spec.split("/revision/")[0].strip("/")
            if repo in REPOS:
                self._json(_model_info(repo))
            else:
                self._json({"error": "Repository not found"}, status=404)
            return
        self.send_error(404, "not found")


def main():
    host = os.environ.get("FAKE_HUB_HOST", "0.0.0.0")
    port = int(os.environ.get("FAKE_HUB_PORT", "443"))
    cert = os.environ.get("FAKE_HUB_CERT")
    key = os.environ.get("FAKE_HUB_KEY")

    httpd = ThreadingHTTPServer((host, port), Handler)
    scheme = "http"
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"fake-hf-hub on {scheme}://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
