"""
Shopping cart — in-memory ephemeral cart per Telegram chat.

The interactive flow is:

  1) /start → choose football match           ┐
  2) choose team (home/away/VIP)              │
  3) choose sector                             ├─ stored in cart
  4) choose price tier (if multiple)          │
  5) enter ticket quantity                    │
  6) review screen ──── confirm? ─────────────┴─→ booking_orchestrator

Until step 6 the user has not booked anything. The cart is a transient
object held in :pydata:`_carts` keyed by chat-id. It is cleared on any
of: confirm-and-book, cancel, /start, fresh login, or 30-min idle.

NOTE — we deliberately keep this layer in-memory (not Postgres). Reason:
  • Carts are ephemeral by design (booking happens within seconds)
  • Bot is single-process on Render free tier (no need for Redis)
  • All persistent state already lives in `bookings` after confirm
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ════════════════════════════════════════════════════════════════════════
# Cart shape
# ════════════════════════════════════════════════════════════════════════
@dataclass
class CartItem:
    # Event identity
    event_slug: str = ""
    event_title: str = ""
    event_id: str = ""              # webook _id (for cart/checkout)
    is_seated: bool = True

    # Teams (resolved from event-detail)
    team_a_id: str = ""
    team_a_name: str = ""
    team_a_logo: str = ""
    team_b_id: str = ""
    team_b_name: str = ""
    team_b_logo: str = ""
    chosen_side: str = ""           # "home" | "away" | "vip" | "neutral"
    chosen_team_label: str = ""     # human-friendly label for review

    # Selected sector (1 ticket type from event_ticket[])
    ticket_id: str = ""
    ticket_title: str = ""
    ticket_color: str = ""
    seats_io_category: str = ""
    group_name: str = ""
    price: float = 0.0
    currency: str = "SAR"
    max_per_order: int = 5
    min_per_order: int = 1

    # User-entered quantity
    quantity: int = 0

    # Bookkeeping
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# Keyed by chat_id
_carts: dict[str, CartItem] = {}

# Auto-expire stale carts (default: 30 minutes)
CART_TTL_SECONDS = 30 * 60


# ════════════════════════════════════════════════════════════════════════
# CRUD-style API
# ════════════════════════════════════════════════════════════════════════
def get(chat_id: str) -> Optional[CartItem]:
    """Return the live cart for *chat_id* or ``None`` if none/expired."""
    c = _carts.get(str(chat_id))
    if not c:
        return None
    if time.time() - c.updated_at > CART_TTL_SECONDS:
        _carts.pop(str(chat_id), None)
        return None
    return c


def get_or_new(chat_id: str) -> CartItem:
    """Return the cart, creating a fresh empty one if missing/expired."""
    c = get(chat_id)
    if c is None:
        c = CartItem()
        _carts[str(chat_id)] = c
    return c


def update(chat_id: str, **fields) -> CartItem:
    """Patch the cart for *chat_id* and bump ``updated_at``."""
    c = get_or_new(chat_id)
    for k, v in fields.items():
        if hasattr(c, k):
            setattr(c, k, v)
    c.updated_at = time.time()
    return c


def clear(chat_id: str) -> None:
    """Remove the cart for *chat_id* (idempotent)."""
    _carts.pop(str(chat_id), None)


def to_dict(chat_id: str) -> dict:
    """Serialise cart for logging / debugging."""
    c = get(chat_id)
    return asdict(c) if c else {}


# ════════════════════════════════════════════════════════════════════════
# Maintenance
# ════════════════════════════════════════════════════════════════════════
def gc() -> int:
    """Drop expired carts. Returns how many were removed."""
    now = time.time()
    dead = [cid for cid, c in _carts.items()
            if now - c.updated_at > CART_TTL_SECONDS]
    for cid in dead:
        _carts.pop(cid, None)
    return len(dead)
