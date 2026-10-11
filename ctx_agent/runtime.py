"""Runtime settings and the trusted storage boundary."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
MAX_SOURCE_FILE = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_TOOL_OUTPUT = 12000
DENY_NAMES = frozenset({
    ".git", ".ssh", ".gnupg", ".env", ".netrc", ".npmrc", ".pypirc",
    ".aws", ".azure", ".kube", ".config", "__pycache__", ".venv",
    "node_modules", ".direnv", "result", ".tox", ".pytest_cache",
    "credentials.json", "secrets.json", "token.json", "logins.json",
    "key4.db", "id_rsa", "id_ed25519", ".password-store", "vault",
})
DENY_SUFFIXES = (".pem", ".key", ".kdbx", ".p12", ".pfx")
DENY_PREFIXES = (".env.",)
ALLOWED_SUFFIXES = (".py", ".rs", ".js", ".ts", ".tsx", ".jsx", ".json",
                    ".toml", ".yaml", ".yml", ".md", ".txt", ".nix",
                    ".sh", ".css", ".html", ".c", ".h", ".cpp", ".go",
                    ".java", ".rb", ".xml", ".sql", ".gml", ".gitignore")


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name))


def sensitive(name: str) -> bool:
    s = name.lower()
    return (s in DENY_NAMES or any(s.startswith(x) for x in DENY_PREFIXES)
            or any(s.endswith(x) for x in DENY_SUFFIXES))


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    socket_path: Path
    projects_dir: Path
    ollama_url: str = "http://127.0.0.1:11434/api/chat"
    model: str = "qwen3:8b"
    max_steps: int = 12
    worker_timeout: int = 90
    socket_group: str = "ctx-agent-users"
    bubblewrap: str = "bwrap"
    python: str = "/run/current-system/sw/bin/python3"
    workspaces_dir: Path | None = None
    checkpoints_dir: Path | None = None
    codex_bridge_socket: Path | None = None
    ssh_bridge_socket: Path | None = None

    @classmethod
    def load(cls, filename: str | os.PathLike) -> "Settings":
        d = json.loads(Path(filename).read_text())
        required = ("state_dir", "socket_path", "projects_dir")
        if any(not isinstance(d.get(k), str) or not d[k].startswith("/") for k in required):
            raise ValueError("Missing absolute state_dir/socket_path/projects_dir")
        # Explicit loopback only: an LLM prompt must not change destination.
        url = d.get("ollama_url", "http://127.0.0.1:11434/api/chat")
        if url not in ("http://127.0.0.1:11434/api/chat",
                       "http://localhost:11434/api/chat"):
            raise ValueError("Ollama endpoint must be local")
        model = d.get("model", "qwen3:8b")
        if not isinstance(model, str) or len(model) > 80 or not model:
            raise ValueError("Invalid model")
        max_steps = d.get("max_steps", 12)
        worker_timeout = d.get("worker_timeout", 90)
        if type(max_steps) is not int or not 1 <= max_steps <= 32:
            raise ValueError("max_steps out of bounds")
        if type(worker_timeout) is not int or not 5 <= worker_timeout <= 180:
            raise ValueError("worker_timeout out of bounds")
        state_dir = Path(d["state_dir"])
        workspaces = d.get("workspaces_dir")
        checkpoints = d.get("checkpoints_dir")
        bridge = d.get("codex_bridge_socket")
        ssh_bridge = d.get("ssh_bridge_socket")
        for name, value in (
            ("workspaces_dir", workspaces),
            ("checkpoints_dir", checkpoints),
            ("codex_bridge_socket", bridge),
            ("ssh_bridge_socket", ssh_bridge),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.startswith("/")
            ):
                raise ValueError(f"{name} must be absolute")
        return cls(*(Path(d[k]) for k in required), ollama_url=url,
                   model=model, max_steps=max_steps,
                   worker_timeout=worker_timeout,
                   socket_group=d.get("socket_group", "ctx-agent-users"),
                   bubblewrap=d.get("bubblewrap", "bwrap"),
                   python=d.get("python", "/run/current-system/sw/bin/python3"),
                   workspaces_dir=Path(workspaces) if workspaces else state_dir / "workspaces",
                   checkpoints_dir=Path(checkpoints) if checkpoints else state_dir / "checkpoints",
                   codex_bridge_socket=Path(bridge) if bridge else None,
                   ssh_bridge_socket=Path(ssh_bridge) if ssh_bridge else None)
