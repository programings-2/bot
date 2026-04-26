# ⚽ Webook Football Sniper Bot — v3.1

بوت تيليجرام تفاعلي لحجز تذاكر **مباريات كرة القدم فقط** على **webook.com** تلقائياً. يعمل على Render Free.

## 🆕 ما الجديد في v3.1

- **فلتر كرة القدم الصارم:** يتجاهل البوت تلقائياً أي فعالية غير مباريات كرة القدم (`sport.slug == 'football'`).
- **واجهة تفاعلية 4 خطوات:** اختيار الفريق → اختيار القطاع → اختيار الفئة السعرية → تحديد العدد.
- **سلة تسوق وشاشة مراجعة:** خيارات المستخدم تُخزّن في سلة مؤقتة، ولا يتم الحجز إلا بعد ضغط زر التأكيد.
- **أرقام المقاعد الدقيقة:** يستخرج البوت أرقام المقاعد المحجوزة من cart-items API ويعرضها في رسالة الدفع.
- **تصنيف ذكي للقطاعات:** شمالي/جنوبي/شرقي/غربي → جمهور الفريق المضيف/الزائر/محايد.

---

## ✨ المميزات

| المجال | الوصف |
|---|---|
| 🧭 **واجهة بالأزرار** | قائمة تفاعلية كاملة عبر InlineKeyboard — لا أوامر معقدة |
| 🔐 **تسجيل دخول مرة واحدة** | Playwright يستخدم فقط عند login، ثم JWT Bearer يُحفظ ويُجدَّد تلقائياً |
| 🧩 **حل reCAPTCHA بالتفاعل** | يرسل لك البوت صورة الـ captcha على تيليجرام، وترد بـ `ok` بعد حلّها |
| 👥 **توزيع ذكي** | خوارزمية تقسّم عدد التذاكر على حسابات متعددة احتراماً لـ `max_per_order` |
| 📡 **API-first** | جميع عمليات الحجز تتم عبر `api.webook.com` مباشرةً (aiohttp)، سرعة × 20 |
| 🔥 **قنّاص سباق الثواني** | حلقة بمعدل 2 ثانية تلتقط لحظة افتتاح البيع |
| 🔔 **مراقبة خلفية** | يجلب كل 5 دقائق الفعاليات الجديدة وينبّه على كلمات متابعة تختارها |
| 💤 **Keep-Alive** | ping داخلي + endpoint `/health` يمنع نوم Render المجاني |
| 🗃️ **SQLite مستمرة** | المهام والحجوزات والحسابات تبقى بعد إعادة التشغيل |
| 🖥️ **لوحة ويب** | صفحة `/` فيها إحصائيات حيّة |

---

## 🧭 تدفق الاستخدام

1. أرسل للبوت `/start` → قائمة رئيسية
2. **إدارة الحسابات** → ➕ إضافة حساب → تعطي الإيميل → تعطي كلمة المرور
3. اضغط **🔐 تسجيل الدخول** — إذا ظهر reCAPTCHA تتبع رسائل البوت.
4. **المباريات المتاحة** → اختر مباراة كرة قدم
5. اختر **جمهورك:** المضيف / الزائر / المنصات VIP / أخرى
6. اختر **القطاع** → ثم **الفئة السعرية** (الواجهة/الدرجة الأولى/خلف المرمى)
7. أدخل عدد التذاكر برسالة نصية
8. راجع السلة ثم اضغط **تأكيد الحجز** — ستصلك روابط PayTabs مع أرقام المقاعد

---

## 🏗️ البنية

```
app/
├── core/
│   ├── config.py           ← إعدادات من env
│   ├── logging_setup.py    ← logger موحد
│   └── storage.py          ← PostgreSQL/SQLite (+ أعمدة الفريقين)
├── services/
│   ├── webook_api.py       ← aiohttp client + get_football_event_detail
│   ├── football_filter.py  ← ⚡️ فلترة وتصنيف القطاعات (جديد)
│   ├── shopping_cart.py    ← 🛒 سلة تسوق مؤقتة (جديد)
│   ├── event_discovery.py  ← استكشاف + فلتر كرة القدم
│   ├── auth_service.py     ← Playwright login + JWT refresh
│   ├── captcha_broker.py   ← ربط Playwright بحوار Telegram
│   ├── distributor.py      ← توزيع التذاكر
│   ├── booking_http.py     ← حجز HTTP مباشر + استخراج المقاعد
│   ├── booking_playwright.py ← fallback متصفح + scrape المقاعد
│   ├── booking_orchestrator.py ← حجز متوازي
│   ├── event_monitor.py    ← حلقات المراقبة الخلفية
│   └── keep_alive.py       ← منع النوم
└── bot/
    ├── notifier.py         ← Telegram Bot API wrapper
    ├── keyboards.py        ← لوحات تفاعلية (تتضمّن team/sector/cart)
    ├── state.py            ← FSM للمحادثات متعددة الخطوات
    └── handlers.py         ← dispatcher + جميع الـ handlers
main.py                     ← FastAPI + lifespan + webhook
```

---

## 🚀 النشر على Render

1. ادخل إلى [Render Dashboard](https://dashboard.render.com)
2. Render سيلتقط `render.yaml` — Docker، خطة Free، منطقة Frankfurt
3. عيّن متغيرات البيئة السرية:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. اضغط Deploy — البوت سيرد على `/start` خلال دقائق

**نصيحة:** أضف `https://your-app.onrender.com/ping` إلى [UptimeRobot](https://uptimerobot.com) مجاناً لضمان عدم النوم مطلقاً.

---

## ⚙️ متغيرات البيئة

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | ❗ مطلوب |
| `TELEGRAM_CHAT_ID` | — | ❗ مطلوب |
| `AUTHORIZED_CHAT_IDS` | = CHAT_ID | معرفات مصرح لها إضافية (فواصل) |
| `WEBOOK_LANG` | `en` | لغة API (en\|ar) |
| `WEBOOK_PUBLIC_TOKEN` | ثابت | توكن العامة للـ webook |
| `EVENT_POLL_INTERVAL` | `300` | ث بين فحوص اكتشاف الفعاليات |
| `SNIPER_POLL_INTERVAL` | `2` | ث بين تكات القنّاص |
| `KEEP_ALIVE_INTERVAL` | `600` | ث بين ping خارجي |
| `LOGIN_CAPTCHA_TIMEOUT` | `180` | ث انتظار حل الـ captcha من المستخدم |
| `HEADLESS` | `true` | Chromium headless |
| `LOG_LEVEL` | `INFO` | `DEBUG` لتفاصيل أكثر |
دمة.
