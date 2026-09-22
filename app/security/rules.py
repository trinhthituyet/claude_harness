"""Generate the ``permissions.deny`` rules for a run's settings blob.

Deny rules are the one layer that holds even if both in-process callbacks are
broken, because the CLI evaluates them before anything reaches a prompt. There is
deliberately **no** allow list: an allow rule would shadow ``can_use_tool`` exactly
the way a whole-tool ``allowed_tools`` entry does. See docs/DESIGN.md section 4.3.
"""

from __future__ import annotations

from pathlib import Path

from app.security.policy import RunPolicy

# Credential and configuration locations that a session should never read.
# With reads otherwise unconfined (decision 1), this list is the only hard
# boundary on reads, so it is kept deliberately broad.
_SECRET_DIRS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".claude",
    ".config/gh",
    ".config/gcloud",
    ".kube",
    ".docker",
)
_SECRET_FILES = (".netrc", ".pypirc", ".git-credentials", ".npmrc")

# Files that would let a session change its own future behaviour or the harness's.
_SHELL_RC = (".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile")


def _abs(path: Path) -> str:
    """Render an absolute path in the rule grammar's ``//`` form."""
    return "//" + str(path).lstrip("/")


def deny_rules(policy: RunPolicy, home: Path | None = None) -> list[str]:
    """Build the deny rule list for this run."""
    home = home or Path.home()
    rules: list[str] = []

    for name in _SECRET_DIRS:
        target = _abs(home / name) + "/**"
        rules.append(f"Read({target})")
        rules.append(f"Edit({target})")
    for name in _SECRET_FILES:
        target = _abs(home / name)
        rules.append(f"Read({target})")
        rules.append(f"Edit({target})")
    for name in _SHELL_RC:
        rules.append(f"Edit({_abs(home / name)})")

    # System locations. Writes only: denying reads of /etc or /usr would break
    # ordinary tasks, and they hold no user credentials.
    for system_dir in ("/etc", "/usr", "/System", "/Library/LaunchAgents"):
        rules.append(f"Edit({_abs(Path(system_dir))}/**)")

    # Inside the project root but able to execute later. These paths pass the
    # containment check by definition, so only a rule can stop them.
    for inside in (".git/hooks", ".claude"):
        rules.append(f"Edit({_abs(policy.root / inside)}/**)")

    # Deduplicate while preserving order for a stable, reviewable settings blob.
    return list(dict.fromkeys(rules))
