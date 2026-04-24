"""
Ticket distribution across accounts (NEW LOGIC v2):

The quantity typed by the user is the TOTAL number of tickets to book,
NOT a per-account quantity. The bot distributes that total across as
few accounts as possible, each taking up to `max_per_order`.

Example: user enters 8, max_per_order = 3, accounts = 10
  → account(a) gets 3, account(b) gets 3, account(c) gets 2, rest unused
  → total = 8 tickets (exactly what user asked)

Example: user enters 3, max_per_order = 5, accounts = 2
  → account(a) gets 3  (all on one account — others unused)
  → total = 3 tickets

Example: user enters 50, max_per_order = 3, accounts = 10
  → maximum achievable = 3 × 10 = 30 → clamp to 30
  → 10 accounts × 3 each = 30, clamped=True

The distribution is:
  • BIAS TOWARDS FEWER ACCOUNTS — fill one account to max, then the
    next, etc. Easier to track payments, lower risk of duplicate
    captcha challenges.
  • If `quantity > max_per_order × accounts_count`, the total is
    clamped to that ceiling and `clamped=True` is set in meta.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Assignment:
    account_id: str
    quantity: int


def distribute(total: int, *, accounts: list[dict], max_per_order: int,
               min_per_order: int = 1) -> tuple[list[Assignment], dict]:
    """
    Distribute `total` tickets across `accounts` so that each account
    takes between `min_per_order` and `max_per_order` tickets.

    Returns (plan, meta) where plan is ONLY the accounts that will
    actually book (empty accounts are filtered out).

    meta = {
        "requested_total":     int,   # what the user asked for
        "actual_total":        int,   # what we will actually book
        "clamped":             bool,  # True if actual_total < requested_total
        "accounts_available":  int,   # total ready accounts
        "accounts_used":       int,   # accounts actually used in plan
        "max_per_order":       int,
        "ceiling":             int,   # max_per_order * accounts_available
    }

    Raises ValueError if no accounts or invalid inputs.
    """
    if not accounts:
        raise ValueError("لا توجد حسابات جاهزة (سجّل دخول حساب أولاً)")
    if total <= 0:
        raise ValueError("العدد يجب أن يكون أكبر من صفر")
    if max_per_order <= 0:
        raise ValueError("الحد الأقصى لكل حساب غير صالح")

    accounts_count = len(accounts)
    ceiling = max_per_order * accounts_count

    # Clamp total to the ceiling
    actual_total = min(total, ceiling)
    clamped = actual_total < total

    # If actual_total < min_per_order, at least one account must take
    # min_per_order (webook rejects smaller orders).
    if actual_total < min_per_order:
        actual_total = min_per_order

    # Greedy fill: each account takes max_per_order until we satisfy the
    # total. The last account may take a partial amount ≥ min_per_order.
    remaining = actual_total
    plan: list[Assignment] = []
    for acc in accounts:
        if remaining <= 0:
            break
        take = min(max_per_order, remaining)
        # Don't create an assignment below min_per_order UNLESS this is
        # the only way to hit the user's requested total
        if take < min_per_order:
            # bump the previous account up by `take` if we can, else skip
            if plan and plan[-1].quantity + take <= max_per_order:
                plan[-1].quantity += take
                remaining = 0
            # else: drop the leftover (can happen when min>1 and remainder<min)
            break
        plan.append(Assignment(account_id=acc["id"], quantity=take))
        remaining -= take

    meta = {
        "requested_total": total,
        "actual_total": sum(a.quantity for a in plan),
        "clamped": clamped,
        "accounts_available": accounts_count,
        "accounts_used": len(plan),
        "max_per_order": max_per_order,
        "min_per_order": min_per_order,
        "ceiling": ceiling,
        # legacy keys kept for backward compat with callers that still
        # reference them (handlers.py was updated but safer to keep them)
        "actual_per_account": max_per_order,
        "total_tickets": sum(a.quantity for a in plan),
        "accounts_count": len(plan),
    }
    return plan, meta


def describe_plan(plan: list[Assignment], accounts: list[dict],
                  meta: dict) -> str:
    """Human-readable Arabic summary of the plan."""
    acc_by_id = {a["id"]: a for a in accounts}
    lines = []
    for a in plan:
        acc = acc_by_id.get(a.account_id, {})
        label = acc.get("label") or acc.get("email", a.account_id)
        # Truncate label to fit on a single line
        if len(label) > 35:
            label = label[:33] + "…"
        lines.append(f"• <code>{label}</code> → <b>{a.quantity}</b> تذكرة")

    # Add unused accounts note if applicable
    unused = meta["accounts_available"] - meta["accounts_used"]
    if unused > 0:
        lines.append(
            f"\n💡 <i>تم استخدام {meta['accounts_used']} من أصل "
            f"{meta['accounts_available']} حساب (الباقي {unused} لا نحتاجها).</i>"
        )

    if meta.get("clamped"):
        lines.append(
            f"\n⚠️ <i>طلبت {meta['requested_total']} تذكرة، لكن الحد الأقصى "
            f"الممكن هو {meta['ceiling']} "
            f"({meta['max_per_order']} × {meta['accounts_available']} حساب). "
            f"تم تقليل الطلب إلى {meta['actual_total']}.</i>"
        )
    return "\n".join(lines)
