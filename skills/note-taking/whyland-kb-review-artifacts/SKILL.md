---
name: whyland-kb-review-artifacts
description: Use when a Hermes agent needs to publish a long review, report, plan, audit, visual handoff, or supporting files for Chris on kb.whyland.com instead of posting oversized content in chat or requiring SSH filesystem access.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [whyland, kb, artifacts, review, handoff]
    related_skills: [whyland-vault-operating-ledger, obsidian]
---

# Whyland KB Review Artifacts

## Overview

Use the Whyland KB artifact endpoint to publish browser-reviewable, static handoff pages at `kb.whyland.com` when chat is the wrong surface. This is for long reports, code review summaries, plans, visual dashboards, audit evidence, generated documents, or small supporting files that Chris should inspect in a browser.

Artifacts are **review surfaces**, not canonical project state. They are intentionally isolated under:

```text
Artifacts/agent-reviews/<project-key>/<yyyy-mm-dd>-<slug>.html
Artifacts/agent-reviews/<project-key>/<yyyy-mm-dd>-<slug>.assets/
Artifacts/agent-reviews/<project-key>/<yyyy-mm-dd>-<slug>.artifact.json
```

The KB API sanitizes HTML, strips active content, records metadata, updates artifact indexes, runs KB maintenance, commits/pushes the vault, and returns a URL.

## When to Use

Use this skill when:

- Your response would be too long or poorly rendered in Matrix/Telegram/Discord.
- Chris needs to review a structured report, plan, audit, diff explanation, benchmark result, generated document, or visual output.
- You have images, JSON, CSV, logs, Markdown, PDFs, or small supporting files that should sit beside a single review page.
- You would otherwise tell Chris to SSH into a Hermes filesystem or browse `/tmp`/workspace artifacts.
- You need a durable-but-not-canonical browser handoff URL.

Do **NOT** use this skill for:

- Secrets, tokens, credentials, private keys, cookies, `.env` files, or raw auth headers.
- Arbitrary web apps, JavaScript demos, forms, trackers, iframes, remote JS, or active content.
- Canonical docs that belong in a repo, project card, system card, decision note, or operation note.
- Huge binary bundles. Keep artifacts small and review-oriented.

## Endpoint

Environment:

```bash
export WHYLAND_KB_API_URL="${WHYLAND_KB_API_URL:-http://kb.whyland.com/api/vault}"
# WHYLAND_KB_API_TOKEN must be profile-local. NEVER print it.
```

Endpoint:

```http
POST /api/vault/artifact
Authorization: Bearer <WHYLAND_KB_API_TOKEN>
Content-Type: application/json
```

Payload shape:

```json
{
  "project": "Trading Model V5",
  "slug": "fill-lifecycle-evidence",
  "title": "Fill Lifecycle Evidence Review",
  "ttl_days": 30,
  "retain": false,
  "html": "<!doctype html><html>...</html>",
  "assets": [
    {
      "path": "chart.png",
      "content_base64": "...",
      "content_type": "image/png"
    }
  ],
  "message": "artifact: publish fill lifecycle evidence review"
}
```

Response shape:

```json
{
  "ok": true,
  "path": "Artifacts/agent-reviews/trading-model-v5/2026-05-13-fill-lifecycle-evidence.html",
  "url": "http://kb.whyland.com/Artifacts/agent-reviews/trading-model-v5/2026-05-13-fill-lifecycle-evidence.html",
  "manifest": "Artifacts/agent-reviews/trading-model-v5/2026-05-13-fill-lifecycle-evidence.artifact.json",
  "assets": [{"path": ".../chart.png", "bytes": 12345}],
  "expires": "2026-06-12",
  "agent_id": "forge",
  "maintenance_exit_code": 0,
  "git": {"committed": true, "commit": "abc123"}
}
```

## Security and Content Rules

The API enforces a static review surface:

- HTML is sanitized server-side.
- `<script>`, iframes, forms, object/embed tags, event handlers, `javascript:` URLs, CSS `@import`, and CSS `url(...)` are stripped.
- A visible banner is injected: generated-by, project/scope, timestamp, TTL, and “review artifact only.”
- Assets are limited by suffix, count, per-file size, and total size.
- Assets live only under the artifact’s sibling `.assets/` directory.
- Artifact writes do NOT edit project cards or system cards.

Allowed asset suffixes currently include:

```text
.png .jpg .jpeg .webp .gif .txt .json .csv .md .pdf .log
```

DO NOT attempt to bypass the sanitizer. If the review needs an interactive app, publish a static summary here and put the app in the proper project deployment flow.

## Retention

Default retention is 30 days:

```json
{"ttl_days": 30}
```

Use shorter TTLs for routine handoffs:

```json
{"ttl_days": 7}
```

Use `retain: true` only for milestone artifacts that should remain available indefinitely:

```json
{"retain": true}
```

If an artifact becomes canonical, DO NOT rely on the artifact URL alone. Promote the underlying content into the proper source of truth: repo docs, project card, decision note, operations note, or runbook.

## Recommended HTML Structure

Keep it boring and reviewable:

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Review Title</title>
  <style>
    body { font-family: system-ui, sans-serif; line-height: 1.5; }
    pre { overflow: auto; padding: 1rem; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ccc; padding: .4rem; }
  </style>
</head>
<body>
  <h1>Review Title</h1>
  <section>
    <h2>Decision Needed</h2>
    <p>What Chris should review or approve.</p>
  </section>
  <section>
    <h2>Evidence</h2>
    <ul>
      <li><a href="./2026-05-13-my-slug.assets/evidence.json">Evidence JSON</a></li>
    </ul>
  </section>
</body>
</html>
```

The API injects its own banner and base CSS. Your CSS can style content, but external CSS, JS, and active behavior are not supported.

## Publishing Recipe

Use Python to avoid leaking tokens in shell history and to handle base64 safely:

```bash
python3 - <<'PY'
import base64
import json
import os
import urllib.request

api = os.environ.get("WHYLAND_KB_API_URL", "http://kb.whyland.com/api/vault").rstrip("/")
token = os.environ["WHYLAND_KB_API_TOKEN"]

html = """<!doctype html>
<html><body>
<h1>Example Review</h1>
<p>This is a static review artifact.</p>
</body></html>"""

payload = {
    "project": "Example Project",
    "slug": "example-review",
    "title": "Example Review",
    "ttl_days": 30,
    "html": html,
    "assets": [
        {
            "path": "evidence.json",
            "content_base64": base64.b64encode(b'{"ok": true}\n').decode(),
            "content_type": "application/json",
        }
    ],
    "message": "artifact: publish example review",
}

req = urllib.request.Request(
    f"{api}/artifact",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=420) as resp:
    out = json.loads(resp.read().decode())
print(out["url"])
PY
```

## Verification

After publishing, ALWAYS verify the returned URL before presenting it:

```bash
curl -fsS "$ARTIFACT_URL" >/tmp/artifact.html
python3 - <<'PY'
from pathlib import Path
s = Path('/tmp/artifact.html').read_text(errors='replace').lower()
assert 'whyland-artifact-banner' in s
assert '<script' not in s
assert 'javascript:' not in s
assert 'onclick' not in s
print('artifact verified')
PY
```

If you published assets, fetch at least one asset URL too.

## Final Response Pattern

Keep the final response short:

```md
Published review artifact:

http://kb.whyland.com/Artifacts/agent-reviews/<project-key>/<date>-<slug>.html

Verified: browser URL returns 200; active content stripped; assets accessible.
Canonical files changed: none — this is a review artifact, not project state.
Expires: <date or retained>.
```

If the artifact led to a durable decision or changed system/project state, ALSO update the relevant project card, decision note, operation note, or repo docs and name those files in the final response.

## Common Pitfalls

1. **Publishing secrets.** NEVER include credentials, tokens, cookies, SSH keys, `.env`, signed URLs, or raw authorization headers.
2. **Treating artifacts as docs.** Artifacts are review surfaces. Canonical docs still live in repos/vault notes.
3. **Expecting JavaScript.** The sanitizer strips active content. Use static HTML/CSS only.
4. **Forgetting URL verification.** A committed artifact is not useful if Quartz/nginx did not publish it. Fetch the final URL.
5. **Over-retaining noise.** Use short TTLs for routine handoffs. Retain only milestone artifacts.
6. **Editing project cards by side effect.** Artifact publishing is isolated under `Artifacts/agent-reviews/`; update project cards separately and only within ownership rules.
7. **Breaking the token map while adding agents.** If Forge bootstraps a new profile token in `/etc/whyland-kb/vault-api-tokens.json`, preserve ownership/readability for the API service user (`nepenthe:nepenthe`, mode `600`). A root-owned `600` replacement makes every token look unauthorized until ownership is fixed. The API reloads the token file on each request; no service restart is needed for token additions.

## Verification Checklist

- [ ] Payload includes project, title, slug, TTL/retain choice, and static HTML.
- [ ] No secrets or sensitive raw logs are included.
- [ ] Supporting files are small, allowed suffixes, and referenced relatively from HTML.
- [ ] API returned `ok: true`, URL, manifest, git result, and maintenance exit code 0.
- [ ] Returned URL fetches successfully from `kb.whyland.com`.
- [ ] HTML contains the KB artifact banner and no active content markers.
- [ ] At least one asset URL was fetched when assets were supplied.
- [ ] Final response clearly labels the page as a review artifact, not canonical state.
