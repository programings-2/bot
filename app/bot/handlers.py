"""Telegram update dispatcher — 100% button-driven, all Arabic UI."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.bot import keyboards as kb
from app.bot import state as fsm
from app.bot import tokens as tok
from app.bot.notifier import Notifier
from app.core.config import DEFAULT_WATCH_KEYWORDS, authorized_chat_ids
from app.core.storage import (
    add_keyword, delete_account, get_account, list_accounts, list_bookings,
    list_keywords, list_recent_events, remove_keyword, upsert_account,
    upsert_event,
)
from app.services import auth_service
from app.services.booking_orchestrator import book_all
from app.services.distributor import describe_plan, distribute
from app.services.event_discovery import enrich_all, fetch_event_slugs
from app.services.webook_api import (
    get_event_detail, get_event_tickets, fetch_squares_summary,
)

log = logging.getLogger("handlers")

WELCOME = (
    "👋 <b>أهلاً بك في بوت حجز التذاكر</b>\n\n"
    "يعرض لك أحدث فعاليات webook.com ويحجز على حساباتك بالتوازي، ثم "
    "يرجع لك روابط الدفع جاهزة.\n\n"
    "اختر من القائمة:"
)

HELP = (
    "🆘 <b>طريقة الاستخدام</b>\n\n"
    "الخطوات:\n"
    "1️⃣ من <b>إدارة الحسابات</b> أضف حساباً أو أكثر\n"
    "2️⃣ اضغط <b>تسجيل الدخول</b> لكل حساب (مرة واحدة فقط)\n"
    "3️⃣ من <b>الفعاليات الجارية</b> اختر فعالية\n"
    "4️⃣ اختر نوع التذكرة\n"
    "5️⃣ أرسل عدد التذاكر كرسالة نصية\n"
    "6️⃣ اضغط تأكيد — سيأتيك رابط الدفع لكل حساب\n\n"
    "💡 <i>التوكن صالح ~٧ أيام ويُجدَّد تلقائياً.</i>"
)


# ════════════════════════════════════════════════════════════════════════
# Entry
# ════════════════════════════════════════════════════════════════════════
async def dispatch(update: dict, notifier: Notifier) -> None:
    try:
        if "callback_query" in update:
            await _on_callback(update["callback_query"], notifier)
        elif "message" in update:
            await _on_message(update["message"], notifier)
    except Exception as e:
        log.exception(f"dispatch err: {e}")


def _authorized(chat_id: str) -> bool:
    ids = authorized_chat_ids()
    return not ids or str(chat_id) in ids


# ════════════════════════════════════════════════════════════════════════
# Messages
# ════════════════════════════════════════════════════════════════════════
async def _on_message(msg: dict, notifier: Notifier) -> None:
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not _authorized(chat_id):
        await notifier.send(chat_id, "🚫 غير مصرّح لك باستخدام هذا البوت.")
        return

    st = fsm.get_state(chat_id)
    if st:
        if st.name == "waiting_email":
            if "@" not in text or "." not in text:
                await notifier.send(chat_id, "⚠️ يرجى إرسال بريد صالح.")
                return
            fsm.set_state(chat_id, "waiting_password", email=text)
            await notifier.send(
                chat_id,
                "✅ تم استلام البريد.\nأرسل الآن <b>كلمة المرور</b>:")
            return

        if st.name == "waiting_password":
            email = st.data.get("email", "")
            account_id = "acc_" + uuid.uuid4().hex[:8]
            upsert_account(account_id, email=email, password=text,
                           label=email.split("@")[0])
            fsm.clear_state(chat_id)
            await notifier.send(
                chat_id,
                f"✅ تمت إضافة الحساب (<code>{email}</code>).\n\n"
                f"اضغط على الحساب ثم <b>🔐 تسجيل الدخول</b> لتفعيله.",
                reply_markup=kb.accounts_keyboard(list_accounts()),
            )
            return

        if st.name == "waiting_qty":
            ctx = st.data  # {slug, ticket_id, ticket_token}
            try:
                n = int(text.strip())
                if n <= 0:
                    raise ValueError
            except ValueError:
                await notifier.send(
                    chat_id, "⚠️ أرسل عدداً صحيحاً موجباً فقط.")
                return
            fsm.clear_state(chat_id)
            await _show_plan(chat_id, ctx["slug"], ctx["ticket_id"],
                             n, notifier)
            return

        if st.name == "waiting_keyword":
            add_keyword(text, added_by=chat_id)
            fsm.clear_state(chat_id)
            await notifier.send(
                chat_id,
                f"✅ تمت إضافة الكلمة: <code>{text}</code>",
                reply_markup=kb.watch_keyboard(list_keywords()),
            )
            return

    # /start or any other text → open main menu
    await notifier.send(chat_id, WELCOME, reply_markup=kb.main_menu())


# ════════════════════════════════════════════════════════════════════════
# Callbacks
# ════════════════════════════════════════════════════════════════════════
async def _on_callback(cq: dict, notifier: Notifier) -> None:
    chat_id = str(cq["message"]["chat"]["id"])
    msg_id = cq["message"]["message_id"]
    data = cq.get("data", "")

    if not _authorized(chat_id):
        await notifier.answer_cb(cq["id"], "🚫 غير مصرّح", show_alert=True)
        return

    await notifier.answer_cb(cq["id"])

    try:
        await _route(chat_id, msg_id, data, notifier)
    except Exception as e:
        log.exception(f"callback err: {e}")
        try:
            await notifier.send(chat_id, f"⚠️ خطأ: <code>{e}</code>",
                                reply_markup=kb.back_to_menu())
        except Exception:
            pass


async def _route(chat_id: str, msg_id: int, data: str,
                 notifier: Notifier) -> None:
    if data == "menu":
        await notifier.edit(chat_id, msg_id, WELCOME,
                            reply_markup=kb.main_menu()); return
    if data == "noop":
        # Non-interactive labels (seat-map legend rows) — silently ignore.
        return
    if data == "help:show":
        await notifier.edit(chat_id, msg_id, HELP,
                            reply_markup=kb.back_to_menu()); return

    # Events
    if data.startswith("events:"):
        arg = data.split(":", 1)[1]
        await _show_events(chat_id, msg_id, arg, notifier); return
    if data.startswith("evt:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id,
                                "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu())
            return
        await _show_event(chat_id, entry["slug"], notifier,
                          edit_msg_id=msg_id, event_token=t)
        return
    if data.startswith("tck:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id,
                                "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        await _ask_quantity(chat_id, entry["slug"], entry["ticket_id"],
                            msg_id, notifier)
        return
    if data.startswith("go:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id,
                                "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        # After plan confirmation, ASK for payment method first.
        await _ask_payment_method(chat_id, msg_id, t, notifier)
        return
    if data.startswith("pay:"):
        # format: pay:<ctx_token>:<credit_card|mada>
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        ctx_t, method = parts[1], parts[2]
        entry = tok.get(ctx_t)
        if not entry:
            await notifier.edit(chat_id, msg_id,
                                "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        await _execute_booking(
            chat_id, msg_id,
            entry["slug"], entry["ticket_id"], entry["qty"],
            notifier, payment_method=method,
        )
        return

    # Accounts
    if data == "accounts:list":
        await notifier.edit(
            chat_id, msg_id, "👥 <b>حساباتك</b>",
            reply_markup=kb.accounts_keyboard(list_accounts())); return
    if data == "acc:add":
        fsm.set_state(chat_id, "waiting_email")
        await notifier.send(chat_id,
                            "📧 أرسل <b>البريد الإلكتروني</b> لحساب webook:")
        return
    if data.startswith("acc:login:"):
        acc_id = data.split(":", 2)[2]
        await _login_flow(chat_id, msg_id, acc_id, notifier); return
    if data.startswith("acc:del:"):
        acc_id = data.split(":", 2)[2]
        delete_account(acc_id)
        await notifier.edit(chat_id, msg_id, "🗑️ تم حذف الحساب.",
                            reply_markup=kb.accounts_keyboard(list_accounts()))
        return
    if data.startswith("acc:"):
        acc_id = data.split(":", 1)[1]
        await _show_account(chat_id, msg_id, acc_id, notifier); return

    # Bookings
    if data == "bookings:list":
        await _show_bookings(chat_id, notifier, edit_msg_id=msg_id); return

    # Watch keywords
    if data == "watch:list":
        kws = list_keywords()
        if not kws:
            for k in DEFAULT_WATCH_KEYWORDS:
                add_keyword(k, "system")
            kws = list_keywords()
        await notifier.edit(
            chat_id, msg_id,
            "👁️ <b>كلمات المراقبة</b>\n\n"
            "سيصلك تنبيه عند ظهور فعالية جديدة تحتوي أياً منها.",
            reply_markup=kb.watch_keyboard(kws)); return
    if data == "watch:add":
        fsm.set_state(chat_id, "waiting_keyword")
        await notifier.send(chat_id, "➕ أرسل الكلمة:"); return
    if data.startswith("watch:del:"):
        kw = data.split(":", 2)[2]
        remove_keyword(kw)
        await notifier.edit(chat_id, msg_id, "👁️ <b>كلمات المراقبة</b>",
                            reply_markup=kb.watch_keyboard(list_keywords()))
        return

    if data == "sniper:menu":
        await notifier.edit(
            chat_id, msg_id,
            "🔥 <b>قنّاص سباق الثواني</b>\n\n"
            "يراقب فعالية محدّدة كل ثانيتين ويحجز فور افتتاح البيع.\n\n"
            "افتح <b>🎫 الفعاليات الجارية</b> → اختر فعالية تنتظر افتتاحها "
            "→ سيظهر خيار تفعيل القنّاص.",
            reply_markup=kb.back_to_menu()); return


# ════════════════════════════════════════════════════════════════════════
# Screens
# ════════════════════════════════════════════════════════════════════════
async def _show_events(chat_id: str, msg_id: int, arg: str,
                       notifier: Notifier) -> None:
    if arg == "refresh":
        await notifier.edit(chat_id, msg_id, "🔄 جارٍ تحديث الفعاليات...",
                            reply_markup=None)
        slugs = await fetch_event_slugs(max_events=200)
        events = await enrich_all(slugs, concurrency=6)
        for e in events:
            upsert_event(e["slug"], e)
        page = 0
    else:
        try:
            page = int(arg)
        except ValueError:
            page = 0
        events = list_recent_events(limit=200)
        if not events:
            await notifier.edit(chat_id, msg_id,
                                "🔄 أول تحميل — جارٍ جلب الفعاليات...",
                                reply_markup=None)
            slugs = await fetch_event_slugs(max_events=200)
            events = await enrich_all(slugs, concurrency=6)
            for e in events:
                upsert_event(e["slug"], e)

    if not events:
        await notifier.edit(chat_id, msg_id,
                            "⚠️ لا توجد فعاليات متاحة الآن.",
                            reply_markup=kb.back_to_menu())
        return

    await notifier.edit(
        chat_id, msg_id,
        f"🎫 <b>الفعاليات المتاحة</b> ({len(events)})\n\n"
        f"اضغط فعالية لعرض تذاكرها:",
        reply_markup=kb.events_keyboard(events, page=page),
    )


async def _show_event(chat_id: str, slug: str, notifier: Notifier,
                      edit_msg_id: int | None = None,
                      event_token: str | None = None) -> None:
    # Fetch BOTH detail (authoritative title) + tickets in parallel
    detail_task = asyncio.create_task(get_event_detail(slug))
    tix_task = asyncio.create_task(get_event_tickets(slug))
    detail = await detail_task
    data = await tix_task

    if not detail and not data:
        t = "⚠️ تعذّر جلب بيانات الفعالية."
        if edit_msg_id:
            await notifier.edit(chat_id, edit_msg_id, t,
                                reply_markup=kb.back_to_menu())
        else:
            await notifier.send(chat_id, t, reply_markup=kb.back_to_menu())
        return

    title = (detail or {}).get("title") or (data or {}).get("event", {}).get("title") or slug
    sub = (detail or {}).get("sub_title") or ""
    desc_raw = (detail or {}).get("description") or ""
    import re
    desc = re.sub(r"<[^>]+>", " ", desc_raw)
    desc = re.sub(r"\s+", " ", desc).strip()[:300]

    tickets = (data or {}).get("tickets") or []
    active = [t for t in tickets if t.get("status") == "active"]
    is_seated = (bool((data or {}).get("is_seated"))
                 or bool((detail or {}).get("is_seated")))

    txt = f"🎭 <b>{title}</b>\n"
    if sub:
        txt += f"{sub}\n"
    if desc:
        txt += f"\n{desc}\n"

    if active and is_seated:
        # SEATED event: render colored-squares "stadium map" interface
        summary = await fetch_squares_summary(slug)
        squares = [s for s in (summary.get("squares") or [])
                   if s.get("sale_status") in ("ongoing", None)]
        if summary.get("has_seat_name"):
            txt += ("\n🏟️ <i>الفعالية تحتوي خريطة مقاعد تفاصيلية.</i>\n"
                    "سيتم اختيار المقاعد المتجاورة تلقائياً بعد تحديد المربع.\n")
        if squares:
            avail_sum = sum(
                (sq["available"] if isinstance(sq.get("available"), int)
                 and sq["available"] >= 0 else 0)
                for sq in squares
            )
            txt += (f"\n🏟️ <b>مربعات الملعب/المنتدى:</b> "
                    f"{len(squares)} مربع\n")
            if avail_sum > 0:
                txt += (f"🪑 إجمالي المقاعد المتاحة (تقريبي): "
                        f"<b>{avail_sum}</b>\n")
            txt += ("\n💡 <i>كل مربع = قسم من الملعب. "
                    "انقر على مربع للاختيار — البوت سيحجز مقاعد متجاورة "
                    "داخل نفس المربع لكل حساب.</i>\n")
            rkb = kb.seat_map_keyboard(slug, squares)
        else:
            txt += "\n⚠️ <i>لا توجد مربعات متاحة للحجز حالياً.</i>"
            rkb = kb.back_to_menu()
    elif active:
        txt += f"\n🎟️ أنواع التذاكر المتاحة: <b>{len(active)}</b>\n\n"
        txt += "اختر نوع التذكرة:"
        rkb = kb.ticket_types_keyboard(slug, tickets)
    else:
        # Maybe it's a subscription event or sale not yet open
        txt += "\n⚠️ <i>لا توجد تذاكر متاحة حالياً عبر API.</i>\n"
        txt += ("قد تكون الفعالية تتطلب اشتراكاً، أو لم يُفتح بيعها بعد، "
                "أو تُباع بطريقة مختلفة (مثل seats.io). "
                "افتحها في المتصفح للتأكد:")
        rkb = {"inline_keyboard": [
            [{"text": "🌐 فتح الفعالية في المتصفح",
              "url": f"https://webook.com/ar/events/{slug}"}],
            [{"text": "⬅️ رجوع للفعاليات", "callback_data": "events:0"}],
            [{"text": "🏠 القائمة", "callback_data": "menu"}],
        ]}

    if edit_msg_id:
        await notifier.edit(chat_id, edit_msg_id, txt, reply_markup=rkb)
    else:
        await notifier.send(chat_id, txt, reply_markup=rkb)


async def _ask_quantity(chat_id: str, slug: str, ticket_id: str,
                        msg_id: int, notifier: Notifier) -> None:
    """Step 3 of booking — show selected sector + ask for TOTAL quantity."""
    payload = await get_football_event_detail(slug)
    if not payload:
        await notifier.edit(chat_id, msg_id, "⚠️ فعالية غير مدعومة.",
                            reply_markup=kb.back_to_menu())
        return
    ticket = next((t for t in payload["sectors"] if t["id"] == ticket_id), None)
    if not ticket:
        await notifier.edit(chat_id, msg_id, "⚠️ لم أجد نوع التذكرة.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    price = ticket.get("display_price") or 0
    ccy = kb._ccy(ticket.get("currency") or "SAR")
    price_str = f"{kb._fmt_price(price)} {ccy}" if price else "يظهر عند الحجز"
    max_per_acc = ticket["max_per_order"]
    min_q = ticket.get("min_per_order", 1)
    max_total = max_per_acc * max(len(accounts), 1)

    if len(accounts) == 0:
        txt = (
            f"🎟️ <b>{ticket['title']}</b>\n"
            f"💰 السعر: <b>{price_str}</b>\n\n"
            f"⚠️ لا يوجد لديك حسابات مُفعّلة بعد.\n"
            f"أضف حساباً من <b>إدارة الحسابات</b> أولاً."
        )
        await notifier.edit(chat_id, msg_id, txt,
                            reply_markup=kb.back_to_menu())
        return

    fsm.set_state(chat_id, "waiting_qty", slug=slug, ticket_id=ticket_id)

    # Cart context (chosen team-side label) for the prompt header
    c = cart.get(chat_id)
    side_lbl = (c.chosen_team_label if c else "") or "—"

    txt = (
        f"⚽ <b>{payload['team_a']['name']} ⚔️ {payload['team_b']['name']}</b>\n"
        f"👥 الجمهور: <b>{side_lbl}</b>\n"
        f"🎟️ القطاع: <b>{ticket['title']}</b>\n"
        f"💰 السعر: <b>{price_str}</b>\n"
        f"👤 حسابات جاهزة: <b>{len(accounts)}</b>\n"
        f"📊 الحد لكل حساب: <b>{max_per_acc}</b> • أقصى إجمالي: <b>{max_total}</b>\n"
        f"🔢 الحد الأدنى: <b>{min_q}</b>\n\n"
        f"✏️ <b>أدخل عدد التذاكر الإجمالي</b> (رقم فقط):\n\n"
        f"💡 <i>سأوزعها تلقائياً على أقل عدد ممكن من الحسابات.</i>"
    )
    await notifier.edit(chat_id, msg_id, txt,
                        reply_markup=kb.back_to_menu())


async def _show_plan(chat_id: str, slug: str, ticket_id: str, qty: int,
                     notifier: Notifier) -> None:
    """Step 4 — RICH cart-review screen (no booking yet!).

    Shows: match • chosen team-side • sector • quantity • distribution
    plan • total amount. User must press ✅ تأكيد to commit.
    """
    payload = await get_football_event_detail(slug)
    if not payload:
        await notifier.send(chat_id, "⚠️ فعالية غير مدعومة.",
                            reply_markup=kb.back_to_menu())
        return
    ticket = next((t for t in payload["sectors"] if t["id"] == ticket_id), None)
    if not ticket:
        await notifier.send(chat_id, "⚠️ نوع التذكرة غير موجود.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    try:
        plan, meta = distribute(qty, accounts=accounts,
                                max_per_order=ticket["max_per_order"],
                                min_per_order=ticket["min_per_order"])
    except ValueError as e:
        txt = (f"⚠️ <b>لا يمكن توزيع {qty} تذاكر</b>\n\n"
               f"السبب: <code>{e}</code>\n\n"
               f"الحلول:\n"
               f"• قلّل العدد\n"
               f"• أضف حسابات جديدة")
        await notifier.send(chat_id, txt, reply_markup=kb.back_to_menu())
        return

    price = float(ticket.get("display_price") or ticket.get("price") or 0)
    actual_total = meta["actual_total"]
    total_amount = price * actual_total
    ccy = kb._ccy(ticket.get("currency") or "SAR")

    # Persist quantity to the cart (review step is idempotent)
    cart.update(chat_id, quantity=actual_total)
    c = cart.get(chat_id)

    # Token carries the FINAL booking context (slug + ticket + qty after clamp)
    context_tok = tok.put({
        "slug": slug, "ticket_id": ticket_id,
        "qty": actual_total,
    })

    side_lbl = (c.chosen_team_label if c else
                {"home": f"جمهور {payload['team_a']['name']}",
                 "away": f"جمهور {payload['team_b']['name']}",
                 "vip":  "المنصات / VIP",
                 "neutral": "قطاع محايد"}.get(
                    classify_sector(ticket), "جمهور"))

    txt = (
        f"🛒 <b>مراجعة السلة</b>\n\n"
        f"⚽ <b>المباراة:</b> {payload['team_a']['name']} ⚔️ "
        f"{payload['team_b']['name']}\n"
        f"🏟️ <b>الملعب:</b> {payload.get('venue_name') or '—'}\n"
        f"👥 <b>الجمهور:</b> {side_lbl}\n"
        f"🎟️ <b>القطاع:</b> {ticket['title']}\n"
        f"💰 <b>سعر التذكرة:</b> {kb._fmt_price(price)} {ccy}\n"
        f"🧮 <b>طلبك:</b> {qty} تذكرة\n"
        f"✅ <b>سنحجز فعلياً:</b> {actual_total} تذكرة\n"
        f"🎯 <b>حسابات مستخدمة:</b> {meta['accounts_used']} / "
        f"{meta['accounts_available']}\n"
        f"💳 <b>المجموع التقريبي:</b> {kb._fmt_price(total_amount)} {ccy}\n\n"
        f"{describe_plan(plan, accounts, meta)}\n\n"
        f"🪑 <i>أرقام المقاعد ستظهر مع روابط الدفع بعد التأكيد.</i>\n\n"
        f"هل ترغب في تأكيد الحجز؟"
    )
    await notifier.send(chat_id, txt,
                        reply_markup=kb.cart_review_keyboard(context_tok))


async def _ask_payment_method(chat_id: str, msg_id: int,
                               context_token: str,
                               notifier: Notifier) -> None:
    """After the user confirms the distribution plan, ask them to pick
    a payment method (credit card or Mada)."""
    txt = (
        "💳 <b>اختر طريقة الدفع</b>\n\n"
        "ستُطبَّق على جميع الحسابات. صفحة PayTabs آمنة ستُفتح لكل "
        "حساب بعد الحجز الناجح.\n\n"
        "🔒 <i>الدفع يتم مباشرة عبر Webook + PayTabs.</i>"
    )
    await notifier.edit(chat_id, msg_id, txt,
                        reply_markup=kb.payment_method_keyboard(context_token))


async def _execute_booking(chat_id: str, msg_id: int,
                           slug: str, ticket_id: str, qty: int,
                           notifier: Notifier,
                           payment_method: str = "credit_card") -> None:
    pay_label = "مدى 🇸🇦" if payment_method == "mada" else "بطاقة ائتمان 💳"
    await notifier.edit(
        chat_id, msg_id,
        f"⚡ <b>جارٍ الحجز…</b>\n"
        f"💳 طريقة الدفع: {pay_label}\n\n🔄 التحضير…",
        reply_markup=None,
    )

    data = await get_event_tickets(slug)
    detail = await get_event_detail(slug)
    ticket = next(
        (t for t in (data.get("tickets") or []) if t["id"] == ticket_id),
        None,
    )
    if not ticket:
        await notifier.edit(chat_id, msg_id, "⚠️ نوع التذكرة غير موجود.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    try:
        plan, meta = distribute(qty, accounts=accounts,
                                max_per_order=ticket["max_per_order"],
                                min_per_order=ticket["min_per_order"])
    except ValueError as e:
        await notifier.edit(chat_id, msg_id,
                            f"⚠️ تعذّر التوزيع: <code>{e}</code>",
                            reply_markup=kb.back_to_menu())
        return

    progress_lines: list[str] = []

    async def _progress(line: str):
        progress_lines.append(line)
        tail = "\n".join(progress_lines[-12:])
        try:
            await notifier.edit(chat_id, msg_id,
                                f"⚡ <b>جارٍ الحجز...</b>\n\n{tail}")
        except Exception:
            pass

    title = (detail or {}).get("title") or slug

    results = await book_all(
        plan,
        event_slug=slug,
        event_title=title,
        ticket_id=ticket_id,
        ticket_title=ticket["title"],
        ticket_price=ticket.get("display_price") or 0,
        currency=ticket["currency"],
        chat_id=chat_id, notifier=notifier,
        progress=_progress,
        payment_method=payment_method,
    )

    succ = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]

    lines = [
        "🎉 <b>انتهى الحجز</b>",
        f"🎭 {title}",
        f"🎫 {ticket['title']}",
        "",
        f"✅ نجاح: <b>{len(succ)}</b>   ❌ فشل: <b>{len(fail)}</b>",
        "",
    ]
    for r in succ:
        seat = r.get("seat_info") or {}
        # Webook returns one of two shapes:
        #   1) {"seats": ["A12", "A13"]}        — from cart-items API
        #   2) {"section":"...","row":"...","seat_number":"..."}  — single seat
        seat_line = ""
        seats_arr = seat.get("seats") if isinstance(seat, dict) else None
        if seats_arr and isinstance(seats_arr, list):
            shown = ", ".join(str(s) for s in seats_arr[:8])
            if len(seats_arr) > 8:
                shown += f" … +{len(seats_arr)-8}"
            seat_line = f"   🪑 المقاعد: <b>{shown}</b>\n"
        elif seat.get("section") or seat.get("seat_number"):
            seat_line = (
                f"   🪑 القسم: {seat.get('section', '—')} · "
                f"صف: {seat.get('row', '—')} · "
                f"كرسي: <b>{seat.get('seat_number', '—')}</b>\n"
            )
        lines.append(
            f"✅ <code>{r['label']}</code> — {r['quantity']} تذكرة\n"
            f"{seat_line}"
            f"   💳 <a href=\"{r['payment_url']}\">اضغط للدفع</a>"
        )
    for r in fail:
        lbl = r.get('label') or r.get('account_id')
        err_msg = (r.get('error') or '')[:200]
        lines.append(f"❌ <code>{lbl}</code>: {err_msg}")

    if succ:
        lines.append("\n⏱️ <i>صلاحية روابط الدفع محدودة — سارع!</i>")

    # If ALL bookings failed, offer a manual-fallback link to the event page
    if not succ and fail:
        lines.append(
            f"\n💡 <i>يمكنك فتح الفعالية يدوياً وإكمال الحجز من المتصفح.</i>"
        )

    keyboard_rows = []
    for r in succ:
        if r.get("payment_url"):
            keyboard_rows.append([
                {"text": f"💳 دفع {r['label']}", "url": r["payment_url"]}
            ])
    # Always offer a link to the event's public booking page as a fallback
    keyboard_rows.append([
        {"text": "🌐 فتح صفحة الحجز يدوياً",
         "url": f"https://webook.com/ar/events/{slug}/book"}
    ])
    keyboard_rows.append([{"text": "⬅️ القائمة", "callback_data": "menu"}])

    # Booking concluded — wipe the cart so the next /start is fresh.
    cart.clear(chat_id)

    await notifier.edit(chat_id, msg_id, "\n".join(lines),
                        reply_markup={"inline_keyboard": keyboard_rows})


async def _show_account(chat_id: str, msg_id: int, acc_id: str,
                        notifier: Notifier) -> None:
    acc = get_account(acc_id)
    if not acc:
        await notifier.edit(chat_id, msg_id, "الحساب غير موجود.",
                            reply_markup=kb.back_to_menu())
        return
    status_map = {
        "ready": "✅ جاهز",
        "refreshing": "🔄 جارٍ التحديث",
        "new": "🆕 جديد — يحتاج تسجيل دخول",
        "needs_relogin": "⚠️ يحتاج إعادة تسجيل دخول",
        "blocked": "🚫 محظور",
    }
    status = status_map.get(acc.get("status", "new"), acc.get("status", "new"))
    last = acc.get("last_used_at") or 0
    last_str = ("منذ " + _ago(last)) if last else "لم يُستخدم بعد"
    exp = acc.get("token_expires_at") or 0
    exp_str = ("ينتهي بعد " + _until(exp)) if exp > time.time() else "منتهٍ"
    err = acc.get("last_error")

    txt = (
        f"👤 <b>{acc.get('label')}</b>\n"
        f"📧 {acc.get('email')}\n"
        f"📊 الحالة: <b>{status}</b>\n"
        f"🔑 التوكن: {exp_str}\n"
        f"🕐 آخر استخدام: {last_str}\n"
        f"🎫 تذاكر محجوزة: <b>{acc.get('tickets_booked', 0)}</b>"
    )
    if err:
        txt += f"\n\n⚠️ <i>آخر خطأ:</i> <code>{err[:150]}</code>"
    await notifier.edit(
        chat_id, msg_id, txt,
        reply_markup=kb.account_actions(acc_id, acc.get("status", "new")))


async def _login_flow(chat_id: str, msg_id: int, acc_id: str,
                      notifier: Notifier) -> None:
    acc = get_account(acc_id)
    if not acc:
        await notifier.edit(chat_id, msg_id, "الحساب غير موجود.",
                            reply_markup=kb.back_to_menu())
        return
    await notifier.edit(
        chat_id, msg_id,
        f"🔐 <b>تسجيل الدخول</b>\n\n"
        f"👤 {acc['label']}\n"
        f"📧 {acc['email']}\n\n"
        f"⏳ جارٍ الاتصال بـ webook.com...\n"
        f"🤖 <i>يُحلّ reCAPTCHA تلقائياً.</i>",
        reply_markup=None,
    )
    res = await auth_service.login_account(acc_id, notifier)
    if res.get("ok"):
        user = res.get("user", {})
        exp_days = int((res["tokens"]["expires_at"] - time.time()) / 86400)
        await notifier.send(
            chat_id,
            f"✅ <b>تم الدخول بنجاح</b>\n\n"
            f"👤 <b>{user.get('name') or acc['label']}</b>\n"
            f"📧 {user.get('email', acc['email'])}\n"
            f"🔑 التوكن صالح لمدة: <b>{exp_days} يوم</b>\n\n"
            f"🎉 الحساب جاهز للحجز.",
            reply_markup=kb.accounts_keyboard(list_accounts()),
        )
    else:
        await notifier.send(
            chat_id,
            f"❌ <b>فشل تسجيل الدخول</b>\n\n"
            f"السبب: <code>{res.get('error')[:200]}</code>",
            reply_markup=kb.accounts_keyboard(list_accounts()),
        )


async def _show_bookings(chat_id: str, notifier: Notifier,
                         edit_msg_id: int | None = None) -> None:
    bks = list_bookings(chat_id=chat_id, limit=10)
    if not bks:
        txt = "📋 لا توجد حجوزات بعد."
    else:
        lines = ["📋 <b>حجوزاتك الأخيرة</b>\n"]
        for b in bks:
            seat = b.get("seat_info") or {}
            s_str = ""
            if seat.get("section") or seat.get("seat_number"):
                s_str = (f" ({seat.get('section', '')} / "
                         f"كرسي {seat.get('seat_number', '')})")
            title = (b.get("event_title") or "—")[:40]
            lines.append(
                f"• <b>{title}</b>\n"
                f"  {b.get('ticket_type', '')} × {b.get('quantity')}"
                f"{s_str}\n"
                f"  💳 <a href=\"{b.get('payment_url', '')}\">رابط الدفع</a>"
            )
        txt = "\n".join(lines)
    rkb = kb.back_to_menu()
    if edit_msg_id:
        await notifier.edit(chat_id, edit_msg_id, txt, reply_markup=rkb)
    else:
        await notifier.send(chat_id, txt, reply_markup=rkb)


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════
def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60: return f"{d} ث"
    if d < 3600: return f"{d // 60} د"
    if d < 86400: return f"{d // 3600} س"
    return f"{d // 86400} ي"


def _until(ts: float) -> str:
    d = max(0, int(ts - time.time()))
    if d < 60: return f"{d} ث"
    if d < 3600: return f"{d // 60} د"
    if d < 86400: return f"{d // 3600} س"
    return f"{d // 86400} ي"


# ════════════════════════════════════════════════════════════════════════
# Long-poll fallback
# ════════════════════════════════════════════════════════════════════════
async def long_poll_loop(notifier: Notifier) -> None:
    # Wait for a valid bot token (admin may set it via /admin after boot)
    while not notifier.token:
        log.info("🤖 waiting for TELEGRAM_BOT_TOKEN (set via /admin)…")
        await asyncio.sleep(15)
    try:
        await notifier.delete_webhook()
    except Exception:
        pass
    offset = None
    log.info("🤖 long-polling started")
    while True:
        if not notifier.token:
            await asyncio.sleep(15)
            continue
        try:
            data = await notifier.get_updates(offset=offset, timeout=25)
            if data and data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    asyncio.create_task(dispatch(upd, notifier))
        except Exception as e:
            log.warning(f"long-poll err: {e}")
            await asyncio.sleep(3)
