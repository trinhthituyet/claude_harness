"""Graph validation for workflows.

Pure functions over light tuples so they can be unit-tested and reused by both the
REST layer and the chat assistant's tools. The rules exist to catch the graphs that
would misbehave at run time rather than fail loudly.

Execution is LangGraph's superstep model, and the rules follow from it: a node's
outgoing edges are evaluated as a group, and the group may select *several* targets.
So a node either **fans out** (every edge unconditional — all are taken, in parallel)
or **branches** (every edge carries a condition, or is the default). Mixing the two
would make it ambiguous whether an unconditional edge is a fan-out arm or a fallback,
so that is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: ``to_key`` value meaning "finish the workflow here".
END = "END"

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


@dataclass(frozen=True)
class NodeSpec:
    key: str
    is_start: bool = False
    max_visits: int = 3
    #: What this step's output is filed under for later steps. Defaults to the key.
    output_key: str | None = None

    @property
    def artifact(self) -> str:
        return self.output_key or self.key


@dataclass(frozen=True)
class EdgeSpec:
    from_key: str
    to_key: str | None  # None or END finishes the workflow
    label: str
    condition: str = ""
    is_default: bool = False
    #: Node keys whose visit budget resets when this edge is taken.
    resets: tuple[str, ...] = ()

    @property
    def ends(self) -> bool:
        return self.to_key in (None, END)

    @property
    def conditional(self) -> bool:
        return bool(self.condition.strip())


class GraphError(ValueError):
    """The graph is not runnable. The message lists every problem found."""


def outgoing(edges: list[EdgeSpec], key: str) -> list[EdgeSpec]:
    return [edge for edge in edges if edge.from_key == key]


def start_key(nodes: list[NodeSpec]) -> str:
    """The entry point: the node marked as start, else the first one."""
    for node in nodes:
        if node.is_start:
            return node.key
    return nodes[0].key


def fans_out(edges: list[EdgeSpec], key: str) -> bool:
    """True when this node starts several branches at once rather than choosing one."""
    out = outgoing(edges, key)
    return len(out) > 1 and all(not e.conditional and not e.is_default for e in out)


def branches(edges: list[EdgeSpec], key: str) -> bool:
    """True when this node picks among its outgoing edges."""
    out = outgoing(edges, key)
    return len(out) > 1 and not fans_out(edges, key)


def joins(edges: list[EdgeSpec], key: str) -> list[str]:
    """The nodes that converge on this one — where a fan-out rejoins."""
    return sorted({e.from_key for e in edges if e.to_key == key})


def reachable(nodes: list[NodeSpec], edges: list[EdgeSpec], extra_roots: tuple[str, ...] = ()) -> set[str]:
    seen: set[str] = set()
    queue = [start_key(nodes), *extra_roots]
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        for edge in outgoing(edges, key):
            if not edge.ends and edge.to_key is not None:
                queue.append(edge.to_key)
    return seen


def can_finish(nodes: list[NodeSpec], edges: list[EdgeSpec], escalation: str | None = None) -> bool:
    """True when some reachable node can terminate: an END edge, or no edges at all."""
    live = reachable(nodes, edges, (escalation,) if escalation else ())
    for key in live:
        out = outgoing(edges, key)
        if not out or any(edge.ends for edge in out):
            return True
    return False


def validate(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    escalation: str | None = None,
) -> None:
    """Raise :class:`GraphError` listing everything wrong with the graph."""
    problems: list[str] = []

    if not nodes:
        raise GraphError("a workflow needs at least one node")

    keys = [node.key for node in nodes]
    key_set = set(keys)
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        problems.append(f"duplicate node keys: {duplicates}")
    for key in keys:
        if not KEY_PATTERN.match(key):
            problems.append(
                f"invalid node key {key!r}: use letters, digits, dash or underscore"
            )
    if END in {k.upper() for k in key_set}:
        problems.append(f"{END!r} is reserved and cannot be a node key")

    artifacts = [node.artifact for node in nodes]
    clashing = sorted({a for a in artifacts if artifacts.count(a) > 1})
    if clashing:
        problems.append(
            f"two nodes write the same output name, so one would overwrite the other: {clashing}"
        )

    starts = [node.key for node in nodes if node.is_start]
    if len(starts) > 1:
        problems.append(f"only one node can be the start, got {starts}")

    for node in nodes:
        if node.max_visits < 1:
            problems.append(f"node {node.key!r}: max_visits must be at least 1")

    if escalation is not None and escalation not in key_set:
        problems.append(f"the escalation node {escalation!r} is not a node in this workflow")

    for edge in edges:
        if edge.from_key not in key_set:
            problems.append(f"edge {edge.label!r} starts at unknown node {edge.from_key!r}")
        if not edge.ends and edge.to_key not in key_set:
            problems.append(f"edge {edge.label!r} points at unknown node {edge.to_key!r}")
        if not edge.label.strip():
            problems.append(f"an edge from {edge.from_key!r} has no label")
        unknown_resets = sorted(set(edge.resets) - key_set)
        if unknown_resets:
            problems.append(
                f"edge {edge.label!r} resets unknown nodes: {unknown_resets}"
            )

    for key in sorted(key_set):
        out = outgoing(edges, key)
        labels = [edge.label for edge in out]
        repeated = sorted({label for label in labels if labels.count(label) > 1})
        if repeated:
            problems.append(f"node {key!r} has repeated edge labels: {repeated}")
        defaults = [edge.label for edge in out if edge.is_default]
        if len(defaults) > 1:
            problems.append(f"node {key!r} has more than one default edge: {defaults}")
        if len(out) > 1:
            plain = [e.label for e in out if not e.conditional and not e.is_default]
            described = [e.label for e in out if e.conditional or e.is_default]
            if plain and described:
                problems.append(
                    f"node {key!r} mixes unconditional edges {plain} with conditional ones "
                    f"{described}: either fan out to all of them, or give every edge a "
                    "condition (one may be the default)"
                )

    if not problems:
        # Only meaningful once the structure is sound.
        extra = (escalation,) if escalation else ()
        unreachable = sorted(key_set - reachable(nodes, edges, extra))
        if unreachable:
            problems.append(
                f"unreachable from the start node {start_key(nodes)!r}: {unreachable}"
            )
        if not can_finish(nodes, edges, escalation):
            problems.append(
                "this workflow can never finish: give some node an edge to END, or "
                "leave a node with no outgoing edges"
            )

    if problems:
        raise GraphError("; ".join(problems))
