"""Synthetic Approach-3 target — plain (NON-dataclass) domain classes + pure computed functions.

Manufactured to MEASURE A3 (usage-guided typed-input construction). Pre-A3 a param typed with a plain
class was warn-and-skip (green=0) → the suite couldn't catch ANY bug there. A3's init-class strategy
constructs the real object from its `__init__` signature (and nests, e.g. `Order(items=[Money(...)])`),
so the generated suite runs and mutation kill rate becomes measurable. Kept dependency-free and
deterministic so it slots straight into the detection corpus via `benchmark/detection_targets_a3.toml`.

The functions are deliberately arithmetic/comparison/boolean-heavy so the AST mutation operators
(arith/comparison/boolean/min-max/return) seed catchable bugs. The classes are PLAIN (no @dataclass,
no pydantic) on purpose — that is exactly the construction gap A3(b) closes.
"""
from __future__ import annotations


class Money:
    def __init__(self, amount: int, currency: str = "USD"):
        self.amount = amount
        self.currency = currency


class Order:
    def __init__(self, items, shipping: Money, discount_pct: int = 0):
        self.items = items                 # list[Money]
        self.shipping = shipping           # Money
        self.discount_pct = discount_pct   # whole-percent discount on the goods subtotal


class TreeNode:
    def __init__(self, value: int, children=None):
        self.value = value
        self.children = children or []     # list[TreeNode]


def subtotal(order: Order) -> int:
    """Sum of the item amounts (before shipping / discount)."""
    return sum(item.amount for item in order.items)


def grand_total(order: Order) -> int:
    """Discounted goods subtotal plus shipping. Discount applies to goods only, not shipping."""
    goods = sum(item.amount for item in order.items)
    discounted = goods - goods * order.discount_pct // 100
    return discounted + order.shipping.amount


def qualifies_free_shipping(order: Order, threshold: int) -> bool:
    """True when the goods subtotal reaches the free-shipping threshold."""
    return subtotal(order) >= threshold


def heaviest_item(order: Order) -> Money:
    """The single most expensive item (ties resolve to the first seen)."""
    best = order.items[0]
    for item in order.items[1:]:
        if item.amount > best.amount:
            best = item
    return best


def sum_tree(root: TreeNode) -> int:
    """Sum of every node's value in the tree (root + all descendants)."""
    return root.value + sum(sum_tree(child) for child in root.children)


def max_depth(root: TreeNode) -> int:
    """Number of nodes on the longest root-to-leaf path (a leaf is depth 1)."""
    if not root.children:
        return 1
    return 1 + max(max_depth(child) for child in root.children)
