from pathlib import Path

from gateway.config import Platform, HomeChannel
from gateway.session import SessionContext, SessionSource, build_session_context_prompt
from gateway.workspace_registry import WorkspaceBinding, WorkspaceRegistry


def write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces.yaml"
    path.write_text(
        """
workspaces:
  example:
    name: Example Project
    repo_path: /srv/example
    canonical_repo_url: git@github.com:example/project.git
    default_branch: main
    channels:
      - platform: matrix
        room_id: "!room:example.org"
        room_name: "Project - Example"
        response_policy: free_response
      - platform: discord
        guild_id: "guild-1"
        channel_id: "channel-1"
        thread_id: "thread-1"
        response_policy: mention_only
      - platform: slack
        workspace_id: "T123"
        channel_id: "C456"
""".strip(),
        encoding="utf-8",
    )
    return path


def test_registry_resolves_matrix_room_binding(tmp_path):
    registry = WorkspaceRegistry(write_registry(tmp_path))
    source = SessionSource(
        platform=Platform.MATRIX,
        chat_id="!room:example.org",
        chat_name="Project - Example",
        chat_type="group",
    )

    binding = registry.resolve_source(source)

    assert binding == WorkspaceBinding(
        slug="example",
        name="Example Project",
        repo_path="/srv/example",
        canonical_repo_url="git@github.com:example/project.git",
        default_branch="main",
        response_policy="free_response",
        source=str(registry.config_path),
    )


def test_registry_resolves_thread_specific_discord_binding(tmp_path):
    registry = WorkspaceRegistry(write_registry(tmp_path))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        thread_id="thread-1",
        guild_id="guild-1",
        chat_type="thread",
    )

    binding = registry.resolve_source(source)

    assert binding is not None
    assert binding.slug == "example"
    assert binding.response_policy == "mention_only"


def test_registry_ignores_wrong_thread_binding(tmp_path):
    registry = WorkspaceRegistry(write_registry(tmp_path))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        thread_id="other-thread",
        guild_id="guild-1",
        chat_type="thread",
    )

    assert registry.resolve_source(source) is None


def test_registry_resolves_slack_channel_id(tmp_path):
    registry = WorkspaceRegistry(write_registry(tmp_path))
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C456",
        guild_id="T123",
        chat_type="channel",
    )

    binding = registry.resolve_source(source)

    assert binding is not None
    assert binding.slug == "example"
    assert binding.response_policy is None


def test_prompt_includes_authoritative_project_binding():
    context = SessionContext(
        source=SessionSource(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_name="Project - Example",
            chat_type="group",
        ),
        connected_platforms=[Platform.MATRIX],
        home_channels={Platform.MATRIX: HomeChannel(platform=Platform.MATRIX, chat_id="!home:example.org", name="Home")},
        workspace_binding=WorkspaceBinding(
            slug="example",
            name="Example Project",
            repo_path="/srv/example",
            canonical_repo_url="git@github.com:example/project.git",
            default_branch="main",
            response_policy="free_response",
            source="/tmp/workspaces.yaml",
        ),
    )

    prompt = build_session_context_prompt(context)

    assert "**Current Project Binding:**" in prompt
    assert "Example Project (`example`)" in prompt
    assert "Repo path: `/srv/example`" in prompt
    assert "Canonical repo URL: `git@github.com:example/project.git`" in prompt
    assert "authoritative for side-effecting project work" in prompt


def test_prompt_marks_missing_project_binding_for_gateway_rooms():
    context = SessionContext(
        source=SessionSource(
            platform=Platform.MATRIX,
            chat_id="!unknown:example.org",
            chat_type="group",
        ),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
    )

    prompt = build_session_context_prompt(context)

    assert "No authoritative project binding was found" in prompt
    assert "continue read-only" in prompt
