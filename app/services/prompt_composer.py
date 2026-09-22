"""Compose a team's roles into a lead system prompt plus subagent definitions.

One session per run: the lead role drives and delegates to the other roles, which
become custom agents invokable through the Agent tool. See docs/DESIGN.md 3.5.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from app.services.snapshot import RunSnapshot


def _roster(snapshot: RunSnapshot) -> str:
    teammates = snapshot.teammates
    if not teammates:
        return ""
    lines = [
        "",
        "## Your team",
        "",
        "You are the lead. These teammates are available as subagents via the Agent tool;",
        "delegate work that matches their role instead of doing it yourself:",
        "",
    ]
    for role in teammates:
        description = role.description.strip() or "no description given"
        lines.append(f"- **{role.name}** — {description}")
    lines += [
        "",
        "Delegate by calling the Agent tool with the teammate's name as the agent type.",
        "Integrate their results yourself and report the combined outcome.",
    ]
    return "\n".join(lines)


def compose_system_prompt(snapshot: RunSnapshot) -> str:
    """The text appended to Claude Code's built-in system prompt."""
    lead = snapshot.lead
    parts = [f"# Role: {lead.name}", "", lead.system_prompt.strip()]
    roster = _roster(snapshot)
    if roster:
        parts.append(roster)
    return "\n".join(parts)


def compose_agents(snapshot: RunSnapshot) -> dict[str, AgentDefinition]:
    """Non-lead roles become subagents."""
    return {
        role.name: AgentDefinition(
            description=role.description.strip() or f"The {role.name} role.",
            prompt=role.system_prompt,
            model=role.model or "inherit",
        )
        for role in snapshot.teammates
    }
