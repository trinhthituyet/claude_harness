"""Seeded "Suggested" catalogs for the Skills and MCPs panels.

These are curated pointers, not installers: adding one creates a local row the user
then fills in (or, for skills, points at a source they trust).
"""

from __future__ import annotations

from typing import Any

SUGGESTED_SKILLS: list[dict[str, str]] = [
    {
        "name": "code-review",
        "description": "Review a diff or PR for correctness bugs and simplifications.",
    },
    {
        "name": "security-review",
        "description": "Audit pending changes for security issues before merge.",
    },
    {
        "name": "test-writer",
        "description": "Generate focused unit tests for changed functions.",
    },
    {
        "name": "api-docs",
        "description": "Keep API reference docs in sync with route definitions.",
    },
    {
        "name": "commit-message",
        "description": "Write conventional commit messages from a staged diff.",
    },
]

SUGGESTED_MCPS: list[dict[str, Any]] = [
    {
        "name": "filesystem",
        "description": "Reference filesystem server. Trust it only if you accept that its "
        "tools bypass the project path boundary.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
    {
        "name": "git",
        "description": "Inspect history, diffs and blame in a repository.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-git"],
    },
    {
        "name": "sqlite",
        "description": "Query a local SQLite database.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "./data.db"],
    },
    {
        "name": "fetch",
        "description": "Fetch and convert web pages to markdown.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
]

ANTHROPIC_MODELS: list[dict[str, str]] = [
    {"model_id": "claude-opus-5", "label": "Opus 5 — most capable"},
    {"model_id": "claude-sonnet-5", "label": "Sonnet 5 — balanced"},
    {"model_id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5 — fastest"},
]

DEFAULT_ROLES: list[dict[str, str]] = [
    {
        "name": "Software Architect",
        "description": "Designs the approach and reviews structural decisions.",
        "system_prompt": (
            "You are a software architect. Before changing code, establish the smallest "
            "design that satisfies the requirement, name the trade-offs you are accepting, "
            "and keep module boundaries clean. Prefer editing existing structure over "
            "adding new layers."
        ),
    },
    {
        "name": "Software Engineer",
        "description": "Implements the change and keeps it consistent with the codebase.",
        "system_prompt": (
            "You are a software engineer. Write code that matches the surrounding "
            "conventions, naming and comment density. Make the change complete: update "
            "callers, imports and configuration. Do not leave TODOs behind."
        ),
    },
    {
        "name": "Tester",
        "description": "Writes and runs tests, and reports failures honestly.",
        "system_prompt": (
            "You are a tester. Cover the behaviour that matters, especially boundaries and "
            "error paths. Run the tests and report real output. If something fails, say so "
            "plainly with the failure text rather than describing it as passing."
        ),
    },
    {
        "name": "Designer",
        "description": "Owns the user-facing surface: layout, copy and accessibility.",
        "system_prompt": (
            "You are a designer. Keep the interface legible and consistent, with clear "
            "empty states, honest error messages and keyboard access. Prefer removing "
            "elements over adding them."
        ),
    },
]
