"""Rules as expressions that the engine both evaluates and shows to clients.

A rule is one Python-syntax expression, for example:

    actor.role == 'admin' or record.owner == actor.id
    record.primary_contact is None or record.primary_contact.organization == record.organization
    not any(d.disposition == 'open' for d in Deviation if d.instrument == record.id)

Names: `actor` is who is acting, `record` is the record being judged, and `saved`
is its stored state (None while it is being created). A reference field is
followed with a dot. A missing value anywhere in a path gives None. Functions:
`changed('field')`, `can_read(record)`, `count(...)`, and `any(...)` over a
generator of another type's live records.

Nothing here executes Python. The expression is parsed into a syntax tree, the
tree is checked against the forms below when the definition loads, and this
module walks it.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from typing import Any, Callable


class RuleError(ValueError):
    """A rule that this language cannot express. Raised when a definition loads."""


class _Unknown:
    """A value that depends on a record not yet chosen. See `possible`."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()
_COMPARISONS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_FUNCTIONS = ("changed", "can_read", "count", "any")


class Node:
    """One record as a rule sees it. Reference fields lead to further nodes."""

    __slots__ = ("type", "id", "_world", "_record")

    def __init__(self, world: "World", type_name: str, record_id: str, record: dict[str, Any] | None = None) -> None:
        self.type, self.id, self._world, self._record = type_name, record_id, world, record

    def data(self) -> dict[str, Any]:
        if self._record is None:
            self._record = self._world.lookup(self.type, self.id) or {"data": {}}
        return self._record["data"]

    def field(self, name: str) -> Any:
        if name == "id":
            return self.id
        if name == "type":
            return self.type
        spec = self._world.types[self.type]["fields"].get(name)
        if spec is None:
            raise RuleError(f"{self.type} has no field {name}.")
        value = self.data().get(name)
        if value is None:
            return None
        if spec["type"] == "reference":
            return Node(self._world, spec["target"], value)
        if spec["type"] == "list" and spec["items"]["type"] == "reference":
            return [Node(self._world, spec["items"]["target"], item) for item in value]
        return value

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Node):
            return (self.type, self.id) == (other.type, other.id)
        return self.id == other

    def __hash__(self) -> int:
        return hash(self.id)


class World:
    """What rules may consult: the types, record lookups, and the read rule."""

    def __init__(
        self, types: dict[str, Any], lookup: Callable[[str, str], dict[str, Any] | None],
        live: Callable[..., list[dict[str, Any]]],
    ) -> None:
        # live(type, terms) returns that type's live records. It may use the terms, as
        # produced by Evaluation.narrowing, to leave out records that cannot match.
        self.types, self.lookup, self.live = types, lookup, live

    def node(self, record: dict[str, Any] | None) -> Node | None:
        return None if record is None else Node(self, record["type"], record["id"], record)


@lru_cache(maxsize=None)
def parse(expression: str, types: tuple[str, ...]) -> ast.expr:
    """Parse and check one rule. `types` are the names a generator may range over."""
    if not isinstance(expression, str) or not expression.strip():
        raise RuleError("A rule must be a nonempty expression.")
    try:
        tree = ast.parse(expression, mode="eval").body
    except SyntaxError as error:
        raise RuleError(f"Cannot parse rule: {expression}") from error
    _check(tree, expression, set(types), {"actor", "record", "saved"})
    return tree


def _check(node: ast.AST, source: str, types: set[str], names: set[str]) -> None:
    def bad(what: str) -> None:
        raise RuleError(f"{what} is not allowed in a rule: {source}")

    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value, source, types, names)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check(node.operand, source, types, names)
    elif isinstance(node, ast.Compare):
        if not all(isinstance(operator, _COMPARISONS) for operator in node.ops):
            bad("This comparison")
        for value in (node.left, *node.comparators):
            _check(value, source, types, names)
    elif isinstance(node, ast.Attribute):
        _check(node.value, source, types, names)
    elif isinstance(node, ast.Name):
        if node.id not in names:
            bad(f"The name {node.id}")
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int, bool, type(None))):
            bad("This constant")
    elif isinstance(node, (ast.Tuple, ast.List)):
        for value in node.elts:
            _check(value, source, types, names)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords or len(node.args) != 1:
            bad("This call")
        argument = node.args[0]
        if node.func.id == "changed":
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                bad("changed() without a field name")
        elif node.func.id in ("count", "any"):
            if not isinstance(argument, ast.GeneratorExp) or len(argument.generators) != 1:
                bad("count() or any() without one generator")
            loop = argument.generators[0]
            if not isinstance(loop.target, ast.Name) or not isinstance(loop.iter, ast.Name) or loop.iter.id not in types or loop.is_async:
                bad("A generator that does not range over a record type")
            inner = names | {loop.target.id}
            for value in (argument.elt, *loop.ifs):
                _check(value, source, types, inner)
        else:
            _check(argument, source, types, names)
    else:
        bad(type(node).__name__)


class Evaluation:
    """One rule evaluated for one actor and record."""

    def __init__(
        self, world: World, actor: Any, record: Any, saved: Any,
        can_read: Callable[[Node], bool] | None = None,
    ) -> None:
        self.world, self.can_read = world, can_read
        self.names = {"actor": actor, "record": record, "saved": saved}

    def run(self, expression: str) -> Any:
        return self._value(parse(expression, tuple(self.world.types)), self.names)

    def _value(self, node: ast.AST, names: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names[node.id]
        if isinstance(node, (ast.Tuple, ast.List)):
            return [self._value(value, names) for value in node.elts]
        if isinstance(node, ast.Attribute):
            owner = self._value(node.value, names)
            if owner is UNKNOWN or owner is None:
                return owner
            if not isinstance(owner, Node):
                raise RuleError(f"Cannot read .{node.attr} of a plain value.")
            return owner.field(node.attr)
        if isinstance(node, ast.UnaryOp):
            value = self._value(node.operand, names)
            return UNKNOWN if value is UNKNOWN else not value
        if isinstance(node, ast.BoolOp):
            return self._all_or_any(node, names)
        if isinstance(node, ast.Compare):
            left, result = self._value(node.left, names), True
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._value(comparator, names)
                step = self._compare(operator, left, right)
                if step is False:
                    return False
                if step is UNKNOWN:
                    result = UNKNOWN
                left = right
            return result
        return self._call(node, names)

    def narrowing(self, conditions: list[ast.AST], variable: str, names: dict[str, Any]) -> list[tuple[tuple[str, ...], str, Any]]:
        """Terms every match must satisfy, as (field path, "eq" or "in", value).

        Taken from the conditions that are joined by `and` and compare a path on
        `variable` with something that does not depend on it. Storage can use
        them to find candidates through an index. Every candidate is still
        judged by the full conditions, so leaving a term out is always safe.
        """
        pending, terms = list(conditions), []
        while pending:
            node = pending.pop()
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                pending.extend(node.values)
                continue
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.In)):
                continue
            left, right = node.left, node.comparators[0]
            if isinstance(node.ops[0], ast.Eq) and _path(left, variable) is None:
                left, right = right, left
            path = _path(left, variable)
            if path is None or len(path) > 2 or _mentions(right, variable):
                continue
            value = self._value(right, names)
            if isinstance(node.ops[0], ast.In):
                if not isinstance(value, list) or any(item is UNKNOWN for item in value):
                    continue
                terms.append((path, "in", [item.id if isinstance(item, Node) else item for item in value]))
            elif value is not UNKNOWN and not isinstance(value, list):
                terms.append((path, "eq", value.id if isinstance(value, Node) else value))
        return terms

    def _all_or_any(self, node: ast.BoolOp, names: dict[str, Any]) -> Any:
        # Kleene logic: a known False decides `and`, a known True decides `or`.
        decides = isinstance(node.op, ast.Or)
        unknown = False
        for value_node in node.values:
            value = self._value(value_node, names)
            if value is UNKNOWN:
                unknown = True
            elif bool(value) is decides:
                return decides
        return UNKNOWN if unknown else not decides

    @staticmethod
    def _compare(operator: ast.cmpop, left: Any, right: Any) -> Any:
        if isinstance(operator, (ast.Is, ast.IsNot)):
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            same = (left is None and right is None) if (left is None or right is None) else left == right
            return same == isinstance(operator, ast.Is)
        if left is UNKNOWN or right is UNKNOWN or (isinstance(right, list) and any(item is UNKNOWN for item in right)):
            return UNKNOWN
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, (ast.In, ast.NotIn)):
            if isinstance(right, str):
                # Text contains text, ignoring case, as a person searching by name expects.
                found = isinstance(left, str) and left.casefold() in right.casefold()
            else:
                found = right is not None and not isinstance(right, Node) and any(left == item for item in right)
            return found == isinstance(operator, ast.In)
        if left is None or right is None or isinstance(left, Node) or isinstance(right, Node):
            return False
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        return left >= right

    def _call(self, node: ast.Call, names: dict[str, Any]) -> Any:
        name, argument = node.func.id, node.args[0]
        if name == "changed":
            record, saved = names["record"], names["saved"]
            if record is UNKNOWN or saved is UNKNOWN:
                return UNKNOWN
            return saved is None or record.field(argument.value) != saved.field(argument.value)
        if name == "can_read":
            target = self._value(argument, names)
            if target is UNKNOWN:
                return UNKNOWN
            return target is not None and self.can_read is not None and self.can_read(target)
        loop = argument.generators[0]
        total, unknown = 0, False
        for record in self.world.live(loop.iter.id, self.narrowing(loop.ifs, loop.target.id, names)):
            if total and name == "any":
                break
            inner = {**names, loop.target.id: self.world.node(record)}
            conditions = [self._value(condition, inner) for condition in loop.ifs]
            if any(condition is not UNKNOWN and not condition for condition in conditions):
                continue
            value = True if name == "count" else self._value(argument.elt, inner)
            if value is UNKNOWN or any(condition is UNKNOWN for condition in conditions):
                unknown = True
            elif value:
                total += 1
        if name == "count":
            return UNKNOWN if unknown else total
        return True if total else (UNKNOWN if unknown else False)


def _mentions(node: ast.AST, variable: str) -> bool:
    return any(isinstance(inner, ast.Name) and inner.id == variable for inner in ast.walk(node))


def _path(node: ast.AST, variable: str) -> tuple[str, ...] | None:
    """The fields of `variable.a.b`, or None when the node is not such a path."""
    fields: list[str] = []
    while isinstance(node, ast.Attribute):
        fields.append(node.attr)
        node = node.value
    return tuple(reversed(fields)) if fields and isinstance(node, ast.Name) and node.id == variable else None


def holds(evaluation: Evaluation, expression: str) -> bool:
    """Whether a rule is satisfied for a known actor and record."""
    value = evaluation.run(expression)
    return value is not UNKNOWN and bool(value)


def possible(world: World, actor: Node, expression: str) -> bool:
    """Whether a rule could be satisfied for this actor by some record.

    False only when the actor alone rules it out, as `actor.role == 'admin'`
    does for a viewer. This is what a type's capabilities report.
    """
    return Evaluation(world, actor, UNKNOWN, UNKNOWN).run(expression) is not False
