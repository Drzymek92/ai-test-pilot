"""Deterministic AST mutation injector — the controlled half of the bug-detection eval.

Seeds ONE well-formed bug into a source file at a time (classic mutation-testing operators),
so a generated test suite can be measured by how many mutants it KILLS (a mutant is killed when
a test that passed on the original now fails). Stdlib `ast` only — no new deps, reproducible,
side-effect-free (it never imports or runs the target; it only rewrites source text), matching the
project's determinism-first / ast-only ethos (cf. `adapters/python_pytest.py`).

Usage:
    from benchmark.mutation import generate_mutants
    for m in generate_mutants(src, max_count=5, seed=0):
        ...   # m.source is the mutated module text; m.description names the change

A "mutant" is the original module with exactly one operator/constant flipped. We enumerate every
mutation site deterministically (stable AST walk order), then materialise one mutant per site; an
optional seeded subset keeps a large file's mutant count bounded without losing reproducibility.
"""
from __future__ import annotations

import ast
import random
from dataclasses import dataclass

# ── operator swap tables (each maps an AST op type to its single mutated replacement) ──
# Chosen so every swap is a CLEAR behaviour change on at least some input (off-by-one, sign flip,
# inverted comparison/boolean) — the kinds of real bugs a test ought to catch.
_BINOP_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.Div, ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult, ast.Mod: ast.Add,
}
_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.In: ast.NotIn, ast.NotIn: ast.In,          # membership bugs
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,          # identity bugs
}
_BOOL_SWAP: dict[type[ast.boolop], type[ast.boolop]] = {
    ast.And: ast.Or, ast.Or: ast.And,
}
# Builtin call swaps (function-name level): a common boundary-logic bug shape.
_CALL_SWAP: dict[str, str] = {"min": "max", "max": "min"}


@dataclass(frozen=True)
class Mutant:
    """One injected bug: the mutated module source plus a human-readable description."""
    index: int
    operator: str          # the operator family, e.g. "binop" | "compare" | "return"
    description: str        # e.g. "Add->Sub @line 12"
    source: str


class _Mutator(ast.NodeTransformer):
    """Applies the single mutation at `target` site index; counts sites when target < 0.

    The walk order is fixed by NodeTransformer's field traversal, so the same source always yields
    the same site numbering — re-parsing per mutant (rather than deep-copying) keeps each mutant a
    clean single-edit variant.
    """

    def __init__(self, target: int, ranges: list[tuple[int, int]] | None = None) -> None:
        self.target = target
        self.ranges = ranges                          # line ranges of in-scope functions (None = whole file)
        self.counter = 0
        self.applied: tuple[str, str] | None = None   # (operator_family, description)

    def _in_scope(self, lineno: int) -> bool:
        return self.ranges is None or any(lo <= lineno <= hi for lo, hi in self.ranges)

    def _site(self, family: str, desc: str, lineno: int) -> bool:
        """Register an in-scope mutation opportunity; return True iff THIS one should be applied.

        Out-of-scope sites (outside the selected functions) are NOT counted, so the suite is only
        ever measured against code it actually targets — a mutant in an untested function is
        unkillable by construction and would otherwise deflate the kill rate."""
        if not self._in_scope(lineno):
            return False
        hit = self.counter == self.target
        self.counter += 1
        if hit:
            self.applied = (family, desc)
        return hit

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        swap = _BINOP_SWAP.get(type(node.op))
        if swap is not None and self._site(
                "binop", f"{type(node.op).__name__}->{swap.__name__} @line {node.lineno}", node.lineno):
            node.op = swap()
        return node

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        self.generic_visit(node)
        swap = _BINOP_SWAP.get(type(node.op))
        if swap is not None and self._site(
                "augassign", f"{type(node.op).__name__}=->{swap.__name__}= @line {node.lineno}", node.lineno):
            node.op = swap()
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            swap = _CMP_SWAP.get(type(op))
            if swap is not None and self._site(
                    "compare", f"{type(op).__name__}->{swap.__name__} @line {node.lineno}", node.lineno):
                node.ops[i] = swap()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        swap = _BOOL_SWAP.get(type(node.op))
        if swap is not None and self._site(
                "boolop", f"{type(node.op).__name__}->{swap.__name__} @line {node.lineno}", node.lineno):
            node.op = swap()
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        # bool first: bool is a subclass of int, so flip True<->False before the numeric branch.
        if isinstance(node.value, bool):
            if self._site("constant", f"{node.value}->{not node.value} @line {node.lineno}", node.lineno):
                node.value = not node.value
        elif isinstance(node.value, (int, float)) and not isinstance(node.value, complex):
            new = node.value + 1
            if self._site("constant", f"{node.value}->{new} @line {node.lineno}", node.lineno):
                node.value = new
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in _CALL_SWAP:
            swapped = _CALL_SWAP[node.func.id]
            if self._site("call", f"{node.func.id}()->{swapped}() @line {node.lineno}", node.lineno):
                node.func = ast.Name(id=swapped, ctx=ast.Load())
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        # Only a meaningful mutation when something non-None is returned.
        if node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            if self._site("return", f"return <expr>->None @line {node.lineno}", node.lineno):
                node.value = ast.Constant(value=None)
        return node


def _scope_ranges(source: str, selector: set[str] | None) -> list[tuple[int, int]] | None:
    """Line ranges of the selected TOP-LEVEL functions (None = mutate the whole file).

    Mirrors the python adapter's top-level-function introspection, so mutants land only in the code
    the generated suite actually targets — the fairness fix that makes kill rate meaningful."""
    if not selector:
        return None
    tree = ast.parse(source)
    ranges = [(n.lineno, n.end_lineno or n.lineno) for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in selector]
    return ranges


def count_sites(source: str, *, selector: set[str] | None = None) -> int:
    """How many in-scope single-mutation sites this source has (deterministic)."""
    m = _Mutator(target=-1, ranges=_scope_ranges(source, selector))
    m.visit(ast.parse(source))
    return m.counter


def _make_mutant(source: str, index: int, ranges: list[tuple[int, int]] | None) -> Mutant | None:
    m = _Mutator(target=index, ranges=ranges)
    tree = m.visit(ast.parse(source))
    if m.applied is None:
        return None
    ast.fix_missing_locations(tree)
    family, desc = m.applied
    return Mutant(index=index, operator=family, description=desc, source=ast.unparse(tree))


_VALIDATOR_DECOS = {"field_validator", "model_validator", "validator", "root_validator"}


def _validator_method(m: ast.AST) -> bool:
    """True iff `m` is a pydantic validator method (`@field_validator`/`@model_validator`/…)."""
    if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for d in m.decorator_list:
        callee = d.func if isinstance(d, ast.Call) else d
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if name in _VALIDATOR_DECOS:
            return True
    return False


def _weaken_validator(source: str, cls_name: str, method_name: str) -> str:
    """Replace a validator's body with `return <last arg>` — it now ACCEPTS anything (guard removed)."""
    tree = ast.parse(source)

    class _T(ast.NodeTransformer):
        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
            if node.name == cls_name:
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method_name:
                        allp = list(m.args.posonlyargs) + list(m.args.args)
                        ret: ast.expr = ast.Name(allp[-1].arg, ast.Load()) if allp else ast.Constant(None)
                        m.body = [ast.Return(ret)]
            return node

    ast.fix_missing_locations(_T().visit(tree))
    return ast.unparse(tree)


def validator_weakening_mutants(source: str) -> list[Mutant]:
    """Mutants that WEAKEN each pydantic validator (drop its guard → accepts out-of-contract values).

    This is the bug class a valid-only suite is blind to (every valid input still passes) and a rejection-test
    rejection test catches (an invalid input is now wrongly accepted, so the `pytest.raises` fails).
    Distinct from the operator-swap `generate_mutants` — a structural weakening, the deletion-style
    operator the publish plan flagged. ast-only, deterministic (one mutant per validator method)."""
    tree = ast.parse(source)
    out: list[Mutant] = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for m in cls.body:
            if _validator_method(m):
                mutated = _weaken_validator(source, cls.name, m.name)
                if mutated != source:
                    out.append(Mutant(index=len(out), operator="validator",
                                      description=f"weaken validator {cls.name}.{m.name}", source=mutated))
    return out


def generate_mutants(source: str, *, selector: set[str] | None = None,
                     max_count: int | None = None, seed: int = 0) -> list[Mutant]:
    """All single-operator mutants of `source` (or a seeded subset of `max_count`).

    `selector` (function names) scopes mutation to those top-level functions only — pass the same
    selector the suite was generated for so every mutant is in code the tests actually exercise.
    Deterministic: site enumeration is fixed by AST walk order, and the optional subset is a seeded
    `random.sample` so the same (source, selector, max_count, seed) always yields the same mutants.
    """
    ranges = _scope_ranges(source, selector)
    n = count_sites(source, selector=selector)
    indices = list(range(n))
    if max_count is not None and n > max_count:
        indices = sorted(random.Random(seed).sample(indices, max_count))
    out: list[Mutant] = []
    for i in indices:
        mut = _make_mutant(source, i, ranges)
        if mut is not None and mut.source != source:
            out.append(mut)
    return out
