"""Discover skills on disk and import uploaded ones.

A skill is a directory containing SKILL.md with YAML frontmatter (at minimum a
``name`` and ``description``). We parse just enough of the frontmatter to list and
describe skills, rather than taking a YAML dependency.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", re.IGNORECASE)


class SkillImportError(ValueError):
    pass


@dataclass
class DiscoveredSkill:
    name: str
    description: str
    path: Path
    scope: str
    metadata: dict[str, str]


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the flat ``key: value`` frontmatter of a SKILL.md."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _read_skill(skill_md: Path, scope: str) -> DiscoveredSkill | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta = parse_frontmatter(text)
    name = meta.get("name") or skill_md.parent.name
    return DiscoveredSkill(
        name=name,
        description=meta.get("description", ""),
        path=skill_md.parent,
        scope=scope,
        metadata=meta,
    )


def discover(project_path: str | Path | None = None) -> list[DiscoveredSkill]:
    """Scan the user skills directory, and a project's, for installed skills."""
    found: dict[str, DiscoveredSkill] = {}
    roots: list[tuple[Path, str]] = [(settings.skills_dir, "user")]
    if project_path:
        roots.append((Path(project_path) / ".claude" / "skills", "project"))
    for root, scope in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if entry.is_dir() and skill_md.is_file():
                skill = _read_skill(skill_md, scope)
                if skill is not None:
                    found.setdefault(skill.name, skill)
    return list(found.values())


def _validate_name(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise SkillImportError(
            f"invalid skill name {name!r}: use letters, digits, dot, dash or underscore"
        )
    return name


def install_from_markdown(name: str, body: str) -> Path:
    """Write a single-file skill into the user skills directory."""
    name = _validate_name(name)
    target = settings.skills_dir / name
    if target.exists():
        raise SkillImportError(f"a skill named {name!r} is already installed")
    target.mkdir(parents=True)
    meta = parse_frontmatter(body)
    if not meta.get("name"):
        body = f"---\nname: {name}\ndescription: {meta.get('description', '')}\n---\n\n{body}"
    (target / "SKILL.md").write_text(body, encoding="utf-8")
    return target


def install_from_zip(data: bytes) -> Path:
    """Unpack an uploaded skill archive, refusing anything that escapes the target."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillImportError("not a valid zip archive") from exc

    names = [n for n in archive.namelist() if not n.endswith("/")]
    if not names:
        raise SkillImportError("archive is empty")
    for entry in names:
        path = Path(entry)
        if path.is_absolute() or ".." in path.parts:
            raise SkillImportError(f"archive entry escapes the target directory: {entry}")

    skill_md = [n for n in names if Path(n).name == "SKILL.md"]
    if not skill_md:
        raise SkillImportError("archive contains no SKILL.md")
    # The shallowest SKILL.md defines the skill root.
    root_entry = min(skill_md, key=lambda n: len(Path(n).parts))
    prefix = Path(root_entry).parent

    meta = parse_frontmatter(archive.read(root_entry).decode("utf-8", errors="replace"))
    name = _validate_name(meta.get("name") or (prefix.name or "skill"))
    target = settings.skills_dir / name
    if target.exists():
        raise SkillImportError(f"a skill named {name!r} is already installed")

    target.mkdir(parents=True)
    try:
        for entry in names:
            relative = Path(entry)
            try:
                relative = relative.relative_to(prefix)
            except ValueError:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target
