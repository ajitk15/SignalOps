"""Cost accounting and the ceilings that stop a run spending without bound.

A workflow that loops — analyse, test, fix, re-test — can in principle spend
until someone notices. Two ceilings, both hard stops rather than warnings:
per-run, so one pathological ticket cannot drain the month; and per-workspace,
so a hundred well-behaved runs cannot either.

Cost is computed from reported usage, never estimated from prompt length. An
estimate that drifts low is a budget that does not hold.
"""
from __future__ import annotations

# USD per million tokens, (input, output). Kept next to the enforcement rather
# than in config: a wrong number here silently under-bills every ceiling above.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}

# Conservative default. A single incident diagnosis costs cents; a run that has
# reached a dollar has almost certainly gone wrong rather than gone deep.
DEFAULT_RUN_BUDGET_USD = 1.00


class BudgetExceeded(Exception):
    """Raised when continuing would spend past a ceiling. Halts the run."""


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD for one call.

    An unknown model is priced at the most expensive rate we know rather than
    zero — an unpriced model must not be a way to run for free past a budget.
    """
    rates = MODEL_PRICING.get(model)
    if rates is None:
        rates = max(MODEL_PRICING.values(), key=lambda r: r[1])
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def check(*, spent_run: float, run_budget: float | None,
          spent_workspace: float = 0.0, workspace_budget: float | None = None) -> None:
    """Raise if either ceiling has been reached. Called before each model call.

    Checked *before* rather than after, so the call that would break the budget
    is the one that does not happen.
    """
    if run_budget is not None and spent_run >= run_budget:
        raise BudgetExceeded(
            f"run has spent ${spent_run:.4f} of its ${run_budget:.2f} budget")
    if workspace_budget is not None and spent_workspace >= workspace_budget:
        raise BudgetExceeded(
            f"workspace has spent ${spent_workspace:.4f} of its "
            f"${workspace_budget:.2f} budget")
