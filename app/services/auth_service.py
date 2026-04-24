"""
Authentication service — multiple strategies:

  1) AUTO  : Playwright opens /login, dismisses cookies, executes
             reCAPTCHA v3 invisibly, POST /api/v2/login with the captcha
             token, captures the JWT access_token (valid ~7 days).
  2) MANUAL: User pastes an access_token obtained from their own browser
             DevTools. Zero-dependency fallback if Playwright can't reach
             webook (Cloudflare throttles, Render slow, etc.).

After login, EVERYTHING is pure HTTP — Playwright is closed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from app.core.config import (
    HEADLESS, LOGIN_CAPTCHA_TIMEOUT, TOKEN_REFRESH_MARGIN,
    WEBOOK_API, WEBOOK_LANG, WEBOOK_ORIGIN, WEBOOK_PUBLIC_TOKEN,
)
from app.core.storage import get_account, save_tokens, set_account_status

log = logging.getLogger("auth")

WEBOOK_RECAPTCHA_SITE_KEY = "6LcvYHooAAAAAC-G46bpymJKtIwfDQpg9DsHPMpL"

# Block noisy third-party domains to speed up page load on Render's slow box
BLOCKED_DOMAINS = (
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "amplitude.com",
    "taboola.com",
    "hotjar.com",
    "clarity.ms",
    "twitter.com",
    "t.co",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "bing.com",
    "yandex.ru",
    "branch.io",
)

# Block heavy resource types that we don't need to log in
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

_playwright_err: Optional[Exception] = None
try:
    from playwright.async_api import (
        async_playwright, TimeoutError as PWTimeout,
    )
except Exception as _e:  # pragma: no cover
    _playwright_err = _e
    PWTimeout = Exception  # type: ignore


class AuthError(Exception):
    pass


# ════════════════════════════════════════════════════════════════════════
# STRATEGY 1 — Automated login
# ════════════════════════════════════════════════════════════════════════
async def login_account(account_id: str, notifier=None,
                        max_attempts: int = 2) -> dict[str, Any]:
    """Automated login with retry."""
    if _playwright_err is not None:
        return {"ok": False,
                "error": f"Playwright غير مثبت: {_playwright_err}"}

    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "الحساب غير موجود"}

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        log.info(f"🔐 login attempt {attempt}/{max_attempts} for {account_id}")
        set_account_status(account_id, "refreshing")
        try:
            result = await _do_login_once(account_id, acc["email"],
                                          acc["password"])
            if result["ok"]:
                save_tokens(
                    account_id=account_id,
                    access=result["access_token"],
                    refresh="",
                    expires_at=result["expires_at"],
                    user_id=result.get("user_id"),
                )
                log.info(f"✅ login success {account_id}")
                return {
                    "ok": True,
                    "tokens": {
                        "access_token": result["access_token"],
                        "expires_at": result["expires_at"],
                        "user_id": result.get("user_id"),
                    },
                    "user": result.get("user") or {},
                }
            last_error = result.get("error", "غير معروف")
            log.warning(f"attempt {attempt} failed: {last_error}")
            if attempt < max_attempts:
                await asyncio.sleep(3)
        except Exception as e:
            last_error = str(e)[:250]
            log.exception(f"attempt {attempt} crashed")
            if attempt < max_attempts:
                await asyncio.sleep(3)

    set_account_status(account_id, "needs_relogin", last_error[:300])
    return {"ok": False, "error": last_error}


async def _do_login_once(account_id: str, email: str,
                         password: str) -> dict[str, Any]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ar-SA",
            # Disable JavaScript permissions we don't need
            permissions=[],
        )

        # ── Resource blocking (big speed boost) ────────────────────────
        async def _route(route):
            req = route.request
            url = req.url
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            if any(d in url for d in BLOCKED_DOMAINS):
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", _route)

        page = await ctx.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)

        try:
            # 1) Open the login page
            await page.goto(
                f"{WEBOOK_ORIGIN}/en/login",
                wait_until="domcontentloaded",
                timeout=45000,
            )

            # 2) Dismiss cookie banner if present (polling for up to 5s)
            for _ in range(10):
                for sel in (
                    "button:has-text('Reject all')",
                    "button:has-text('Accept all')",
                    "button:has-text('Accept')",
                ):
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await page.wait_for_timeout(500)
                            break
                    except Exception:
                        pass
                else:
                    await page.wait_for_timeout(500)
                    continue
                break

            # 3) Wait for grecaptcha — up to 45 seconds (Render is slow!)
            log.info("   waiting for grecaptcha…")
            try:
                await page.wait_for_function(
                    "() => window.grecaptcha && "
                    "typeof window.grecaptcha.execute === 'function'",
                    timeout=45000,
                )
            except PWTimeout:
                # Last-ditch effort: inject the script manually
                log.warning("grecaptcha not loaded — injecting manually")
                await page.evaluate(f"""
                  () => new Promise((resolve) => {{
                    if (window.grecaptcha && grecaptcha.execute) return resolve();
                    const s = document.createElement('script');
                    s.src = 'https://www.google.com/recaptcha/api.js'
                          + '?render={WEBOOK_RECAPTCHA_SITE_KEY}';
                    s.onload = () => resolve();
                    s.onerror = () => resolve();
                    document.head.appendChild(s);
                  }})
                """)
                await page.wait_for_function(
                    "() => window.grecaptcha && "
                    "typeof window.grecaptcha.execute === 'function'",
                    timeout=20000,
                )

            # 4) Execute reCAPTCHA v3 (invisible)
            captcha_token = await page.evaluate(f"""
              () => new Promise((resolve, reject) => {{
                grecaptcha.ready(() => {{
                  grecaptcha.execute('{WEBOOK_RECAPTCHA_SITE_KEY}',
                    {{action: 'login'}}).then(resolve).catch(reject);
                }});
              }})
            """)
            if not captcha_token or len(captcha_token) < 30:
                raise AuthError("لم يُولَّد captcha token صالح")

            # 5) POST /api/v2/login from the page's fetch (native origin)
            result = await page.evaluate(f"""
              async () => {{
                const r = await fetch('{WEBOOK_API}/login', {{
                  method: 'POST',
                  credentials: 'include',
                  headers: {{
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'token': '{WEBOOK_PUBLIC_TOKEN}',
                    'authorization': 'Bearer',
                    'accept-language': 'ar-SA',
                  }},
                  body: JSON.stringify({{
                    email: {json.dumps(email)},
                    password: {json.dumps(password)},
                    captcha: {json.dumps(captcha_token)},
                    lang: '{WEBOOK_LANG}',
                  }}),
                }});
                return {{status: r.status, body: await r.text()}};
              }}
            """)

            try:
                body = json.loads(result.get("body") or "{}")
            except Exception:
                body = {}

            if result.get("status") != 200 or body.get("status") != "success":
                err_info = body.get("error") or body.get("message") \
                           or result.get("body", "")[:200]
                raise AuthError(f"رفض الخادم: {err_info}")

            data = body.get("data") or {}
            access_token = data.get("access_token")
            if not access_token:
                raise AuthError("الاستجابة لا تحتوي على access_token")

            return {
                "ok": True,
                "access_token": access_token,
                "expires_at": _jwt_expiry(access_token)
                              or (time.time() + 7 * 86400),
                "user_id": data.get("_id"),
                "user": {
                    "name": data.get("name") or data.get("first_name", ""),
                    "email": data.get("email", email),
                },
            }
        except (PWTimeout, AuthError) as e:
            return {"ok": False, "error": str(e)[:250]}
        except Exception as e:
            return {"ok": False, "error": f"خطأ داخلي: {str(e)[:200]}"}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════
# STRATEGY 2 — Manual token paste
# ════════════════════════════════════════════════════════════════════════
async def login_with_manual_token(account_id: str, access_token: str
                                   ) -> dict[str, Any]:
    """
    Accept a raw JWT access_token from the user (captured from their own
    browser's localStorage). Validates it and persists if good.
    """
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "الحساب غير موجود"}

    access_token = (access_token or "").strip()
    # Trim "Bearer " if the user pasted it too
    if access_token.lower().startswith("bearer "):
        access_token = access_token[7:].strip()

    # Basic JWT sanity check
    if not access_token.startswith("eyJ") or access_token.count(".") != 2:
        return {"ok": False, "error": "التوكن ليس بصيغة JWT صالحة"}

    expires_at = _jwt_expiry(access_token)
    if not expires_at:
        return {"ok": False,
                "error": "تعذّر قراءة صلاحية التوكن من محتواه"}
    if expires_at < time.time() + 300:
        return {"ok": False,
                "error": "التوكن منتهي الصلاحية أو على وشك الانتهاء"}

    # Probe a private endpoint to make sure the token actually works
    import aiohttp
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(
                f"{WEBOOK_API}/currencies?lang={WEBOOK_LANG}&visible_in=rs",
                headers={
                    "accept": "application/json",
                    "token": WEBOOK_PUBLIC_TOKEN,
                    "authorization": f"Bearer {access_token}",
                    "accept-language": WEBOOK_LANG,
                    "user-agent": "Mozilla/5.0",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return {"ok": False,
                            "error": f"التوكن غير مقبول من webook ({r.status})"}
        except Exception as e:
            return {"ok": False, "error": f"تعذّر التحقق: {e}"}

    # Extract user_id from JWT sub claim
    user_id = _jwt_sub(access_token) or ""

    save_tokens(
        account_id=account_id,
        access=access_token,
        refresh="",
        expires_at=expires_at,
        user_id=user_id,
    )
    log.info(f"✅ manual token accepted for {account_id}")
    return {
        "ok": True,
        "tokens": {
            "access_token": access_token,
            "expires_at": expires_at,
            "user_id": user_id,
        },
    }


# ════════════════════════════════════════════════════════════════════════
# Token maintenance
# ════════════════════════════════════════════════════════════════════════
async def get_valid_bearer(account_id: str, notifier=None,
                           auto_relogin: bool = True) -> Optional[str]:
    acc = get_account(account_id)
    if not acc:
        return None
    token = acc.get("access_token") or ""
    expires_at = acc.get("token_expires_at") or 0

    if token and time.time() < (expires_at - TOKEN_REFRESH_MARGIN):
        return token

    if not auto_relogin:
        return token or None

    log.info(f"token expiring for {account_id} — auto re-login")
    res = await login_account(account_id, notifier)
    if res.get("ok"):
        return res["tokens"]["access_token"]
    return None


# ════════════════════════════════════════════════════════════════════════
# JWT helpers
# ════════════════════════════════════════════════════════════════════════
def _jwt_payload(token: str) -> Optional[dict]:
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None


def _jwt_expiry(token: str) -> Optional[float]:
    p = _jwt_payload(token) or {}
    exp = p.get("exp")
    return float(exp) if exp else None


def _jwt_sub(token: str) -> Optional[str]:
    p = _jwt_payload(token) or {}
    return p.get("sub") or None
