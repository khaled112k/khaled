# -*- coding: utf-8 -*-
"""
x_alert.py — مراقب ويبوك السريع وينشر التنبيه في X (تويتر) فوراً.

- يفتح المتصفح مرة وحدة ويعيد فحص الصفحة كل ~12 ثانية (بدل ما يفتح ويسكّر).
- يحجب الصور والفيديو والإعلانات عشان التحميل يكون أسرع.
- يطابق الأندية من رابط المباراة (al-hilal, al-nassr...) وهذا أدق من النص العربي.
- أول تشغيل: يحفظ المباريات الموجودة بدون ما ينشر (عشان ما يصير سبام).
- يشتغل لمدة RUN_MINUTES وبعدها يطلع، وملف التشغيل يعيد تشغيله.

المتغيرات (Secrets / env):
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   (مطلوبة)
  TARGET_URLS   روابط الصفحات مفصولة بفاصلة (افتراضي: صفحة كرة القدم)
  POLL_SECONDS  الفاصل بين كل فحص بالثواني (افتراضي 12)
  RUN_MINUTES   مدة التشغيل قبل ما يطلع (افتراضي 340 = 5 ساعات و40 دقيقة)
  INCLUDE_LINK  true يحط رابط ويبوك بالتغريدة (أغلى: ~0.20$ بدل ~0.015$)
  ALL_CLUBS     true ينشر كل المباريات مو بس الأندية المحددة
  DRY_RUN       true يطبع التغريدة بدون ما ينشرها (للتجربة)
"""

import os
import re
import json
import time
import random
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# ---------------- الإعدادات ----------------
TARGET_URLS = [u.strip() for u in os.getenv(
    "TARGET_URLS", "https://webook.com/ar/explore?tag=football").split(",") if u.strip()]
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "12"))
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "340"))
INCLUDE_LINK = os.getenv("INCLUDE_LINK", "false").lower() == "true"
ALL_CLUBS = os.getenv("ALL_CLUBS", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

CACHE_FILE = "x_seen_events.json"
MAX_POST_ATTEMPTS = 3

# الأندية: كلمات تظهر في رابط المباراة  ->  الوسم
CLUBS = [
    (["al-hilal"], "#الهلال"),
    (["al-nassr"], "#النصر"),
    (["al-ittihad"], "#الاتحاد"),
    (["al-ahli"], "#الأهلي"),
    (["al-qadsiah", "al-qadisiyah", "al-qadsia", "al-qadisiya"], "#القادسية"),
]

EVENT_SELECTOR = 'a[href*="/events/"]'
BLOCK_TYPES = {"image", "media", "font"}
# نحجب الإعلانات والتحليلات بس (ويبوك تجيب المباريات من دومين ثاني: webook.cmscloud.ai)
BLOCK_HOSTS = (
    "googletagmanager", "google-analytics", "analytics.google", "doubleclick",
    "googlesyndication", "googleadservices", "facebook", "hotjar", "clarity.ms",
    "crazyegg", "amplitude", "ads-twitter", "analytics.twitter", "tiktok",
    "snapchat", "sc-static", "adform", "app-us1.com", "stat-rock", "cloudflareinsights",
)


# ---------------- حفظ المباريات اللي شفناها ----------------
def load_seen():
    if not os.path.exists(CACHE_FILE):
        return None  # أول تشغيل
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = set(json.load(f))
        return data or None  # ملف فاضي = نعامله كأول تشغيل (عشان ما يصير سبام)
    except Exception:
        return None


def save_seen(seen):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


# ---------------- قراءة بطاقة المباراة ----------------
def event_key(href):
    """مفتاح ثابت للمباراة بغض النظر عن اللغة أو المدينة: الجزء بعد /events/"""
    return href.split("/events/")[-1].split("?")[0].strip("/").lower()


def full_url(href):
    return href if href.startswith("http") else f"https://webook.com{href}"


def parse_card(text):
    """
    مثال النص:
    فعالية · 28 نوفمبر | 4:00 م | دوري روشن 26/27 – الأهلي ضد التعاون | جدة, المملكة العربية السعودية | يبدأ من | 20
    """
    parts = [p.strip() for p in re.split(r"[\n|]+", text or "") if p.strip()]
    info = {"title": "", "date": "", "time": "", "city": "", "price": ""}

    for i, p in enumerate(parts):
        if "·" in p and not info["date"]:
            info["date"] = p.split("·", 1)[1].strip()
        elif re.fullmatch(r"\d{1,2}:\d{2}\s*[صم]?", p) and not info["time"]:
            info["time"] = p
        elif "المملكة" in p and not info["city"]:
            info["city"] = p.split(",")[0].split("،")[0].strip()
        elif "يبدأ من" in p and not info["price"]:
            rest = p.replace("يبدأ من", "").strip()
            if rest:
                info["price"] = rest
            elif i + 1 < len(parts):
                info["price"] = parts[i + 1]

    titles = [p for p in parts if " ضد " in p or " vs " in p.lower()]
    if titles:
        info["title"] = titles[0]
    else:
        skip = {info["date"], info["time"], info["price"], "يبدأ من"}
        cands = [p for p in parts if p not in skip and "·" not in p and "المملكة" not in p]
        info["title"] = max(cands, key=len) if cands else ""

    # نشيل اسم البطولة الطويل من العنوان: "دوري روشن 26/27 – الأهلي ضد التعاون" -> "الأهلي ضد التعاون"
    for sep in ("–", "—", " - "):
        if sep in info["title"]:
            left, right = info["title"].split(sep, 1)
            if " ضد " in right:
                info["title"] = right.strip()
                info["league"] = left.strip()
            break
    return info


def club_tags(key):
    tags = []
    for variants, tag in CLUBS:
        if any(v in key for v in variants):
            tags.append(tag)
    return tags


# ---------------- التغريدة ----------------
def build_tweet(info, tags, url):
    title = info["title"] or "مباراة جديدة"
    if len(title) > 110:
        title = title[:107] + "..."

    lines = [f"🚨 نزلت التذاكر | {title}"]
    when = " ".join(x for x in (info["date"], info["time"]) if x)
    if when:
        lines.append(f"📅 {when}")
    if info["city"]:
        lines.append(f"📍 {info['city']}")
    if info["price"]:
        lines.append(f"💰 تبدأ من {info['price']} ريال")
    lines.append("⚡ متاحة الآن في ويبوك")
    if INCLUDE_LINK:
        lines.append(url)

    hashtags = " ".join(tags + ["#دوري_روشن", "#تذاكر"])
    text = "\n".join(lines) + "\n\n" + hashtags
    return text[:280]


_x_client = None


def get_x_client():
    global _x_client
    if _x_client is None:
        import tweepy
        _x_client = tweepy.Client(
            consumer_key=os.environ["X_API_KEY"],
            consumer_secret=os.environ["X_API_SECRET"],
            access_token=os.environ["X_ACCESS_TOKEN"],
            access_token_secret=os.environ["X_ACCESS_SECRET"],
        )
    return _x_client


def post_to_x(text):
    """يرجع True إذا انتشرت (أو كانت مكررة)، False إذا فشلت ونحاول مرة ثانية."""
    if DRY_RUN:
        print("🧪 DRY_RUN — التغريدة:\n" + text + "\n")
        return True
    import tweepy
    try:
        res = get_x_client().create_tweet(text=text)
        print(f"✅ انتشرت التغريدة: {res.data.get('id') if res and res.data else '?'}")
        return True
    except tweepy.TooManyRequests as e:
        reset = 60
        try:
            reset = max(5, int(e.response.headers.get("x-rate-limit-reset", 0)) - int(time.time()))
        except Exception:
            pass
        print(f"⏳ X: تجاوزت الحد، بنحاول بعد {min(reset, 300)} ثانية")
        time.sleep(min(reset, 300))
        return False
    except tweepy.Forbidden as e:
        msg = str(e).lower()
        if "duplicate" in msg:
            print("ℹ️ X: تغريدة مكررة، تم تجاهلها")
            return True
        print(f"❌ X رفض التغريدة (تأكد من صلاحية Read and Write أو الرصيد): {e}")
        return False
    except Exception as e:
        print(f"❌ فشل النشر في X: {e}")
        return False


# ---------------- المتصفح ----------------
def make_page(p):
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 900},
        locale="ar-SA",
        timezone_id="Asia/Riyadh",
    )

    def route_handler(route):
        req = route.request
        host = (urlparse(req.url).hostname or "").lower()
        if req.resource_type in BLOCK_TYPES:
            return route.abort()
        if any(b in host for b in BLOCK_HOSTS):
            return route.abort()
        return route.continue_()

    context.route("**/*", route_handler)
    page = context.new_page()
    page.set_default_timeout(25000)
    return browser, page


def scan(page, url):
    """يرجع قائمة [(href, text)] أو None إذا الصفحة ما حمّلت."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(EVENT_SELECTOR, timeout=20000)
    except Exception:
        try:
            print(f"   ↳ عنوان الصفحة: {page.title()!r} | الرابط: {page.url}")
        except Exception:
            pass
        return None
    page.wait_for_timeout(600)  # ثانية صغيرة عشان تكمل البطاقات
    return page.eval_on_selector_all(
        EVENT_SELECTOR,
        "els => els.map(e => [e.getAttribute('href') || '', e.innerText || ''])",
    )


# ---------------- الحلقة الرئيسية ----------------
def main():
    seen = load_seen()
    first_run = seen is None
    if first_run:
        seen = set()
        print("🆕 أول تشغيل: بحفظ المباريات الموجودة بدون نشر")

    attempts = {}  # key -> عدد محاولات النشر الفاشلة
    deadline = time.time() + RUN_MINUTES * 60
    fails_in_row = 0
    loop = 0
    previews = 0

    with sync_playwright() as p:
        browser, page = make_page(p)
        while time.time() < deadline:
            loop += 1
            t0 = time.time()
            for url in TARGET_URLS:
                try:
                    items = scan(page, url)
                except Exception as e:
                    items = None
                    print(f"❌ خطأ في التحميل: {e}")

                if items is None:
                    fails_in_row += 1
                    print(f"⚠️ الصفحة ما حمّلت ({fails_in_row} مرات متتالية)")
                    if fails_in_row >= 5:
                        print("🔄 إعادة تشغيل المتصفح")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        time.sleep(20)
                        browser, page = make_page(p)
                        fails_in_row = 0
                    continue
                fails_in_row = 0

                new_count = 0
                for href, text in items:
                    if not href or "/events/" not in href:
                        continue
                    key = event_key(href)
                    if not key or key in seen:
                        continue

                    if first_run:
                        seen.add(key)
                        # في وضع التجربة: نعرض شكل التغريدة لأول 3 مباريات من الأندية
                        if DRY_RUN and previews < 3 and (club_tags(key) or ALL_CLUBS):
                            previews += 1
                            post_to_x(build_tweet(parse_card(text), club_tags(key), full_url(href)))
                        continue

                    tags = club_tags(key)
                    if not tags and not ALL_CLUBS:
                        seen.add(key)  # مو من الأندية المطلوبة
                        continue

                    info = parse_card(text)
                    tweet = build_tweet(info, tags, full_url(href))
                    print(f"✨ مباراة جديدة: {info['title']}  ({key})")
                    if post_to_x(tweet):
                        seen.add(key)
                        new_count += 1
                    else:
                        attempts[key] = attempts.get(key, 0) + 1
                        if attempts[key] >= MAX_POST_ATTEMPTS:
                            print(f"🚫 تركت المباراة بعد {MAX_POST_ATTEMPTS} محاولات: {key}")
                            seen.add(key)

                if first_run or new_count:
                    save_seen(seen)

            # ما ننهي "أول تشغيل" إلا إذا الصفحة حمّلت فعلاً ولقينا مباريات
            if first_run and seen:
                first_run = False
                save_seen(seen)
                print(f"💾 تم حفظ {len(seen)} مباراة موجودة. من الحين أي جديد ينتشر فوراً.")

            took = time.time() - t0
            if loop % 20 == 1:
                print(f"🔁 فحص #{loop} خلص في {took:.1f} ث — محفوظ {len(seen)} مباراة")
            wait = max(1.0, POLL_SECONDS + random.uniform(-3, 3) - took)
            time.sleep(wait)

        save_seen(seen)
        try:
            browser.close()
        except Exception:
            pass
    print("🏁 انتهى وقت التشغيل، ملف التشغيل بيعيده")


if __name__ == "__main__":
    main()
