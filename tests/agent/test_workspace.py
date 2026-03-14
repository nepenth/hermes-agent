from __future__ import annotations

import json
from pathlib import Path


def _config(tmp_path: Path) -> dict:
    return {
        "workspace": {
            "enabled": True,
            "path": str(tmp_path / "workspace"),
            "auto_create": True,
            "persist_gateway_uploads": "ask",
        },
        "knowledgebase": {
            "enabled": True,
            "path": str(tmp_path / "knowledgebase"),
            "roots": [],
            "retrieval_mode": "off",
            "auto_index": True,
            "watch_for_changes": False,
            "max_injected_chunks": 6,
            "max_injected_tokens": 3200,
            "dense_top_k": 40,
            "sparse_top_k": 40,
            "fused_top_k": 30,
            "final_top_k": 8,
            "min_fused_score": 0.0,
            "injection_format": "sourced_note",
            "chunking": {
                "default_tokens": 512,
                "overlap_tokens": 80,
                "code_strategy": "structural",
                "markdown_strategy": "headings",
            },
            "embeddings": {
                "provider": "local",
                "model": "embeddinggemma-300m",
                "dimensions": 768,
            },
            "reranker": {
                "enabled": False,
                "provider": "local",
                "model": "bge-reranker-v2-m3",
            },
            "indexing": {
                "respect_gitignore": True,
                "respect_hermesignore": True,
                "include_hidden": False,
                "max_file_mb": 10,
            },
        },
    }


class TestWorkspacePaths:
    def test_get_workspace_paths_creates_expected_directories(self, tmp_path):
        from agent.workspace import get_workspace_paths

        paths = get_workspace_paths(_config(tmp_path), ensure=True)

        assert paths.workspace_root == tmp_path / "workspace"
        assert paths.knowledgebase_root == tmp_path / "knowledgebase"
        for subdir in ("docs", "notes", "data", "code", "uploads", "media"):
            assert (paths.workspace_root / subdir).is_dir()
        assert paths.indexes_dir.is_dir()
        assert paths.manifests_dir.is_dir()
        assert paths.cache_dir.is_dir()


class TestWorkspaceManifest:
    def test_build_workspace_manifest_writes_summary(self, tmp_path):
        from agent.workspace import build_workspace_manifest

        cfg = _config(tmp_path)
        workspace = Path(cfg["workspace"]["path"])
        (workspace / "docs").mkdir(parents=True)
        (workspace / "notes").mkdir(parents=True)
        (workspace / "docs" / "a.md").write_text("alpha\n", encoding="utf-8")
        (workspace / "notes" / "b.txt").write_text("beta\n", encoding="utf-8")

        manifest = build_workspace_manifest(cfg)

        assert manifest["success"] is True
        assert manifest["file_count"] == 2
        assert manifest["manifest_path"].endswith("workspace.json")
        assert Path(manifest["manifest_path"]).exists()
        paths = {entry["relative_path"] for entry in manifest["files"]}
        assert paths == {"docs/a.md", "notes/b.txt"}

        saved = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
        assert saved["file_count"] == 2


class TestWorkspaceSearch:
    def test_workspace_search_finds_text_matches_and_respects_ignore(self, tmp_path):
        from agent.workspace import workspace_search

        cfg = _config(tmp_path)
        workspace = Path(cfg["workspace"]["path"])
        (workspace / "docs").mkdir(parents=True)
        (workspace / "docs" / "keep.md").write_text("Hermes likes retrieval\n", encoding="utf-8")
        (workspace / "docs" / "skip.md").write_text("Hermes hidden\n", encoding="utf-8")
        (workspace / ".hermesignore").write_text("docs/skip.md\n", encoding="utf-8")
        (workspace / "docs" / "blob.bin").write_bytes(b"\x00\x01\x02Hermes")

        result = workspace_search("Hermes", config=cfg)

        assert result["success"] is True
        assert result["count"] == 1
        match = result["matches"][0]
        assert match["relative_path"] == "docs/keep.md"
        assert match["line"] == 1

    def test_workspace_search_supports_file_glob(self, tmp_path):
        from agent.workspace import workspace_search

        cfg = _config(tmp_path)
        workspace = Path(cfg["workspace"]["path"])
        (workspace / "docs").mkdir(parents=True)
        (workspace / "docs" / "a.md").write_text("deploy target\n", encoding="utf-8")
        (workspace / "docs" / "a.txt").write_text("deploy target\n", encoding="utf-8")

        result = workspace_search("deploy", config=cfg, file_glob="*.md")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["matches"][0]["relative_path"] == "docs/a.md"
