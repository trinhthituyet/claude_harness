"""Graph rules: the shapes that must be rejected before a run starts."""

from __future__ import annotations

import pytest

from app.services.graph import (
    EdgeSpec,
    GraphError,
    NodeSpec,
    branches,
    can_finish,
    fans_out,
    joins,
    reachable,
    start_key,
    validate,
)


def nodes(*keys, start=None):
    return [NodeSpec(key, is_start=(key == (start or keys[0]))) for key in keys]


def edge(from_key, to_key, label="go", condition="", is_default=False, resets=()):
    return EdgeSpec(from_key, to_key, label, condition, is_default, tuple(resets))


# --------------------------------------------------------------------- shapes


def test_a_linear_graph_is_valid():
    validate(nodes("design", "build"), [edge("design", "build"), edge("build", None, "done")])


def test_a_single_node_with_no_edges_is_valid():
    """The simplest workflow: one role, one step, then stop."""
    validate(nodes("solo"), [])


def test_a_loop_is_valid():
    validate(
        nodes("build", "test"),
        [
            edge("build", "test", "to_test"),
            edge("test", "build", "rework", condition="tests fail"),
            edge("test", None, "approved", condition="tests pass", is_default=True),
        ],
    )


def test_empty_graph_is_rejected():
    with pytest.raises(GraphError, match="at least one node"):
        validate([], [])


def test_duplicate_keys_are_rejected():
    with pytest.raises(GraphError, match="duplicate node keys"):
        validate([NodeSpec("a", True), NodeSpec("a")], [])


def test_invalid_key_is_rejected():
    with pytest.raises(GraphError, match="invalid node key"):
        validate([NodeSpec("has space", True)], [])


def test_end_is_a_reserved_key():
    with pytest.raises(GraphError, match="reserved"):
        validate([NodeSpec("END", True)], [])


def test_two_start_nodes_are_rejected():
    with pytest.raises(GraphError, match="only one node can be the start"):
        validate([NodeSpec("a", True), NodeSpec("b", True)], [])


def test_edge_to_unknown_node_is_rejected():
    with pytest.raises(GraphError, match="points at unknown node"):
        validate(nodes("a"), [edge("a", "nowhere")])


def test_edge_from_unknown_node_is_rejected():
    with pytest.raises(GraphError, match="starts at unknown node"):
        validate(nodes("a"), [edge("ghost", None)])


def test_repeated_edge_label_on_one_node_is_rejected():
    with pytest.raises(GraphError, match="repeated edge labels"):
        validate(
            nodes("a", "b"),
            [edge("a", "b", "same", condition="x"), edge("a", None, "same", condition="y")],
        )


def test_two_defaults_on_one_node_are_rejected():
    with pytest.raises(GraphError, match="more than one default"):
        validate(
            nodes("a", "b"),
            [
                edge("a", "b", "one", condition="x", is_default=True),
                edge("a", None, "two", condition="y", is_default=True),
            ],
        )


def test_several_unconditional_edges_are_a_fan_out():
    """Not an error: every arm is taken, in parallel, as manager_dispatch does."""
    validate(
        nodes("a", "b", "c"),
        [edge("a", "b", "x"), edge("a", "c", "y"),
         edge("b", None, "done"), edge("c", None, "done")],
    )
    assert fans_out(
        [edge("a", "b", "x"), edge("a", "c", "y")], "a"
    ) is True


def test_mixing_conditional_and_unconditional_edges_is_rejected():
    """Ambiguous: is the bare edge a fan-out arm, or a fallback nobody described?"""
    with pytest.raises(GraphError, match="mixes unconditional edges"):
        validate(
            nodes("a", "b", "c"),
            [edge("a", "b", "x"),
             edge("a", "c", "y", condition="something is wrong"),
             edge("b", None, "done"), edge("c", None, "done")],
        )


def test_branching_with_a_default_is_accepted():
    validate(
        nodes("a", "b", "c"),
        [
            edge("a", "b", "x", condition="something is wrong"),
            edge("a", "c", "y", is_default=True),
            edge("b", None, "done"),
            edge("c", None, "done"),
        ],
    )


def test_unreachable_node_is_rejected():
    with pytest.raises(GraphError, match="unreachable"):
        validate(nodes("a", "orphan"), [edge("a", None, "done")])


def test_a_graph_that_cannot_finish_is_rejected():
    """Two nodes pointing at each other forever: the step budget would be the only exit."""
    with pytest.raises(GraphError, match="can never finish"):
        validate(nodes("a", "b"), [edge("a", "b", "there"), edge("b", "a", "back")])


def test_zero_visit_budget_is_rejected():
    with pytest.raises(GraphError, match="max_visits"):
        validate([NodeSpec("a", True, max_visits=0)], [])


# ------------------------------------------------------------------- helpers


def test_start_key_prefers_the_marked_node():
    assert start_key([NodeSpec("a"), NodeSpec("b", True)]) == "b"


def test_start_key_falls_back_to_the_first_node():
    assert start_key([NodeSpec("a"), NodeSpec("b")]) == "a"


def test_reachable_follows_edges_and_ignores_end():
    found = reachable(
        nodes("a", "b", "c"),
        [edge("a", "b", "x"), edge("b", None, "done"), edge("a", "c", "y")],
    )
    assert found == {"a", "b", "c"}


def test_can_finish_detects_a_terminal_node():
    assert can_finish(nodes("a", "b"), [edge("a", "b", "x")]) is True


def test_can_finish_is_false_for_a_closed_loop():
    assert can_finish(nodes("a", "b"), [edge("a", "b", "x"), edge("b", "a", "y")]) is False


# ------------------------------------------------- fan-out, joins, escalation


def test_branches_and_fans_out_are_distinguished():
    fan = [edge("a", "b", "x"), edge("a", "c", "y")]
    assert fans_out(fan, "a") and not branches(fan, "a")

    branch = [edge("a", "b", "x", condition="broken"),
              edge("a", "c", "y", condition="fine", is_default=True)]
    assert branches(branch, "a") and not fans_out(branch, "a")

    lone = [edge("a", "b", "x")]
    assert not fans_out(lone, "a") and not branches(lone, "a")


def test_joins_reports_what_converges_on_a_node():
    edges = [edge("architect", "review", "r1"), edge("designer", "review", "r2"),
             edge("tester", "review", "r3")]
    assert joins(edges, "review") == ["architect", "designer", "tester"]


def test_two_nodes_writing_the_same_output_name_is_rejected():
    """One would silently overwrite the other's artifact."""
    with pytest.raises(GraphError, match="same output name"):
        validate(
            [NodeSpec("a", True, output_key="doc"), NodeSpec("b", output_key="doc")],
            [edge("a", "b", "x"), edge("b", None, "done")],
        )


def test_an_unknown_escalation_node_is_rejected():
    with pytest.raises(GraphError, match="escalation node"):
        validate(nodes("a"), [edge("a", None, "done")], escalation="ghost")


def test_an_escalation_node_may_be_reachable_only_by_escalation():
    """It is jumped to when a budget runs out, so no edge needs to point at it."""
    validate(
        [NodeSpec("a", True), NodeSpec("human")],
        [edge("a", None, "done"), edge("human", None, "handed_over")],
        escalation="human",
    )


def test_an_edge_resetting_an_unknown_node_is_rejected():
    with pytest.raises(GraphError, match="resets unknown nodes"):
        validate(nodes("a"), [edge("a", None, "done", resets=("ghost",))])


# ------------------------------------------------------------- output schemas


def test_a_usable_output_schema_is_accepted():
    from app.services.graph import validate_output_schema

    assert validate_output_schema({
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string", "description": "one line"},
        },
        "required": ["approved", "issues"],
        "additionalProperties": False,
    }) == []


def test_a_nested_object_schema_is_accepted():
    """The example graph's Issues model: per-role lists inside one object."""
    from app.services.graph import validate_output_schema

    assert validate_output_schema({
        "type": "object",
        "properties": {
            "issues": {
                "type": "object",
                "properties": {
                    "architect": {"type": "array", "items": {"type": "string"}},
                    "designer": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }) == []


def test_an_enum_field_is_accepted():
    """PMReport.failure_type is a literal set."""
    from app.services.graph import validate_output_schema

    assert validate_output_schema({
        "type": "object",
        "properties": {
            "failure_type": {"type": "string",
                             "enum": ["none", "code_bug", "arch_issue"]},
        },
    }) == []


def test_a_non_object_schema_is_rejected():
    from app.services.graph import validate_output_schema

    problems = validate_output_schema({"type": "string"})
    assert any("must be" in p for p in problems)


def test_an_empty_schema_is_rejected():
    from app.services.graph import validate_output_schema

    assert validate_output_schema({}) != []


def test_a_property_without_a_type_is_rejected():
    from app.services.graph import validate_output_schema

    assert any("needs a \"type\"" in p for p in
               validate_output_schema({"type": "object", "properties": {"a": {}}}))


def test_an_array_without_items_is_rejected():
    from app.services.graph import validate_output_schema

    problems = validate_output_schema(
        {"type": "object", "properties": {"a": {"type": "array"}}})
    assert any("without \"items\"" in p for p in problems)


def test_required_naming_a_missing_property_is_rejected():
    from app.services.graph import validate_output_schema

    problems = validate_output_schema(
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]})
    assert any("do not exist" in p for p in problems)


def test_unsupported_keywords_are_rejected():
    """Silently passing them through would fail later, in the CLI, with no clue why."""
    from app.services.graph import validate_output_schema

    problems = validate_output_schema(
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}})
    assert any("unsupported keywords" in p for p in problems)


def test_a_node_schema_is_checked_by_validate():
    with pytest.raises(GraphError, match="output schema"):
        validate([NodeSpec("a", True, output_schema={"type": "string"})], [])
