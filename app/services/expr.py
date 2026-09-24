"""A tiny expression language for edge conditions over structured step results.

Once a step declares a JSON output shape, a branch should be able to *test* it rather
than ask a model to judge it. ``issues contains architecture`` is cheaper, instant and
repeatable; a natural-language condition still exists for the genuinely judgemental cases.

Grammar::

    expr        := or
    or          := and ( "or" and )*
    and         := not ( "and" not )*
    not         := "not" not | primary
    primary     := "(" expr ")" | comparison
    comparison  := term unary | term binop operand | term
    unary       := "is empty" | "is not empty" | "is true" | "is false"
    binop       := "==" | "!=" | ">" | ">=" | "<" | "<=" | "contains" | "not contains" | "in"
    operand     := string | number | true | false | null | bareword | path
    path        := name ( "." name | "[" int "]" )*

A bare ``path`` on its own is a truthiness test, so ``approved`` and ``not approved``
both read naturally. Either side may be a field path or a literal, so ``issues contains
architecture`` and ``'architecture' in issues`` both work.

**A bare word beside an operator means the word itself if it names nothing in scope.**
That is what makes the motivating example work: in ``issues contains architecture`` the
``issues`` field resolves, and there is no ``architecture`` field, so it means the word.
The same rule on either side makes ``architect in counts`` read correctly too. A quoted
string is always a literal, and multi-word operands must be quoted:
``summary contains 'not ready'``.

A bare word used **on its own** — a truthiness or ``is empty`` test — is only ever a field
path, so ``nope`` is false rather than becoming the truthy word "nope".

``contains`` is deliberately forgiving, because the data is usually prose written by a
model: on a string it is a case-insensitive substring test; on a list it matches an element
outright *or* any string element that contains the operand; on an object it tests for a key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BINOPS = ("==", "!=", ">=", "<=", ">", "<")
WORD_OPS = ("not contains", "contains", "in")
UNARY = ("is not empty", "is empty", "is true", "is false")
KEYWORDS = {"and", "or", "not", "is", "empty", "true", "false", "null", "contains", "in"}

_TOKEN = re.compile(
    r"""
    \s*(?:
        (?P<string>"[^"]*"|'[^']*')
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<op>==|!=|>=|<=|>|<|\(|\)|\[|\]|\.)
      | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    )
    """,
    re.VERBOSE,
)


class ExprError(ValueError):
    """The expression could not be parsed."""


@dataclass(frozen=True)
class Missing:
    """A path that does not resolve. Distinct from None, which is a real value."""

    path: str


MISSING = Missing("")


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(text, position)
        if not match or match.end() == position:
            raise ExprError(f"unexpected character {text[position]!r} at position {position}")
        position = match.end()
        for kind in ("string", "number", "op", "name"):
            value = match.group(kind)
            if value is not None:
                tokens.append((kind, value))
                break
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0

    # --- token helpers ---------------------------------------------------

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        position = self.index + offset
        return self.tokens[position] if position < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise ExprError("the expression ends too early")
        self.index += 1
        return token

    def at_word(self, word: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == "name" and token[1].lower() == word

    def take_phrase(self, phrase: str) -> bool:
        """Consume a multi-word operator like ``is not empty`` if it is next."""
        words = phrase.split()
        for offset, word in enumerate(words):
            token = self.peek(offset)
            if token is None or token[0] != "name" or token[1].lower() != word:
                return False
        self.index += len(words)
        return True

    # --- grammar ---------------------------------------------------------

    def parse(self) -> dict:
        node = self.parse_or()
        if self.peek() is not None:
            kind, value = self.peek()  # type: ignore[misc]
            raise ExprError(f"unexpected {value!r} after a complete expression")
        return node

    def parse_or(self) -> dict:
        node = self.parse_and()
        while self.at_word("or"):
            self.next()
            node = {"op": "or", "left": node, "right": self.parse_and()}
        return node

    def parse_and(self) -> dict:
        node = self.parse_not()
        while self.at_word("and"):
            self.next()
            node = {"op": "and", "left": node, "right": self.parse_not()}
        return node

    def parse_not(self) -> dict:
        if self.at_word("not"):
            # "not contains" is an operator, not a negation; only treat a leading
            # "not" as negation.
            self.next()
            return {"op": "not", "value": self.parse_not()}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        token = self.peek()
        if token is None:
            raise ExprError("the expression ends too early")
        if token == ("op", "("):
            self.next()
            node = self.parse_or()
            closing = self.peek()
            if closing != ("op", ")"):
                raise ExprError("missing a closing parenthesis")
            self.next()
            return node
        return self.parse_comparison()

    def parse_comparison(self) -> dict:
        left = self.parse_term()

        for phrase in UNARY:
            if self.take_phrase(phrase):
                return {"op": phrase.replace(" ", "_"), "left": left}

        for phrase in WORD_OPS:
            if self.take_phrase(phrase):
                return {"op": phrase.replace(" ", "_"), "left": left,
                        "operand": self.parse_operand()}

        token = self.peek()
        if token is not None and token[0] == "op" and token[1] in BINOPS:
            self.next()
            return {"op": token[1], "left": left, "operand": self.parse_operand()}

        return {"op": "truthy", "left": left}

    def parse_term(self) -> dict:
        """Either side of an operator: a literal, or a path that may also be a bare word."""
        token = self.peek()
        if token is None:
            raise ExprError("the expression ends too early")
        kind, value = token
        if kind == "string":
            self.next()
            return {"literal": value[1:-1]}
        if kind == "number":
            self.next()
            return {"literal": float(value) if "." in value else int(value)}
        if kind == "name" and value.lower() in ("true", "false", "null"):
            self.next()
            return {"literal": None if value.lower() == "null" else value.lower() == "true"}
        return {"path_or_word": self.parse_path()}

    def parse_path(self) -> list:
        token = self.peek()
        if token is None or token[0] != "name":
            got = token[1] if token else "nothing"
            raise ExprError(f"expected a field name, got {got!r}")
        if token[1].lower() in KEYWORDS - {"true", "false", "null"}:
            raise ExprError(f"{token[1]!r} is a keyword and cannot start a field path")
        self.next()
        parts: list = [token[1]]
        while True:
            nxt = self.peek()
            if nxt == ("op", "."):
                self.next()
                name = self.next()
                if name[0] != "name":
                    raise ExprError(f"expected a field name after '.', got {name[1]!r}")
                parts.append(name[1])
            elif nxt == ("op", "["):
                self.next()
                index = self.next()
                if index[0] != "number":
                    raise ExprError(f"expected a number inside [], got {index[1]!r}")
                closing = self.next()
                if closing != ("op", "]"):
                    raise ExprError("missing a closing ]")
                parts.append(int(index[1]))
            else:
                return parts

    def parse_operand(self) -> dict:
        token = self.peek()
        if token is None:
            raise ExprError("an operator is missing its right-hand side")
        kind, value = token
        if kind == "string":
            self.next()
            return {"literal": value[1:-1]}
        if kind == "number":
            self.next()
            return {"literal": float(value) if "." in value else int(value)}
        if kind == "name":
            lowered = value.lower()
            if lowered in ("true", "false"):
                self.next()
                return {"literal": lowered == "true"}
            if lowered == "null":
                self.next()
                return {"literal": None}
            # Either a field path or, if it names nothing in scope, a bare word.
            return {"path_or_word": self.parse_path()}
        raise ExprError(f"unexpected {value!r} on the right-hand side")


def parse(text: str) -> dict:
    """Parse an expression into a tree, raising :class:`ExprError` with the reason."""
    if not text or not text.strip():
        raise ExprError("the expression is empty")
    return _Parser(tokenize(text)).parse()


def roots(text: str) -> set[str]:
    """The first segment of every field path used, for validating against a schema."""
    found: set[str] = set()

    def walk(node: dict) -> None:
        for key in ("left", "operand"):
            term = node.get(key)
            if isinstance(term, dict):
                if "path" in term and term["path"]:
                    found.add(str(term["path"][0]))
                elif "path_or_word" in term and term["path_or_word"]:
                    found.add(str(term["path_or_word"][0]))
                elif "op" in term:
                    walk(term)
        for key in ("left", "right", "value"):
            child = node.get(key)
            if isinstance(child, dict) and "op" in child:
                walk(child)

    walk(parse(text))
    return found


# --------------------------------------------------------------- evaluation


def resolve(path: list, context: dict[str, Any]) -> Any:
    """Follow a dotted/indexed path, returning :data:`MISSING` if it does not exist."""
    current: Any = context
    for segment in path:
        if isinstance(segment, int):
            if isinstance(current, (list, tuple)) and -len(current) <= segment < len(current):
                current = current[segment]
            else:
                return MISSING
        elif isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return MISSING
    return current


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _contains(left: Any, operand: Any) -> bool:
    if left is MISSING or isinstance(left, Missing):
        return False
    needle = _text(operand).strip().lower()
    if isinstance(left, str):
        return needle in left.lower()
    if isinstance(left, dict):
        return any(needle == str(key).lower() for key in left)
    if isinstance(left, (list, tuple, set)):
        for item in left:
            if isinstance(item, str):
                if needle == item.strip().lower() or needle in item.lower():
                    return True
            elif item == operand:
                return True
        return False
    return needle == _text(left).strip().lower()


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is MISSING or isinstance(left, Missing):
        return op == "!="
    if op == "==":
        if isinstance(left, str) and isinstance(right, str):
            return left.strip().lower() == right.strip().lower()
        return left == right
    if op == "!=":
        return not _compare("==", left, right)
    try:
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
    except TypeError:
        return False
    raise ExprError(f"unknown operator {op!r}")


def _empty(value: Any) -> bool:
    if value is MISSING or isinstance(value, Missing) or value is None:
        return True
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) == 0
    return False


def _term_value(term: dict, context: dict[str, Any], *, strict: bool = False) -> Any:
    """A literal, a resolved path, or — beside an operator — a bare word taken literally.

    ``strict`` is for a term standing alone (truthiness, ``is empty``): there the word
    fallback would turn every typo into a truthy string, so a missing path stays missing.
    """
    if "literal" in term:
        return term["literal"]
    if "path" in term:
        return resolve(term["path"], context)
    path = term["path_or_word"]
    value = resolve(path, context)
    if (value is MISSING or isinstance(value, Missing)) and not strict:
        # Nothing of that name in scope, so the user meant the word itself.
        return ".".join(str(part) for part in path)
    return value


def _eval(node: dict, context: dict[str, Any]) -> bool:
    op = node["op"]
    if op == "and":
        return _eval(node["left"], context) and _eval(node["right"], context)
    if op == "or":
        return _eval(node["left"], context) or _eval(node["right"], context)
    if op == "not":
        return not _eval(node["value"], context)

    standalone = op in ("truthy", "is_empty", "is_not_empty", "is_true", "is_false")
    left = _term_value(node["left"], context, strict=standalone)
    if op == "truthy":
        return not _empty(left) and left is not False
    if op == "is_empty":
        return _empty(left)
    if op == "is_not_empty":
        return not _empty(left)
    if op == "is_true":
        return left is True
    if op == "is_false":
        return left is False

    operand = _term_value(node["operand"], context)
    if op == "contains":
        return _contains(left, operand)
    if op == "not_contains":
        return not _contains(left, operand)
    if op == "in":
        return _contains(operand, left)
    return _compare(op, left, operand)


def evaluate(text: str, context: dict[str, Any]) -> bool:
    """Evaluate an expression against a context of field names to values."""
    return _eval(parse(text), context)
