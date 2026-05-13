# Cross-Profile Rollout Notes

Use this only from Forge/operator context when sharing `whyland-kb-review-artifacts` across local Hermes profiles.

## Runtime skill roots

Copy the canonical skill tree to:

```text
/home/nepenthe/.hermes/skills/note-taking/whyland-kb-review-artifacts/
/home/nepenthe/.hermes/profiles/_shared/skills/note-taking/whyland-kb-review-artifacts/
/home/nepenthe/.hermes/profiles/<profile>/skills/note-taking/whyland-kb-review-artifacts/
```

Then verify each profile has the same SHA256 as the repo source.

## Token bootstrap

Each profile SHOULD have its own KB token in its profile-local `.env`:

```text
WHYLAND_KB_API_URL=http://kb.whyland.com/api/vault
WHYLAND_KB_API_TOKEN=<profile-specific token>
```

The KB API token map lives on `whyland-kb`:

```text
/etc/whyland-kb/vault-api-tokens.json
```

When adding a token, MUST preserve:

```text
owner: nepenthe:nepenthe
mode: 600
```

A root-owned `600` replacement makes every API request unauthorized because the service runs as `nepenthe`.

The API reloads this token file per request. Token additions DO NOT require restarting `whyland-vault-api.service`.

## Verification

For every profile:

1. Load the profile-local `.env` without printing the token.
2. Call `GET $WHYLAND_KB_API_URL/whoami` with the bearer token.
3. Confirm the returned `agent_id` matches the profile and role is expected.
4. Publish a tiny smoke artifact only when changing API behavior, not for routine skill copying.

DO NOT restart Hermes gateways just to refresh skills. Already-running sessions may have cached skill lists; new sessions/reset pick up the copied skill.
