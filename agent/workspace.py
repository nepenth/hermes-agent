from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.config import get_hermes_home, load_config

DEFAULT_WORKSPACE_SUBDIRS = ("docs", "notes", "data", "code", "uploads", "media")
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".xz", ".7z", ".mp3", ".wav", ".ogg", ".mp4",
    ".mov", ".avi", ".sqlite", ".db", ".bin", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".otf",
}


@dataclass
class WorkspacePaths:
    workspace_root: Path
    knowledgebase_root: Path
    indexes_dir: Path
    manifests_dir: Path
    cache_dir: Path
    manifest_path: Path


@dataclass
class WorkspaceEntry:
    relative_path: str
    size_bytes: int
    modified_at: str
    mime_type: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return config if config is not None else load_config()


def _resolve_root(raw_path: str | None, fallback_name: str) -> Path:
    if raw_path:
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        return Path(expanded).resolve()
    return (get_hermes_home() / fallback_name).resolve()


def get_workspace_paths(config: dict[str, Any] | None = None, ensure: bool = False) -> WorkspacePaths:
    cfg = _ensure_config(config)
    workspace_cfg = cfg.get("workspace", {}) or {}
    kb_cfg = cfg.get("knowledgebase", {}) or {}

    workspace_root = _resolve_root(workspace_cfg.get("path"), "workspace")
    knowledgebase_root = _resolve_root(kb_cfg.get("path"), "knowledgebase")
    indexes_dir = knowledgebase_root / "indexes"
    manifests_dir = knowledgebase_root / "manifests"
    cache_dir = knowledgebase_root / "cache"
    manifest_path = manifests_dir / "workspace.json"

    if ensure:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for subdir in DEFAULT_WORKSPACE_SUBDIRS:
            (workspace_root / subdir).mkdir(parents=True, exist_ok=True)
        knowledgebase_root.mkdir(parents=True, exist_ok=True)
        indexes_dir.mkdir(parents=True, exist_ok=True)
        manifests_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

    return WorkspacePaths(
        workspace_root=workspace_root,
        knowledgebase_root=knowledgebase_root,
        indexes_dir=indexes_dir,
        manifests_dir=manifests_dir,
        cache_dir=cache_dir,
        manifest_path=manifest_path,
    )


def _workspace_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("workspace", {}) or {}).get("enabled", True))


def _load_ignore_patterns(workspace_root: Path, include_hidden: bool = False) -> list[str]:
    patterns: list[str] = []
    ignore_file = workspace_root / ".hermesignore"
    if not include_hidden and ignore_file.exists():
        raw = ignore_file.read_text(encoding="utf-8", errors="ignore")
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                patterns.append(stripped)
    return patterns


def _is_hidden_rel(rel_path: Path) -> bool:
    return any(part.startswith(".") for part in rel_path.parts)


def _matches_ignore(rel_posix: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if fnmatch.fnmatch(rel_posix, normalized):
            return True
        if fnmatch.fnmatch(Path(rel_posix).name, normalized):
            return True
        if rel_posix.startswith(normalized + "/"):
            return True
    return False


def _iter_workspace_files(paths: WorkspacePaths, config: dict[str, Any], include_hidden: bool = False) -> Iterable[Path]:
    kb_cfg = config.get("knowledgebase", {}) or {}
    indexing_cfg = kb_cfg.get("indexing", {}) or {}
    max_file_mb = int(indexing_cfg.get("max_file_mb", 10) or 10)
    max_file_bytes = max_file_mb * 1024 * 1024
    patterns = _load_ignore_patterns(paths.workspace_root, include_hidden=include_hidden)

    for file_path in sorted(paths.workspace_root.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(paths.workspace_root)
        if rel_path.as_posix() == ".hermesignore":
            continue
        if not include_hidden and _is_hidden_rel(rel_path):
            continue
        if _matches_ignore(rel_path.as_posix(), patterns):
            continue
        try:
            if file_path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        yield file_path


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".md":
        return "text/markdown"
    if ext in {".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".rst"}:
        return "text/plain"
    return "application/octet-stream"


def _entry_for(path: Path, root: Path) -> WorkspaceEntry:
    stat_result = path.stat()
    return WorkspaceEntry(
        relative_path=path.relative_to(root).as_posix(),
        size_bytes=stat_result.st_size,
        modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc).isoformat(),
        mime_type=_mime_for(path),
    )


def build_workspace_manifest(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _ensure_config(config)
    if not _workspace_enabled(cfg):
        return {"success": False, "error": "Workspace is disabled in config."}

    paths = get_workspace_paths(cfg, ensure=True)
    entries = [_entry_for(path, paths.workspace_root) for path in _iter_workspace_files(paths, cfg)]

    payload = {
        "success": True,
        "generated_at": _utc_now_iso(),
        "workspace_root": str(paths.workspace_root),
        "knowledgebase_root": str(paths.knowledgebase_root),
        "manifest_path": str(paths.manifest_path),
        "file_count": len(entries),
        "files": [asdict(entry) for entry in entries],
    }
    paths.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def workspace_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _ensure_config(config)
    if not _workspace_enabled(cfg):
        return {"success": False, "error": "Workspace is disabled in config."}

    paths = get_workspace_paths(cfg, ensure=True)
    entries = [_entry_for(path, paths.workspace_root) for path in _iter_workspace_files(paths, cfg)]
    category_counts: dict[str, int] = {}
    for entry in entries:
        top = entry.relative_path.split("/", 1)[0]
        category_counts[top] = category_counts.get(top, 0) + 1

    return {
        "success": True,
        "workspace_root": str(paths.workspace_root),
        "knowledgebase_root": str(paths.knowledgebase_root),
        "manifest_path": str(paths.manifest_path),
        "manifest_exists": paths.manifest_path.exists(),
        "file_count": len(entries),
        "category_counts": category_counts,
        "default_subdirs": list(DEFAULT_WORKSPACE_SUBDIRS),
    }


def workspace_list(
    config: dict[str, Any] | None = None,
    relative_path: str = "",
    recursive: bool = True,
    limit: int = 100,
    offset: int = 0,
    include_hidden: bool = False,
) -> dict[str, Any]:
    cfg = _ensure_config(config)
    if not _workspace_enabled(cfg):
        return {"success": False, "error": "Workspace is disabled in config."}

    paths = get_workspace_paths(cfg, ensure=True)
    base = paths.workspace_root
    if relative_path:
        candidate = (base / relative_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return {"success": False, "error": "Requested path escapes workspace root."}
        base = candidate
        if not base.exists():
            return {"success": False, "error": f"Workspace path not found: {relative_path}"}

    entries: list[dict[str, Any]] = []
    patterns = _load_ignore_patterns(paths.workspace_root, include_hidden=include_hidden)
    iterator = base.rglob("*") if recursive else base.iterdir()
    for path in sorted(iterator):
        if not path.is_file():
            continue
        rel = path.relative_to(paths.workspace_root)
        if not include_hidden and _is_hidden_rel(rel):
            continue
        if _matches_ignore(rel.as_posix(), patterns):
            continue
        entries.append(asdict(_entry_for(path, paths.workspace_root)))

    sliced = entries[offset:offset + limit]
    return {
        "success": True,
        "workspace_root": str(paths.workspace_root),
        "base_path": str(base),
        "count": len(sliced),
        "total_count": len(entries),
        "entries": sliced,
    }


def _is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    try:
        chunk = path.read_bytes()[:1024]
    except OSError:
        return True
    return b"\x00" in chunk


def workspace_search(
    query: str,
    config: dict[str, Any] | None = None,
    relative_path: str = "",
    file_glob: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_hidden: bool = False,
) -> dict[str, Any]:
    cfg = _ensure_config(config)
    if not _workspace_enabled(cfg):
        return {"success": False, "error": "Workspace is disabled in config."}
    if not query.strip():
        return {"success": False, "error": "Query cannot be empty."}

    paths = get_workspace_paths(cfg, ensure=True)
    base = paths.workspace_root
    if relative_path:
        candidate = (base / relative_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return {"success": False, "error": "Requested path escapes workspace root."}
        base = candidate
        if not base.exists():
            return {"success": False, "error": f"Workspace path not found: {relative_path}"}

    try:
        regex = re.compile(query)
    except re.error as e:
        return {"success": False, "error": f"Invalid regex: {e}"}
    patterns = _load_ignore_patterns(paths.workspace_root, include_hidden=include_hidden)
    matches: list[dict[str, Any]] = []

    for file_path in sorted(base.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(paths.workspace_root)
        if not include_hidden and _is_hidden_rel(rel):
            continue
        if _matches_ignore(rel.as_posix(), patterns):
            continue
        if file_glob and not fnmatch.fnmatch(file_path.name, file_glob):
            continue
        if _is_probably_binary(file_path):
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "relative_path": rel.as_posix(),
                        "path": str(file_path),
                        "line": line_number,
                        "content": line,
                    }
                )

    sliced = matches[offset:offset + limit]
    return {
        "success": True,
        "query": query,
        "workspace_root": str(paths.workspace_root),
        "count": len(sliced),
        "total_count": len(matches),
        "matches": sliced,
    }
