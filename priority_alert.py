# -*- coding: utf-8 -*-
"""
priority_alert.py — يراقب انتهاء "الحجز المبكر" (أولوية العضويات) في ويبوك ويبلّغ قناة VIP.

- يسجل دخول بحساب ويبوك احتياطي (بدون عضوية) عشان تطلع له صفحة الأولوية.
- كل PRIORITY_EVERY ثانية يفتح صفحة الحجز (/book) لكل مباراة في PRIORITY_URLS.
- يقرأ الصفحة بس: ما يختار فريق ولا يحجز.
- إذا اختفت رسالة "الحجز المبكر" وظهرت صفحة اختيار التذاكر -> تنبيه لقناة VIP.
- إذا فشل الدخول: يبلّغك انت بس (ADMIN_CHAT_ID) وما يحاول يتجاوز أي تحقق.
- يشتغل RUN_MINUTES وبعدها يطلع، وملف التشغيل يعيد تشغيله.

المتغيرات (Secrets / env):
  WEBOOK_EMAIL, WEBOOK_PASSWORD   الحساب الاحتياطي (مطلوبة)
  VIP_BOT_TOKEN                   بوت VIP (مطلوب)
  VIP_CHAT_ID     رقم قناة VIP (افتراضي -1004418967399)
  ADMIN_CHAT_ID   رقمك لتنبيهات الأعطال (افتراضي 881479054)
  PRIORITY_URLS   روابط صفحات المباريات مفصولة بفاصلة
  PRIORITY_EVERY  كل كم ثانية نفحص (افتراضي 60)
  RUN_MINUTES     مدة التشغيل (افتراضي 340)
  DRY_RUN         true يطبع الرسائل بدون إرسال
"""

import os
import re
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

# ======== المباريات اللي نراقب انتهاء أولويتها ========
# حط روابط صفحات المباريات (مو صفحة الحجز) مفصولة بفاصلة. غيّرها متى ما بغيت.
PRIORITY_URLS = [u.strip() for u in os.getenv(
    "PRIORITY_URLS",
    "https://webook.com/ar/sa/ruh/sports-event/events/rsl-26-27-nassr-vs-al-hilal-m7g1a8l6z5",
).split(",") if u.strip()]
# =====================================================

PRIORITY_EVERY = float(os.getenv("PRIORITY_EVERY", "60"))
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "340"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
WEBOOK_EMAIL = os.getenv("WEBOOK_EMAIL", "").strip()
WEBOOK_PASSWORD = os.getenv("WEBOOK_PASSWORD", "")
VIP_BOT_TOKEN = os.getenv("VIP_BOT_TOKEN", "").strip()
VIP_CHAT_ID = os.getenv("VIP_CHAT_ID", "-1004418967399").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "881479054").strip()
# قناة الأصحاب المجانية (نفس تنبيه انتهاء الأولوية)
FRIENDS_BOT_TOKEN = os.getenv("FRIENDS_BOT_TOKEN", "").strip()
FRIENDS_CHAT_ID = os.getenv("FRIENDS_CHAT_ID", "").strip()
STATE_FILE = "priority_state.json"

PRIORITY_WORDS = ["الحجز المبكر", "العضويات الموسمية", "أولويتك بالحجز"]
OPEN_WORDS = ["اختر التذاكر", "فحص أمني", "تفاصيل الحجز", "اختر الفريق", "اختر مقاعدك"]

DERBIES = [
    ({"ittihad", "ahli"}, "ديربي جدة"),
    ({"nassr", "hilal"}, "ديربي الرياض"),
]


# ---------------- تيليجرام ----------------
def send(text, chat_id=None, token=None):
    chat = chat_id or VIP_CHAT_ID
    token = token or VIP_BOT_TOKEN
    if DRY_RUN or not token:
        print(f"🧪 [{chat}]\n{text}")
        return True
    body = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if json.load(r).get("ok"):
                    return True
        except Exception as e:
            print(f"⚠️ تيليجرام محاولة {i + 1}: {e}")
        time.sleep(2)
    return False


# ---------------- الحالة ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ حفظ الحالة: {e}")


# ---------------- ويبوك ----------------
COOKIE_JS = r"""() => {
  const dlgs = [...document.querySelectorAll('[role="dialog"]')].filter(d =>
    d.querySelector('[form="cookie_consent_settings"], #cookie_consent_settings') ||
    /cookie|كوكيز|ملفات تعريف/i.test(d.innerText || ''));
  if (!dlgs.length) return 'none';
  const btns = dlgs.flatMap(d => [...d.querySelectorAll('button')]);
  const rej = btns.find(b => /رفض|الضرورية فقط|الأساسية فقط|Reject|Necessary only|Decline/i.test(b.innerText || ''));
  if (rej) { rej.click(); return 'rejected'; }
  dlgs.forEach(d => d.remove());
  document.querySelectorAll('[data-radix-portal], [data-state="open"][data-aria-hidden], .fixed.inset-0').forEach(o => {
    if (!o.querySelector('input')) o.remove(); });
  document.body.style.pointerEvents = '';
  return 'removed';
}"""


def dismiss_cookies(pg):
    """يقفل نافذة الكوكيز (يختار الضروري فقط إذا لقاه، وإلا يشيل النافذة)."""
    try:
        r = pg.evaluate(COOKIE_JS)
        if r != "none":
            print(f"   🍪 نافذة الكوكيز: {r}")
            pg.wait_for_timeout(800)
            if r == "rejected":
                pg.evaluate(COOKIE_JS)
    except Exception as e:
        print(f"   ⚠️ الكوكيز: {e}")


def click_submit(pg):
    btn = pg.locator('button[type="submit"]')
    if btn.count():
        try:
            btn.first.click(timeout=5000)
        except Exception:
            btn.first.evaluate("el => el.click()")
        return
    pg.locator("button", has_text=re.compile(
        "تسجيل الدخول|دخول|متابعة|التالي|Log in|Login|Continue", re.I)).first.click(timeout=10000)


def login(browser):
    """يسجل دخول بالحساب الاحتياطي. ما يتجاوز أي تحقق: إذا طلع تحقق يفشل ويبلغك."""
    ctx = browser.new_context(locale="ar-SA")
    pg = ctx.new_page()
    ok = False
    try:
        pg.goto("https://webook.com/ar/login", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(3000)
        dismiss_cookies(pg)
        email = pg.locator('input[type="email"], input[name*="email" i], '
                           'input[autocomplete="username"], input[autocomplete="email"]').first
        email.fill(WEBOOK_EMAIL, timeout=15000)
        pwd = pg.locator('input[type="password"]').first
        if not pwd.is_visible():
            email.press("Enter")
            pg.wait_for_timeout(1500)
            if not pwd.is_visible():
                dismiss_cookies(pg)
                click_submit(pg)
            pwd.wait_for(state="visible", timeout=15000)
        pwd.fill(WEBOOK_PASSWORD)
        pwd.press("Enter")
        pg.wait_for_timeout(2500)
        if "/login" in pg.url:
            dismiss_cookies(pg)
            try:
                click_submit(pg)
            except Exception:
                pass
        pg.wait_for_timeout(7000)
        ok = "/login" not in pg.url
        if not ok:
            print(f"   ↳ بعد الدخول بقيت على: {pg.url}")
    except Exception as e:
        print(f"⚠️ تسجيل الدخول: {e}")
    finally:
        try:
            pg.close()
        except Exception:
            pass
    if not ok:
        try:
            ctx.close()
        except Exception:
            pass
        return None
    print("🔑 سجلت دخول ويبوك (الحساب الاحتياطي)")
    return ctx


def read_status(ctx, url):
    """يرجع (الحالة, العنوان): priority / open / logged_out / error / None (مو واضح)."""
    pg = None
    try:
        pg = ctx.new_page()
        pg.goto(url.rstrip("/") + "/book", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1500)
        dismiss_cookies(pg)
        try:
            pg.wait_for_function(
                "(w) => w.some(x => document.body && document.body.innerText.includes(x))",
                arg=PRIORITY_WORDS + OPEN_WORDS, timeout=15000)
        except Exception:
            pass
        if "/login" in pg.url:
            return "logged_out", ""
        body = pg.inner_text("body")
    except Exception as e:
        print(f"⚠️ فحص {url}: {e}")
        return "error", ""
    finally:
        if pg:
            try:
                pg.close()
            except Exception:
                pass
    m = re.search(r"[^\n]*(?:^|\s)ضد(?:\s|$)[^\n]*", body)
    title = m.group(0).strip() if m else ""
    if any(w in body for w in PRIORITY_WORDS):
        return "priority", title
    if any(w in body for w in OPEN_WORDS):
        return "open", title
    return None, title


def message(title, url):
    key = url.split("/events/")[-1].lower()
    tokens = set(re.split(r"-", key))
    derby = next((name for clubs, name in DERBIES if clubs <= tokens), None)
    lines = [f"🔥🔥 {derby} — انتهت الأولوية 🔥🔥" if derby else "🔓 انتهت الأولوية"]
    if title:
        lines.append(f"🏟️ {title}")
    lines += ["✅ الحجز صار متاح للجميع الحين", "⚡ الحق قبل لا تخلص!", f"🔗 {url}"]
    return "\n\n".join(lines)


# ---------------- الحلقة الرئيسية ----------------
def main():
    if not PRIORITY_URLS:
        print("ℹ️ ما فيه روابط في PRIORITY_URLS")
        return
    if not (WEBOOK_EMAIL and WEBOOK_PASSWORD):
        print("❌ لازم WEBOOK_EMAIL و WEBOOK_PASSWORD")
        send("⚠️ مراقبة الأولوية متوقفة: ما فيه إيميل أو كلمة مرور الحساب الاحتياطي في GitHub Secrets.",
             chat_id=ADMIN_CHAT_ID)
        return

    state = load_state()
    deadline = time.time() + RUN_MINUTES * 60
    warned = False
    ctx = None
    loop = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        while time.time() < deadline:
            loop += 1
            t0 = time.time()
            if ctx is None:
                ctx = login(browser)
                if ctx is None:
                    if not warned:
                        send("⚠️ ما قدرت أسجل دخول ويبوك بالحساب الاحتياطي، فمراقبة الأولوية متوقفة. "
                             "تأكد من الإيميل وكلمة المرور في GitHub Secrets.", chat_id=ADMIN_CHAT_ID)
                        warned = True
                    time.sleep(max(60, PRIORITY_EVERY))
                    continue
                warned = False

            for url in PRIORITY_URLS:
                status, title = read_status(ctx, url)
                if status in ("logged_out", "error"):
                    print(f"↻ {status}: بعيد تسجيل الدخول")
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    ctx = None
                    break
                if status is None:
                    print(f"❔ ما اتضحت الحالة: {title or url}")
                    continue
                prev = state.get(url)
                if prev != status:
                    state[url] = status
                    save_state(state)
                if prev is None:
                    print(f"👀 بدأت أراقب: {title or url} — {'أولوية' if status == 'priority' else 'متاح للجميع'}")
                elif prev == "priority" and status == "open":
                    print(f"🔓 انتهت الأولوية: {title or url}")
                    msg = message(title, url)
                    send(msg)
                    if FRIENDS_BOT_TOKEN and FRIENDS_CHAT_ID:
                        send(msg, chat_id=FRIENDS_CHAT_ID, token=FRIENDS_BOT_TOKEN)

            took = time.time() - t0
            if loop % 30 == 1:
                print(f"🔁 فحص #{loop} خلص في {took:.1f} ث — {json.dumps(state, ensure_ascii=False)}")
            time.sleep(max(5.0, PRIORITY_EVERY - took))

        save_state(state)
        try:
            browser.close()
        except Exception:
            pass
    print("🏁 انتهى وقت التشغيل، ملف التشغيل بيعيده")


if __name__ == "__main__":
    main()
