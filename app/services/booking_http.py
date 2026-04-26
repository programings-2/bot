"""
Direct HTTP booking engine — bypasses Playwright entirely.

Discovered APIs (via reverse engineering of live webook.com traffic):
  GET  /api/v2/event-detail/{slug}                           → event_id, time_slots(dates)
  GET  /api/v2/event-ticket-details/{slug}                   → tickets, is_seated
  GET  /api/v2/event-detail/{slug}/timeslot-capacity?time_slot={date}
                                                              → time_slot_id(s)
  POST /api/v2/cart/add-to-cart                              → add ticket(s) to cart
  POST /api/v2/event-detail/{slug}/checkout                  → returns PayTabs URL

Key insight:
  The `authorization: Bearer <jwt>` + `token: <public>` + `accept-language: ar-SA`
  + browser-like `sec-ch-ua` headers are REQUIRED. Otherwise the edge
  returns 403 (Cloudflare bot protection).

Benefits vs Playwright:
  • ~20× faster (no browser to boot)
  • Uses ~5 MB RAM instead of ~250 MB
  • No reCAPTCHA during booking (only at login, handled separately)
  • Concurrency-friendly: N accounts bookable in parallel trivially
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp

from app.core.config import WEBOOK_API, WEBOOK_ORIGIN, WEBOOK_PUBLIC_TOKEN

log = logging.getLogger("booking_http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def build_headers(bearer: str, lang: str = "en") -> dict[str, str]:
    """Headers that pass Cloudflare and webook's edge checks."""
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": DEFAULT_UA,
        "accept-language": "ar-SA",  # ← required; ar-SA OR ar passes, pure 'en' gets blocked
        "authorization": f"Bearer {bearer}" if bearer else "Bearer",
        "token": WEBOOK_PUBLIC_TOKEN,
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Chromium";v="128"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }


# ════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ════════════════════════════════════════════════════════════════════════
async def _get(session: aiohttp.ClientSession, url: str, bearer: str,
               timeout: int = 15) -> tuple[int, Any]:
    try:
        async with session.get(
            url, headers=build_headers(bearer),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:600]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


async def _post(session: aiohttp.ClientSession, url: str, bearer: str,
                body: dict, timeout: int = 25) -> tuple[int, Any]:
    try:
        async with session.post(
            url, headers=build_headers(bearer), json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:600]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


# ════════════════════════════════════════════════════════════════════════
# Discovery (meta about event for booking)
# ════════════════════════════════════════════════════════════════════════
async def fetch_event_meta(session: aiohttp.ClientSession, slug: str,
                            bearer: str) -> dict[str, Any]:
    """Return {event_id, is_seated, time_slot_dates, booking_seats_without_map, title}."""
    url = f"{WEBOOK_API}/event-detail/{slug}?lang=en&visible_in=rs"
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return {}
    d = data.get("data") or {}
    return {
        "event_id": d.get("_id"),
        "title": d.get("title") or slug,
        "is_seated": bool(d.get("is_seated")),
        "booking_seats_without_map": bool(d.get("booking_seats_without_map")),
        "time_slot_dates": list(d.get("time_slots") or []),
        "is_experience": bool(d.get("is_experience")),
        "require_visa": bool(d.get("require_visa")),
    }


async def fetch_timeslot_id(session: aiohttp.ClientSession, slug: str,
                             date_str: str, ticket_id: str,
                             bearer: str) -> Optional[str]:
    """Return the _id of the first time slot for the given date (YYYY-MM-DD)
    whose capacity for `ticket_id` is > 0 (or unknown)."""
    url = (f"{WEBOOK_API}/event-detail/{slug}/timeslot-capacity"
           f"?time_slot={date_str}&visible_in=rs&lang=en")
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return None
    slots = data.get("data") or []
    for s in slots:
        if s.get("is_soldout"):
            continue
        cap = s.get(ticket_id)
        # cap may be None/-1 (unlimited) or >0 (available)
        if cap is None or cap == -1 or (isinstance(cap, (int, float)) and cap > 0):
            return s.get("_id")
    # fallback: return first slot regardless
    return slots[0].get("_id") if slots else None


# ════════════════════════════════════════════════════════════════════════
# Cart
# ════════════════════════════════════════════════════════════════════════
async def add_to_cart(session: aiohttp.ClientSession, *,
                      ticket_id: str, quantity: int,
                      parent_event_id: str, time_slot_id: Optional[str],
                      bearer: str) -> tuple[bool, Any]:
    """Add a ticket line to the user's cart. Returns (ok, data)."""
    body = {
        "ticket_id": ticket_id,
        "quantity": quantity,
        "type": "ticket",
        "parent_event_id": parent_event_id,
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id

    url = f"{WEBOOK_API}/cart/add-to-cart?lang=en"
    status, data = await _post(session, url, bearer, body)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data


async def get_cart(session: aiohttp.ClientSession, parent_event_id: str,
                    bearer: str) -> dict:
    url = (f"{WEBOOK_API}/cart/cart-items"
           f"?lang=en&parent_event_id={parent_event_id}")
    status, data = await _get(session, url, bearer)
    return (data or {}).get("data") or {} if status == 200 else {}


async def clear_cart(session: aiohttp.ClientSession, parent_event_id: str,
                      bearer: str) -> None:
    """Best-effort cart clear (used before retry)."""
    for url in [
        f"{WEBOOK_API}/cart/clear?lang=en&parent_event_id={parent_event_id}",
        f"{WEBOOK_API}/cart/clear-cart?lang=en&parent_event_id={parent_event_id}",
    ]:
        try:
            async with session.post(url, headers=build_headers(bearer),
                                     timeout=aiohttp.ClientTimeout(total=8)):
                pass
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════
# Checkout
# ════════════════════════════════════════════════════════════════════════
async def create_checkout(session: aiohttp.ClientSession, *,
                          slug: str, event_id: str,
                          ticket_id: str, quantity: int,
                          time_slot_id: Optional[str],
                          bearer: str,
                          payment_method: str = "credit_card",
                          ) -> tuple[bool, dict]:
    """
    Create a payment session → returns the PayTabs redirect URL.

    Response (success):
      {"status":"success","data":{
         "payment_session_id": "...",
         "order_id": "...",
         "payment_provider": "paytabs",
         "redirect_url": "https://secure-webook.paytabs.com/payment/page/..."
      }}
    """
    body = {
        "event_id": event_id,
        "redirect": f"{WEBOOK_ORIGIN}/en/payment-success",
        "redirect_failed": f"{WEBOOK_ORIGIN}/en/payment-failed",
        "booking_source": "rs-web",
        "lang": "en",
        "payment_method": payment_method,
        "is_wallet": False,
        "saudi_redeem": None,
        "refund_guarantee": False,
        "perks": [],
        "merchandise": [],
        "addons": [],
        "vouchers": [],
        "tickets": [{"qty": quantity, "id": ticket_id}],
        "app_source": "rs",
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id

    url = f"{WEBOOK_API}/event-detail/{slug}/checkout?lang=en"
    status, data = await _post(session, url, bearer, body, timeout=30)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data or {}


# ════════════════════════════════════════════════════════════════════════
# Seats (for seated events) — selection + hold
# ════════════════════════════════════════════════════════════════════════
async def fetch_seat_map_token(session: aiohttp.ClientSession, slug: str,
                                ticket_id: str, bearer: str) -> dict:
    """For seats.io-backed events webook embeds a short-lived session.
    We look for the embed init endpoint first; if it's not available
    we'll just fall back to best-available hold via the normal checkout
    call (which accepts `selected_seats` under the hood)."""
    url = (f"{WEBOOK_API}/event-detail/{slug}/seats-session"
           f"?ticket_id={ticket_id}&lang=en")
    status, data = await _get(session, url, bearer)
    if status == 200 and isinstance(data, dict):
        return data.get("data") or {}
    return {}


async def hold_adjacent_seats(session: aiohttp.ClientSession, *,
                              slug: str, event_id: str,
                              ticket_id: str, quantity: int,
                              time_slot_id: Optional[str],
                              bearer: str) -> tuple[bool, list[str]]:
    """
    Best-effort: try a few known webook seat-hold endpoints to
    automatically hold adjacent seats. Returns (ok, seat_labels).

    Webook's seated flow (sports/concerts) relies on seats.io and the
    exact hold endpoint is not always reachable without the seats.io
    client SDK. For MVP we return False so the caller falls back to the
    non-seated `/checkout` flow (which webook accepts for events where
    `booking_seats_without_map=true`).
    """
    candidates = [
        ("POST", f"{WEBOOK_API}/seats/hold", {
            "event_id": event_id, "ticket_id": ticket_id,
            "quantity": quantity, "time_slot_id": time_slot_id,
            "selection_mode": "best_available_adjacent",
        }),
        ("POST", f"{WEBOOK_API}/event-detail/{slug}/seats/hold", {
            "ticket_id": ticket_id,
            "quantity": quantity, "time_slot_id": time_slot_id,
            "mode": "best_available_adjacent",
        }),
    ]
    for method, url, body in candidates:
        try:
            if method == "POST":
                status, data = await _post(session, url, bearer, body, timeout=15)
            else:
                status, data = await _get(session, url, bearer)
            if status == 200 and isinstance(data, dict) and data.get("status") == "success":
                seats = (data.get("data") or {}).get("seats") or []
                return True, [str(s) for s in seats]
        except Exception:
            continue
    return False, []


# ════════════════════════════════════════════════════════════════════════
# High-level orchestrator — single call
# ════════════════════════════════════════════════════════════════════════
async def book_ticket_http(*, bearer: str, slug: str, ticket_id: str,
                           quantity: int,
                           payment_method: str = "credit_card",
                           preferred_date: Optional[str] = None,
                           ) -> dict[str, Any]:
    """
    Complete booking: discovery → add to cart → checkout → PayTabs URL.

    Returns:
      {
        "ok": bool,
        "payment_url": "https://secure-webook.paytabs.com/...",
        "order_id": "...",
        "payment_session_id": "...",
        "seat_info": {...},       # if seated
        "logs": [...],
        "error": "..."
      }
    """
    result = {
        "ok": False, "payment_url": "", "order_id": "",
        "payment_session_id": "", "seat_info": {},
        "logs": [], "error": "",
    }
    if not bearer:
        result["error"] = "لا يوجد توكن JWT صالح (يحتاج تسجيل دخول جديد)"
        return result

    async with aiohttp.ClientSession() as session:
        # 1. event meta
        meta = await fetch_event_meta(session, slug, bearer)
        if not meta.get("event_id"):
            result["error"] = "تعذّر جلب بيانات الفعالية"
            return result
        event_id = meta["event_id"]
        result["logs"].append(f"📋 event_id={event_id[:8]} seated={meta['is_seated']}")

        # 2. resolve time_slot_id if event uses time slots
        time_slot_id = None
        dates = meta.get("time_slot_dates") or []
        if dates:
            pick = preferred_date if preferred_date in dates else dates[0]
            time_slot_id = await fetch_timeslot_id(session, slug, pick, ticket_id, bearer)
            if time_slot_id:
                result["logs"].append(f"⏰ time_slot={pick}")
            else:
                result["logs"].append(f"⚠️ no time_slot for {pick}")

        # 3. Seated flow: try to hold adjacent seats first (best-effort)
        if meta.get("is_seated") and not meta.get("booking_seats_without_map"):
            ok, seats = await hold_adjacent_seats(
                session, slug=slug, event_id=event_id,
                ticket_id=ticket_id, quantity=quantity,
                time_slot_id=time_slot_id, bearer=bearer,
            )
            if ok and seats:
                result["seat_info"] = {"seats": seats}
                result["logs"].append(f"🪑 seats: {', '.join(seats[:6])}")
            else:
                result["logs"].append(
                    "🪑 seat-hold endpoint غير متاح — سنجرب المسار العادي"
                )

        # 4. Add to cart (idempotent; clear first to be safe)
        await clear_cart(session, event_id, bearer)
        ok, cart_data = await add_to_cart(
            session,
            ticket_id=ticket_id, quantity=quantity,
            parent_event_id=event_id, time_slot_id=time_slot_id,
            bearer=bearer,
        )
        if not ok:
            msg = (cart_data.get("message") or cart_data.get("error")
                   or str(cart_data))[:200]
            result["error"] = f"فشل add-to-cart: {msg}"
            return result
        result["logs"].append(f"🛒 cart ok ({cart_data.get('item_quantity', quantity)} tickets)")

        # 4b. Scrape seat labels from the LIVE cart — webook stores the
        #     reserved seat IDs/labels under cart-items right after
        #     add-to-cart succeeds (and BEFORE checkout finalises). For
        #     seated events these are the actual numbered seats; for
        #     non-seated events the field is absent (we just skip).
        try:
            seat_labels = await fetch_cart_seat_labels(
                session, parent_event_id=event_id, bearer=bearer,
            )
            if seat_labels:
                prev = (result.get("seat_info") or {}).get("seats") or []
                merged = list(dict.fromkeys([*prev, *seat_labels]))
                result["seat_info"] = {"seats": merged}
                result["logs"].append(
                    f"🪑 seats from cart: {', '.join(merged[:6])}"
                    + (f" (+{len(merged)-6})" if len(merged) > 6 else "")
                )
        except Exception as _e:  # pragma: no cover
            log.debug(f"cart-seat scrape failed (non-fatal): {_e}")

        # 5. Create checkout = get PayTabs URL
        ok, co_data = await create_checkout(
            session, slug=slug, event_id=event_id,
            ticket_id=ticket_id, quantity=quantity,
            time_slot_id=time_slot_id, bearer=bearer,
            payment_method=payment_method,
        )
        if not ok:
            msg = (co_data.get("message") or co_data.get("error")
                   or str(co_data))[:250]
            result["error"] = f"فشل checkout: {msg}"
            return result

        pay_url = (co_data.get("redirect_url")
                   or (co_data.get("response") or {}).get("redirect_url"))
        if not pay_url:
            result["error"] = "checkout نجح لكن لم يرجع redirect_url"
            return result

        result["ok"] = True
        result["payment_url"] = pay_url
        result["order_id"] = co_data.get("order_id", "")
        result["payment_session_id"] = co_data.get("payment_session_id", "")
        result["logs"].append("💳 PayTabs URL ready")
        return result
