"""
Football-only filtering & sector classification.

Webook returns rich event metadata. For our bot we restrict the user
journey to football matches ONLY. Each match has:

  • event-detail.sport.slug == "football"
  • event-detail.home_team / away_team — {_id, name, logo, score}
  • event-ticket-details.event_ticket[*] — each ticket entry IS a sector:
        title              → "الفئة الأولى الشمالية - الواجهة"
        group_name         → "VIP" | "GA"
        seats_io_category  → "1", "2", … (numeric stadium block id)
        ticket_color       → "#c51c2f" (block fill colour)
        price / max_per_order / sale_status
        quantity           → typically None (Webook hides true seat count)

The classification convention used by Webook (and by every Saudi club
homepage we surveyed) is:

  •  "الشمالية" / "North" / "- N"  → جمهور الفريق المضيف (home_team)
  •  "الجنوبية" / "South" / "- S"  → جمهور الفريق الزائر (away_team)
  •  "الشرقية"  / "East"  / "- E"  → منصة شرقية (محايد + VIP)
  •  "الغربية"  / "West"  / "- W"  → منصة غربية (محايد + VIP)
  •  "VIP" / "منصة" / "ذهبية" / "فضية" / "Premium" / "Gold" / "Silver"
                                  → VIP / منصات (يعرض في تبويب منفصل)

For ambiguous sectors we fall back to the `group_name` field
(GA = جمهور عام, VIP = فاخر). The user sees three high-level tabs:

   👥 جمهور {home_team_name}    — sectors classified as home/north
   👥 جمهور {away_team_name}    — sectors classified as away/south
   ⭐ المنصات / VIP             — VIP, gold, silver, premium, etc.

Inside each tab, sectors are listed as buttons:

   [🟥 الفئة الأولى الشمالية — 8.7 ر.س — متاح]

After the user picks a sector, if multiple price tiers (e.g.
"الواجهة" vs "الدرجة الأولى") share the same direction we group
them and ask the user to pick the price tier inside that sector.
"""
from __future__ import annotations

import re
from typing import Any

# ════════════════════════════════════════════════════════════════════════
# Side classification keyword tables
# ════════════════════════════════════════════════════════════════════════
_HOME_RX = re.compile(
    # Arabic + English north/home indicators
    r"(الشمال|شمال[يي]?|شماليه|north|\bN\b|\b- N\b|home(?:\s*stand)?)",
    re.I,
)
_AWAY_RX = re.compile(
    r"(الجنوب|جنوب[يي]?|جنوبيه|south|\bS\b|\b- S\b|away(?:\s*stand)?)",
    re.I,
)
_EAST_RX = re.compile(
    r"(الشرق|شرق[يي]?|شرقيه|east|\bE\b|\b- E\b)",
    re.I,
)
_WEST_RX = re.compile(
    r"(الغرب|غرب[يي]?|غربيه|west|\bW\b|\b- W\b)",
    re.I,
)
_VIP_RX = re.compile(
    r"(vip|premium|gold|silver|platinum|royal|diamond|"
    r"منصة|المنصة|ذهبي|فضي|بلاتين|بلاتيني|الواي|كبار|الشخصيات|"
    r"فاخر|روي|ملوكي|ريجال)",
    re.I,
)


# ════════════════════════════════════════════════════════════════════════
# Public helpers
# ════════════════════════════════════════════════════════════════════════
def is_football_event(detail: dict | None) -> bool:
    """Return True iff the event-detail payload is a football match.

    We rely primarily on `sport.slug == "football"`. As a defensive
    fallback (Webook occasionally drops the field for legacy events) we
    also accept `home_team.name` + `away_team.name` both being non-empty
    strings.
    """
    if not isinstance(detail, dict):
        return False
    sport = detail.get("sport") or {}
    if isinstance(sport, dict) and (sport.get("slug") or "").lower() == "football":
        return True
    # Fallback heuristic
    ht = detail.get("home_team")
    at = detail.get("away_team")
    if (
        isinstance(ht, dict) and isinstance(at, dict)
        and (ht.get("name") or "").strip()
        and (at.get("name") or "").strip()
    ):
        return True
    return False


def extract_teams(detail: dict | None) -> dict[str, dict[str, str]]:
    """
    Return a normalised representation of the two competing teams.

    Output shape — keys ``team_a`` (home) and ``team_b`` (away):

        {
          "team_a": {"id":"...", "name":"الهلال", "logo":"https://...", "side":"home"},
          "team_b": {"id":"...", "name":"الفتح",  "logo":"https://...", "side":"away"},
        }

    If a team is missing (rare), the corresponding entry is an empty
    placeholder with ``name="—"`` so the UI degrades gracefully.
    """
    detail = detail or {}
    home = detail.get("home_team") if isinstance(detail.get("home_team"), dict) else {}
    away = detail.get("away_team") if isinstance(detail.get("away_team"), dict) else {}
    return {
        "team_a": {
            "id": (home or {}).get("_id") or "",
            "name": (home or {}).get("name") or "الفريق المضيف",
            "logo": (home or {}).get("logo") or "",
            "side": "home",
        },
        "team_b": {
            "id": (away or {}).get("_id") or "",
            "name": (away or {}).get("name") or "الفريق الزائر",
            "logo": (away or {}).get("logo") or "",
            "side": "away",
        },
    }


def classify_sector(ticket: dict) -> str:
    """
    Classify a single normalised ticket dict into a side bucket.

    Returns one of:
       "home"     — home/north fan stand
       "away"     — away/south fan stand
       "vip"      — VIP / premium / royal / gold / silver / منصات
       "neutral"  — east, west or unclassified general admission

    Resolution order (first match wins):
      1) Title matches a VIP keyword  → "vip"
      2) group_name == "VIP"          → "vip"
      3) Title matches North / -N     → "home"
      4) Title matches South / -S     → "away"
      5) East / West / unknown        → "neutral"
    """
    title = (ticket.get("title") or "").strip()
    group = (ticket.get("group_name") or "").strip().upper()
    blob = f"{title} {group}"

    # 1) VIP — strongest signal first (keyword in title or group)
    if _VIP_RX.search(blob):
        return "vip"
    if group in ("VIP", "PREMIUM", "GOLD", "SILVER", "PLATINUM"):
        return "vip"

    # 2) Direction-based
    if _HOME_RX.search(title):
        return "home"
    if _AWAY_RX.search(title):
        return "away"

    # 3) East / West → neutral (often shared with VIP boxes; if pure GA we
    # still classify as "neutral" so the user sees them in a fourth bucket)
    if _EAST_RX.search(title) or _WEST_RX.search(title):
        return "neutral"

    return "neutral"


def group_tickets_by_side(tickets: list[dict]) -> dict[str, list[dict]]:
    """Group a list of normalised tickets into ``home/away/vip/neutral``
    buckets using :func:`classify_sector`. The original order inside
    each bucket is preserved (Webook usually orders sectors by price)."""
    buckets: dict[str, list[dict]] = {"home": [], "away": [], "vip": [], "neutral": []}
    for t in tickets:
        # Skip tickets that aren't actually for sale
        if t.get("status") and t.get("status") != "active":
            continue
        side = classify_sector(t)
        buckets[side].append(t)
    return buckets


def cluster_price_tiers(sectors: list[dict]) -> list[dict]:
    """
    Inside one side bucket, multiple sectors often differ ONLY by their
    price-tier suffix:

        "الفئة الأولى الشمالية - الواجهة"        SAR 100
        "الفئة الأولى الشمالية - الدرجة الأولى"  SAR 50

    They share the SAME `seats_io_category` family but a different
    `price`. The UI is friendlier if we collapse such siblings into a
    parent sector with N price-tier buttons.

    Heuristic: two sectors are siblings iff their titles share the SAME
    prefix up to the last "-" / "—" separator. We return a list of
    cluster dicts:

        {
          "key":  "الفئة الأولى الشمالية",
          "title": "الفئة الأولى الشمالية",
          "color": "#c51c2f",
          "tiers": [<ticket>, <ticket>, …],   # sorted by price desc
        }
    """
    out: dict[str, dict] = {}
    order: list[str] = []
    for tk in sectors:
        title = (tk.get("title") or "").strip()
        # split on EN/AR dashes
        parts = re.split(r"\s*[-—–]\s*", title, maxsplit=1)
        prefix = parts[0].strip() if parts else title
        key = prefix.lower()
        if key not in out:
            out[key] = {
                "key": key,
                "title": prefix,
                "color": tk.get("ticket_color") or "",
                "group_name": tk.get("group_name") or "",
                "tiers": [],
            }
            order.append(key)
        out[key]["tiers"].append(tk)
    # Sort tiers by price desc (most expensive first → "الواجهة" usually)
    for c in out.values():
        c["tiers"].sort(
            key=lambda t: float(t.get("display_price") or t.get("price") or 0),
            reverse=True,
        )
    return [out[k] for k in order]


# ════════════════════════════════════════════════════════════════════════
# UI label helpers (used by keyboards.py)
# ════════════════════════════════════════════════════════════════════════
def availability_label(qty: Any) -> str:
    """
    Webook hides true seat counts (`quantity` is usually None) but
    returns a non-null integer for some events. Render appropriately.
    """
    if qty is None or qty == -1 or qty == "":
        return "متاح"
    try:
        n = int(qty)
    except (TypeError, ValueError):
        return "متاح"
    if n <= 0:
        return "نفد"
    return f"{n} مقعد"


def short_color_emoji(hex_color: str) -> str:
    """
    Map a 6-digit hex colour to the closest unicode square emoji.
    Reused by handlers.py so callbacks display a sector colour visually.
    """
    if not hex_color or not hex_color.startswith("#"):
        return "🟦"
    try:
        c = hex_color.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        return "🟦"
    palette = {
        "🟥": (220, 50, 50),  "🟧": (240, 130, 40),
        "🟨": (230, 210, 70), "🟩": (80, 180, 100),
        "🟦": (70, 110, 220), "🟪": (150, 60, 190),
        "🟫": (140, 90, 60),  "⬛": (40, 40, 40),
        "⬜": (235, 235, 235),
    }
    best, best_d = "🟦", 10 ** 9
    for e, (pr, pg, pb) in palette.items():
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best = d, e
    return best
