"""Inline keyboard builders. All callback_data strings are ≤ 64 bytes.

Long identifiers (slug, ObjectId) are stored via app.bot.tokens so the
callback_data carries only an 8-char opaque token.
"""
from __future__ import annotations

from typing import Any

from app.bot import tokens as tok


def main_menu() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🎫 الفعاليات الجارية", "callback_data": "events:0"}],
        [{"text": "🔥 قنّاص سباق الثواني", "callback_data": "sniper:menu"}],
        [{"text": "👥 إدارة الحسابات", "callback_data": "accounts:list"}],
        [{"text": "📋 حجوزاتي", "callback_data": "bookings:list"}],
        [{"text": "👁️ كلمات المراقبة", "callback_data": "watch:list"}],
        [{"text": "ℹ️ تعليمات", "callback_data": "help:show"}],
    ]}


def events_keyboard(events: list[dict], page: int = 0,
                    page_size: int = 8) -> dict[str, Any]:
    """Football-only events list. Each row shows team_a vs team_b
    if available, otherwise falls back to the event title."""
    start = page * page_size
    chunk = events[start:start + page_size]
    rows = []
    for e in chunk:
        t = tok.put({"slug": e["slug"]})
        ta = e.get("team_a_name") or (e.get("team_a") or {}).get("name") or ""
        tb = e.get("team_b_name") or (e.get("team_b") or {}).get("name") or ""
        if ta and tb:
            label = f"⚽ {_truncate(ta, 18)} ⚔️ {_truncate(tb, 18)}"
        else:
            label = f"• {_truncate(e['title'] or e['slug'], 50)}"
        rows.append([{"text": label, "callback_data": f"evt:{t}"}])
    # Pagination
    nav = []
    if page > 0:
        nav.append({"text": "◀️ السابق",
                    "callback_data": f"events:{page-1}"})
    if start + page_size < len(events):
        nav.append({"text": "التالي ▶️",
                    "callback_data": f"events:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🔄 تحديث", "callback_data": "events:refresh"}])
    rows.append([{"text": "⬅️ القائمة الرئيسية", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


# ═════════════════════════════════════════════════════════════════════
# Football-specific keyboards (interactive booking journey)
# ═════════════════════════════════════════════════════════════════════
def team_selection_keyboard(slug: str, team_a_name: str,
                             team_b_name: str,
                             buckets: dict[str, list]) -> dict[str, Any]:
    """
    Step 1 of the football booking flow.

    Shows up to four side-tabs:
        ⚽ جمهور {team_a}                (home / north stand)
        ⚽ جمهور {team_b}                (away / south stand)
        ⭐ المنصات / VIP                  (premium tickets)
        🏟️ قطاعات أخرى                   (east/west / unknown)

    *buckets* must be the dict produced by
    :func:`football_filter.group_tickets_by_side`. Empty buckets are
    suppressed.
    """
    rows = []
    home_n = len(buckets.get("home") or [])
    away_n = len(buckets.get("away") or [])
    vip_n = len(buckets.get("vip") or [])
    neu_n = len(buckets.get("neutral") or [])

    if home_n:
        t_h = tok.put({"slug": slug, "side": "home"})
        rows.append([{
            "text": f"🏠 جمهور {_truncate(team_a_name or 'المضيف', 22)}  —  {home_n} قطاع",
            "callback_data": f"side:{t_h}",
        }])
    if away_n:
        t_a = tok.put({"slug": slug, "side": "away"})
        rows.append([{
            "text": f"✌️ جمهور {_truncate(team_b_name or 'الزائر', 22)}  —  {away_n} قطاع",
            "callback_data": f"side:{t_a}",
        }])
    if vip_n:
        t_v = tok.put({"slug": slug, "side": "vip"})
        rows.append([{
            "text": f"⭐ المنصات / VIP  —  {vip_n} قطاع",
            "callback_data": f"side:{t_v}",
        }])
    if neu_n:
        t_n = tok.put({"slug": slug, "side": "neutral"})
        rows.append([{
            "text": f"🏟️ قطاعات أخرى (شرق / غرب)  —  {neu_n} قطاع",
            "callback_data": f"side:{t_n}",
        }])

    if not rows:
        rows.append([{
            "text": "⚠️ لا توجد قطاعات متاحة للحجز حالياً",
            "callback_data": "noop",
        }])

    rows.append([{"text": "⬅️ رجوع للفعاليات",
                  "callback_data": "events:0"}])
    rows.append([{"text": "🏠 القائمة", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def sector_selection_keyboard(slug: str, side: str,
                              clusters: list[dict]) -> dict[str, Any]:
    """
    Step 2 — list sectors inside the chosen side.

    Each *cluster* is a parent sector (one or more price tiers). When
    a cluster has multiple tiers we route to a price-tier picker; when
    it has a single tier we go straight to the quantity prompt.

    Each row label looks like::

        🟥 الفئة الأولى الشمالية  •  100 ر.س  •  متاح (أو 50 مقعد)
    """
    from app.services.football_filter import (
        availability_label, short_color_emoji,
    )
    rows = []
    if not clusters:
        rows.append([{"text": "⚠️ لا توجد قطاعات في هذا الجانب",
                       "callback_data": "noop"}])
    for cl in clusters:
        emoji = short_color_emoji(cl.get("color") or "")
        title = _truncate(cl.get("title") or "قطاع", 22)
        tiers = cl.get("tiers") or []
        if len(tiers) == 1:
            t = tiers[0]
            price = t.get("display_price") or 0
            ccy = _ccy(t.get("currency") or "SAR")
            avail = availability_label(t.get("quantity"))
            ptok = tok.put({
                "slug": slug, "side": side, "ticket_id": t["id"],
            })
            label = f"{emoji} {title} • {_fmt_price(price)} {ccy} • {avail}"
            rows.append([{"text": label, "callback_data": f"sec:{ptok}"}])
        else:
            # multiple tiers — route to tier-picker, carry cluster key
            tids = ",".join(t["id"] for t in tiers)
            ctok = tok.put({
                "slug": slug, "side": side,
                "cluster_title": cl.get("title"),
                "ticket_ids": tids,
            })
            min_price = min(
                float(t.get("display_price") or t.get("price") or 0)
                for t in tiers
            )
            ccy = _ccy(tiers[0].get("currency") or "SAR")
            label = (f"{emoji} {title} • من {_fmt_price(min_price)} {ccy} • "
                     f"{len(tiers)} فئات")
            rows.append([{"text": label, "callback_data": f"clu:{ctok}"}])

    rows.append([{"text": "⬅️ تغيير الفريق",
                  "callback_data": f"evt:{tok.put({'slug': slug})}"}])
    rows.append([{"text": "🏠 القائمة", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def price_tier_keyboard(slug: str, side: str, ticket_ids: list[str],
                         tickets: list[dict]) -> dict[str, Any]:
    """Step 2b — within a multi-tier cluster, choose a single ticket."""
    from app.services.football_filter import (
        availability_label, short_color_emoji,
    )
    rows = []
    by_id = {t["id"]: t for t in tickets}
    for tid in ticket_ids:
        t = by_id.get(tid)
        if not t:
            continue
        price = t.get("display_price") or 0
        ccy = _ccy(t.get("currency") or "SAR")
        emoji = short_color_emoji(t.get("ticket_color") or "")
        # Show the "tier suffix" (text after the dash)
        full = t.get("title") or ""
        import re as _re
        parts = _re.split(r"\s*[-—–]\s*", full, maxsplit=1)
        suffix = parts[1].strip() if len(parts) > 1 else full
        label = (f"{emoji} {_truncate(suffix, 22)} • "
                 f"{_fmt_price(price)} {ccy} • "
                 f"{availability_label(t.get('quantity'))}")
        ptok = tok.put({
            "slug": slug, "side": side, "ticket_id": tid,
        })
        rows.append([{"text": label, "callback_data": f"sec:{ptok}"}])

    rows.append([{"text": "⬅️ رجوع",
                  "callback_data": f"side:{tok.put({'slug': slug, 'side': side})}"}])
    return {"inline_keyboard": rows}


def cart_review_keyboard(context_token: str) -> dict[str, Any]:
    """Step 4 — final review screen before booking."""
    return {"inline_keyboard": [
        [{"text": "✅ تأكيد الحجز",
          "callback_data": f"go:{context_token}"}],
        [{"text": "✏️ تعديل العدد",
          "callback_data": f"qty:{context_token}"}],
        [{"text": "❌ إلغاء", "callback_data": "cart:cancel"}],
    ]}


def ticket_types_keyboard(event_slug: str,
                          tickets: list[dict]) -> dict[str, Any]:
    rows = []
    any_active = False
    for t in tickets:
        if t.get("status") != "active":
            continue
        any_active = True
        status = t.get("sale_status") or ""
        badge = ""
        if status == "ongoing":
            badge = " ✅"
        elif status == "not_yet":
            badge = " ⏳"
        elif status == "ended":
            badge = " ⛔"
        price = t.get("display_price") or 0
        ccy = _ccy(t.get("currency") or "SAR")
        price_lbl = f"{_fmt_price(price)} {ccy}" if price else "—"
        # ticket_id + slug are too long for callback_data, use token store
        callback_tok = tok.put({"slug": event_slug, "ticket_id": t["id"]})
        label = f"{_truncate(t['title'], 30)} — {price_lbl}{badge}"
        rows.append([{"text": label, "callback_data": f"tck:{callback_tok}"}])

    if not any_active:
        rows.append([{"text": "⚠️ لا توجد تذاكر متاحة",
                      "callback_data": "menu"}])

    # Back to the events list
    rows.append([{"text": "⬅️ رجوع للفعاليات",
                  "callback_data": "events:0"}])
    return {"inline_keyboard": rows}


def confirm_plan_keyboard(context_token: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "✅ تأكيد وبدء الحجز",
          "callback_data": f"go:{context_token}"}],
        [{"text": "❌ إلغاء", "callback_data": "menu"}],
    ]}


def accounts_keyboard(accounts: list[dict]) -> dict[str, Any]:
    rows = []
    for a in accounts:
        icon = {
            "ready": "✅", "refreshing": "🔄", "new": "🆕",
            "needs_relogin": "⚠️", "blocked": "🚫",
        }.get(a.get("status", ""), "❓")
        email = a.get("email", "—")
        label = a.get("label") or email.split("@")[0]
        rows.append([{"text": f"{icon} {label} — {_truncate(email, 25)}",
                      "callback_data": f"acc:{a['id']}"}])
    rows.append([{"text": "➕ إضافة حساب جديد", "callback_data": "acc:add"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def account_actions(account_id: str, status: str) -> dict[str, Any]:
    rows = []
    if status in ("new", "needs_relogin", "blocked"):
        rows.append([{"text": "🔐 تسجيل الدخول الآن",
                      "callback_data": f"acc:login:{account_id}"}])
    else:
        rows.append([{"text": "🔄 إعادة تسجيل الدخول",
                      "callback_data": f"acc:login:{account_id}"}])
    rows.append([{"text": "🗑️ حذف الحساب",
                  "callback_data": f"acc:del:{account_id}"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "accounts:list"}])
    return {"inline_keyboard": rows}


def watch_keyboard(keywords: list[str]) -> dict[str, Any]:
    rows = [[{"text": f"🗑️ {k[:40]}",
              "callback_data": f"watch:del:{k[:40]}"}]
            for k in keywords[:20]]
    rows.append([{"text": "➕ إضافة كلمة", "callback_data": "watch:add"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def back_to_menu() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "⬅️ القائمة الرئيسية", "callback_data": "menu"}]
    ]}


def back_to_event(event_token: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "⬅️ رجوع", "callback_data": f"evt:{event_token}"}],
        [{"text": "🏠 القائمة", "callback_data": "menu"}],
    ]}


def seat_map_keyboard(event_slug: str,
                     squares: list[dict]) -> dict:
    """Render the stadium/forum map as a grid of colored squares.

    ``squares`` — list of dicts with keys:
        id, title, price, currency, available, color, group_name

    The grid tries to mimic the real stadium layout:
    - Squares with -N (North), -S (South), -E (East), -W (West) hints
      in their title are placed accordingly.
    - If no direction hints exist, squares are laid in rows of 2.
    - A color emoji is picked closest to ticket's brand color, so the
      buttons visually mimic the online seat-map.
    """
    north, south, east, west, center = [], [], [], [], []
    for sq in squares:
        t = (sq.get("title") or "").upper()
        title_ar = sq.get("title") or ""
        if "- N" in t or "NORTH" in t or "شمال" in title_ar:
            north.append(sq)
        elif "- S" in t or "SOUTH" in t or "جنوب" in title_ar:
            south.append(sq)
        elif "- E" in t or "EAST" in t or "شرق" in title_ar:
            east.append(sq)
        elif "- W" in t or "WEST" in t or "غرب" in title_ar:
            west.append(sq)
        else:
            center.append(sq)

    def _btn(sq: dict) -> dict:
        avail = sq.get("available")
        if avail is None or avail == -1:
            avail_txt = "متاح"
        elif avail == 0:
            avail_txt = "نفد"
        else:
            avail_txt = f"{avail}"
        color = _color_emoji(sq.get("color", ""))
        price = _fmt_price(sq.get("price") or 0)
        ccy = _ccy(sq.get("currency") or "SAR")
        lbl = f"{color} {_truncate(sq['title'], 16)} • {avail_txt} • {price}{ccy}"
        tok_obj = tok.put({"slug": event_slug, "ticket_id": sq["id"]})
        return {"text": lbl, "callback_data": f"tck:{tok_obj}"}

    rows = []
    rows.append([{"text": "🏟️ اختر المربع (الربع) من الخريطة:",
                  "callback_data": "noop"}])
    if north:
        rows.append([{"text": "⬆️  الشمال  ⬆️", "callback_data": "noop"}])
        _grid_append(rows, north, width=2, btn=_btn)
    if east and west:
        rows.append([{"text": "◀️  شرق / غرب  ▶️", "callback_data": "noop"}])
        max_n = max(len(east), len(west))
        for i in range(max_n):
            r = []
            if i < len(west):
                r.append(_btn(west[i]))
            if i < len(east):
                r.append(_btn(east[i]))
            rows.append(r)
    elif east:
        rows.append([{"text": "▶️  الشرق  ▶️", "callback_data": "noop"}])
        _grid_append(rows, east, width=2, btn=_btn)
    elif west:
        rows.append([{"text": "◀️  الغرب  ◀️", "callback_data": "noop"}])
        _grid_append(rows, west, width=2, btn=_btn)
    if center:
        if north or south or east or west:
            rows.append([{"text": "⚬  أخرى  ⚬", "callback_data": "noop"}])
        _grid_append(rows, center, width=2, btn=_btn)
    if south:
        rows.append([{"text": "⬇️  الجنوب  ⬇️", "callback_data": "noop"}])
        _grid_append(rows, south, width=2, btn=_btn)

    rows.append([{"text": "⬅️ رجوع للفعاليات", "callback_data": "events:0"}])
    return {"inline_keyboard": rows}


def payment_method_keyboard(context_token: str) -> dict:
    """Let the user pick Credit Card or Mada before booking."""
    return {"inline_keyboard": [
        [{"text": "💳 بطاقة ائتمان (Visa / Mastercard)",
          "callback_data": f"pay:{context_token}:credit_card"}],
        [{"text": "🇸🇦 مدى (Mada)",
          "callback_data": f"pay:{context_token}:mada"}],
        [{"text": "❌ إلغاء", "callback_data": "menu"}],
    ]}


def _grid_append(rows: list, items: list[dict], width: int, btn) -> None:
    for i in range(0, len(items), width):
        rows.append([btn(sq) for sq in items[i:i + width]])


def _color_emoji(hex_color: str) -> str:
    """Pick a color emoji closest to the ticket's brand color."""
    if not hex_color or not hex_color.startswith("#"):
        return "🟦"
    try:
        c = hex_color.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        return "🟦"
    palette = {
        "🟥": (220, 50, 50), "🟧": (240, 130, 40),
        "🟨": (230, 210, 70), "🟩": (80, 180, 100),
        "🟦": (70, 110, 220), "🟪": (150, 60, 190),
        "🟫": (140, 90, 60), "⬛": (40, 40, 40), "⬜": (235, 235, 235),
    }
    best = "🟦"
    best_d = 10 ** 9
    for e, (pr, pg, pb) in palette.items():
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best = e
    return best


# ── helpers ─────────────────────────────────────────────────────────
def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ccy(code: str) -> str:
    return {
        "SAR": "ر.س", "AED": "د.إ", "USD": "$", "EUR": "€",
        "KWD": "د.ك", "QAR": "ر.ق",
    }.get((code or "").upper(), code or "")


def _fmt_price(p: float) -> str:
    p = float(p or 0)
    if p == int(p):
        return str(int(p))
    return f"{p:.2f}"
