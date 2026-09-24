"""The edge-condition expression language.

The motivating case is `issues contains architecture`: cheap, instant and repeatable,
against a step that declared a JSON output shape — no model call and no judgement.
"""

from __future__ import annotations

import pytest

from app.services.expr import ExprError, evaluate, parse, resolve, roots

REVIEW = {
    "approved": False,
    "issues": ["architecture is wrong: the parser owns too much",
               "tests miss the n < 1 case"],
    "summary": "not ready",
    "counts": {"architect": 2, "designer": 0},
    "score": 7,
    "notes": None,
    "review_notes": {"approved": False, "issues": ["architecture"]},
}


def ev(text: str, context=None) -> bool:
    return evaluate(text, REVIEW if context is None else context)


# ----------------------------------------------------------- the motivating case


def test_contains_matches_a_word_inside_a_list_element():
    """The example from the request: a bare word on the right is the word itself."""
    assert ev("issues contains architecture") is True
    assert ev("issues contains database") is False


def test_contains_is_case_insensitive():
    assert ev("issues contains ARCHITECTURE") is True
    # A multi-word operand needs quoting; bare words are single tokens.
    assert ev("summary contains 'NOT READY'") is True


def test_contains_matches_a_whole_list_element():
    assert ev("review_notes.issues contains architecture") is True


def test_contains_on_a_string_is_a_substring_test():
    assert ev("summary contains ready") is True
    assert ev("summary contains shipped") is False


def test_contains_on_an_object_tests_for_a_key():
    assert ev("counts contains architect") is True
    assert ev("counts contains tester") is False


def test_not_contains():
    assert ev("issues not contains database") is True
    assert ev("issues not contains architecture") is False


# -------------------------------------------------------------------- booleans


def test_a_bare_path_is_a_truthiness_test():
    assert ev("approved") is False
    assert ev("summary") is True


def test_not_negates():
    assert ev("not approved") is True


def test_is_true_and_is_false_are_strict():
    assert ev("approved is false") is True
    assert ev("approved is true") is False
    # A non-boolean is neither.
    assert ev("summary is true") is False


def test_is_empty_and_is_not_empty():
    assert ev("notes is empty") is True
    assert ev("issues is not empty") is True
    assert ev("counts.designer is empty") is False  # 0 is a value, not emptiness
    assert ev("missing_field is empty") is True


# ----------------------------------------------------------------- comparisons


def test_numeric_comparison():
    assert ev("score > 5") is True
    assert ev("score >= 7") is True
    assert ev("score < 5") is False
    assert ev("counts.architect == 2") is True


def test_string_equality_ignores_case_and_padding():
    assert ev("summary == 'NOT READY'") is True
    assert ev('summary == "not ready"') is True


def test_equality_with_booleans_and_null():
    assert ev("approved == false") is True
    assert ev("notes == null") is True


def test_comparing_incompatible_types_is_false_not_an_error():
    assert ev("summary > 5") is False


# ---------------------------------------------------------------- dotted paths


def test_dotted_paths_reach_into_nested_objects():
    assert ev("counts.architect > 1") is True
    assert ev("review_notes.approved is false") is True


def test_indexing_into_a_list():
    assert ev("issues[0] contains architecture") is True
    assert ev("issues[1] contains tests") is True


def test_a_missing_path_is_false_rather_than_an_error():
    assert ev("nope.deeper contains anything") is False
    assert ev("nope") is False


def test_a_missing_path_is_not_equal_to_a_value():
    """`!=` on something absent is true: it is certainly not that value."""
    assert ev("nope != 'x'") is True
    assert ev("nope == 'x'") is False


# ------------------------------------------------------------------- combining


def test_and_or_and_parentheses():
    assert ev("not approved and issues is not empty") is True
    assert ev("approved or score > 5") is True
    assert ev("approved and score > 5") is False
    assert ev("(approved or score > 5) and issues contains architecture") is True


def test_operator_precedence_puts_and_above_or():
    # False and False -> False, then or True
    assert ev("approved and score < 1 or summary contains ready") is True


# ---------------------------------------------------- right-hand side resolution


def test_a_bare_word_that_names_a_field_uses_its_value():
    context = {"wanted": "architecture", "issues": ["architecture is wrong"]}
    assert evaluate("issues contains wanted", context) is True


def test_a_quoted_string_is_always_a_literal():
    context = {"wanted": "architecture", "issues": ["wanted is wrong"]}
    # Quoted, so it looks for the word "wanted", not the field's value.
    assert evaluate("issues contains 'wanted'", context) is True
    assert evaluate("issues contains wanted", context) is False


def test_in_reverses_containment():
    assert ev("'architecture is wrong: the parser owns too much' in issues") is True
    # A bare word beside an operator is the word when it names nothing in scope.
    assert ev("architect in counts") is True
    assert ev("tester in counts") is False


def test_a_bare_word_alone_is_only_ever_a_field():
    """Otherwise every typo would silently become a truthy string."""
    assert ev("nope") is False
    assert ev("not nope") is True
    assert ev("nope is empty") is True


# --------------------------------------------------------------------- parsing


def test_roots_reports_the_fields_an_expression_reads():
    assert roots("issues contains architecture") == {"issues", "architecture"}
    assert roots("not approved and counts.architect > 1") == {"approved", "counts"}


def test_resolve_returns_missing_for_an_absent_path():
    from app.services.expr import MISSING

    assert resolve(["nope"], {"a": 1}) is MISSING
    assert resolve(["a"], {"a": 1}) == 1


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "and",
    "issues contains",
    "issues ==",
    "(issues contains architecture",
    "issues contains architecture)",
    "issues @ architecture",
    "== 5",
])
def test_unparseable_expressions_are_rejected_with_a_reason(text):
    with pytest.raises(ExprError):
        parse(text)


def test_the_error_says_what_is_wrong():
    with pytest.raises(ExprError, match="right-hand side"):
        parse("issues contains")
    with pytest.raises(ExprError, match="closing parenthesis"):
        parse("(approved")
    with pytest.raises(ExprError, match="unexpected character"):
        parse("issues @ x")


def test_a_keyword_cannot_be_a_field_name():
    with pytest.raises(ExprError, match="keyword"):
        parse("and contains x")


def test_a_multi_word_bare_operand_is_rejected_clearly():
    """`contains not ready` would read `not` as negation, so quotes are required."""
    with pytest.raises(ExprError):
        parse("summary contains not ready")


def test_a_literal_may_be_on_the_left():
    assert ev("'architecture' in issues") is True
    assert ev("7 == score") is True
