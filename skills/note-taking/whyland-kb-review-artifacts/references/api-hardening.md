# KB Artifact API Hardening Notes

Session learning from hardening `kb.whyland.com/api/vault/artifact` for review artifacts.

## Problem observed

Publishing an artifact with a raw Markdown asset under `<slug>.assets/` can fail if downstream vault maintenance treats that `.md` file as a normal vault note and requires YAML frontmatter. When artifact writes fail after partial filesystem/index changes, rollback must also restore generated index files and clean untracked artifact files; otherwise the vault worktree remains dirty even though the API returned failure.

## Server-side fixes that worked

On the Whyland KB service, artifact assets should be excluded from normal note processing:

- `vault_lint.py` skips Markdown under `Artifacts/agent-reviews/*/*.assets/`.
- `generate_indexes.py` skips Markdown under artifact `.assets/` directories.
- `vault_search.py` skips Markdown under artifact `.assets/` directories.
- `vault_api.py` artifact rollback reverts generated `_indexes`, reverts `Artifacts/agent-reviews`, cleans untracked artifact files, and reports any post-rollback dirty state.

Restart scope used: only `whyland-vault-api.service`; Hermes gateways/agents were not restarted.

## Smoke verification pattern

Publish a test artifact with:

- one static HTML page;
- one raw `.md` asset without YAML frontmatter;
- short TTL;
- no secrets.

Then verify:

```bash
curl -fsS "$ARTIFACT_URL" >/tmp/artifact.html
curl -fsS "$ASSET_URL" >/tmp/asset.md
python3 - <<'PY'
from pathlib import Path
s = Path('/tmp/artifact.html').read_text(errors='replace').lower()
assert 'whyland-artifact-banner' in s
assert '<script' not in s
assert 'javascript:' not in s
print('artifact smoke ok')
PY
```

On the KB host, validate:

```bash
/srv/whyland-kb/tools/vault_lint.py --strict
systemctl is-active whyland-vault-api.service
git -C /srv/whyland-kb/vault status --short
```

Expected: lint exit 0, service active, worktree clean.

## Agent-facing fallback

If future publishing fails specifically because Markdown assets are being interpreted as vault notes, agents should retry by attaching raw Markdown as `.txt`, publish the HTML review page, and report the API/tooling regression. The user should never need to SSH into Hermes filesystems just to review a long agent handoff.
