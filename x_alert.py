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
  TARGET_URLS   روابط الصفحات مفصولة بفاصلة (افتراضي: صفحة كرة القدم + صفحة الاستكشاف، أول 7 بكل وحدة)
  POLL_SECONDS  الفاصل بين كل فحص بالثواني (افتراضي 12)
  RUN_MINUTES   مدة التشغيل قبل ما يطلع (افتراضي 340 = 5 ساعات و40 دقيقة)
  INCLUDE_LINK  true يحط رابط ويبوك بالتغريدة (أغلى: ~0.20$ بدل ~0.015$)
  ALL_CLUBS     true ينشر كل المباريات مو بس الأندية المحددة
  DRY_RUN       true يطبع التغريدة بدون ما ينشرها (للتجربة)
  VIP_BOT_TOKEN توكن بوت VIP — إذا موجود يرسل التنبيه لقناة VIP أول
  VIP_CHAT_ID   رقم قناة VIP (افتراضي -1004418967399)
  VIP_HEAD_START كم ثانية تسبق VIP تغريدة X (افتراضي 90)
  VIP_CATEGORIES true يرسل لـ VIP رسالة ثانية فيها كل الفئات وأسعارها
  WATCH_URLS    روابط مباريات نراقب دفعاتها (زر احجز رجع / فئة جديدة) — التنبيه لـ VIP بس
  WATCH_EVERY   كل كم ثانية نفحص صفحات المراقبة (افتراضي 60)
  EXCLUDE_WORDS كلمات تستثني البطولة إذا ظهرت بالرابط أو العنوان (افتراضي: تحت 21 والنسائي)
"""

import os
import re
import json
import time
import random
import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# ---------------- الإعدادات ----------------
TARGET_URLS = [u.strip() for u in os.getenv(
    "TARGET_URLS", "https://webook.com/ar/explore?tag=football,https://webook.com/ar/explore").split(",") if u.strip()]
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "12"))
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "340"))
INCLUDE_LINK = os.getenv("INCLUDE_LINK", "false").lower() == "true"
ALL_CLUBS = os.getenv("ALL_CLUBS", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

CACHE_FILE = "x_seen_events.json"
USD_TO_SAR = 3.75  # سعر الربط الرسمي
MAX_POST_ATTEMPTS = 3

VIP_BOT_TOKEN = os.getenv("VIP_BOT_TOKEN", "").strip()
VIP_CHAT_ID = os.getenv("VIP_CHAT_ID", "-1004418967399").strip()
VIP_HEAD_START = float(os.getenv("VIP_HEAD_START", "90"))
VIP_CATEGORIES = os.getenv("VIP_CATEGORIES", "true").lower() == "true"

# ======== مراقبة دفعات التذاكر ========
# حط روابط صفحات المباريات اللي تبي تراقب دفعاتها (مفصولة بفاصلة). غيّرها متى ما بغيت.
WATCH_URLS = [u.strip() for u in os.getenv(
    "WATCH_URLS",
    "https://webook.com/ar/sa/jed/sports-event/events/rsl-26-27-the-jeddah-derby-ittihad-vs-ahli",
).split(",") if u.strip()]
WATCH_EVERY = float(os.getenv("WATCH_EVERY", "60"))  # كل كم ثانية يفحص صفحات المراقبة
WATCH_FILE = "watch_state.json"
# =====================================

# الأندية: كلمات تظهر في رابط المباراة  ->  الوسم
# نطابق الاسم ككلمة كاملة بين الشرطات، بـ al- أو بدونها (مثال: ittihad-vs-ahli)
CLUBS = [
    (["hilal"], "#الهلال"),
    (["nassr", "nasr"], "#النصر"),
    (["ittihad", "itihad"], "#الاتحاد"),
    (["ahli"], "#الأهلي"),
    (["qadsiah", "qadisiyah", "qadsia", "qadisiya"], "#القادسية"),
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
            # السعر: أول رقم بعد "يبدأ من" (أحياناً يجي قبله رمز العملة لحاله)
            # إذا السعر بالدولار (سيرفرات GitHub بأمريكا) نحوّله للريال: الريال مربوط بالدولار 3.75
            usd = False
            for cand in [p] + parts[i + 1:i + 4]:
                if "$" in cand or "USD" in cand.upper():
                    usd = True
                m = re.search(r"\d[\d,٬]*(?:\.\d+)?", cand.replace("يبدأ من", ""))
                if m:
                    num = m.group(0).replace(",", "").replace("٬", "")
                    try:
                        val = float(num)
                        if usd:
                            val *= USD_TO_SAR
                        info["price"] = f"{round(val):,}"
                    except ValueError:
                        info["price"] = ""
                    break

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


# بطولات ما نبي لها تنبيه: دوري جوّي للنخبة تحت 21 والدوري النسائي
EXCLUDE_WORDS = [w.strip().lower() for w in os.getenv(
    "EXCLUDE_WORDS",
    "u21,u-21,under-21,jawwy,elite-league,تحت 21,جوي,women,womens,ladies,wsl,سيدات,نسائي",
).split(",") if w.strip()]


def is_excluded(key, text):
    blob = (key + " " + (text or "")).lower()
    blob = re.sub(r"[\u064B-\u0652\u0640]", "", blob)  # نشيل التشكيل: جوّي -> جوي
    return any(w in blob for w in EXCLUDE_WORDS)


def club_tags(key):
    tags = []
    for variants, tag in CLUBS:
        if any(re.search(rf"(^|-){v}(-|$)", key) for v in variants):
            tags.append(tag)
    return tags


# ---------------- التغريدة ----------------
# الديربيات: تصميم مميز للتغريدة فور نزول التذاكر
DERBIES = [
    ({"#الاتحاد", "#الأهلي"}, "ديربي جدة", "#ديربي_جدة"),
    ({"#الهلال", "#النصر"}, "ديربي الرياض", "#ديربي_الرياض"),
]


def derby_of(tags):
    for clubs, name, tag in DERBIES:
        if clubs <= set(tags):
            return name, tag
    return None


def build_tweet(info, tags, url):
    title = info["title"] or "مباراة جديدة"
    if len(title) > 110:
        title = title[:107] + "..."

    derby = derby_of(tags)
    if derby:
        lines = [f"🔥🔥 {derby[0]} 🔥🔥\n🚨 نزلت تذاكر الديربي | {title}"]
    else:
        lines = [f"🚨 نزلت تذاكر | {title}"]
    when = " ".join(x for x in (info["date"], info["time"]) if x)
    if when:
        lines.append(f"📅 {when}")
    if info["city"]:
        lines.append(f"📍 {info['city']}")
    if info["price"]:
        lines.append(f"💰 تبدأ من {info['price']} ريال")
    lines.append("⚡ الحق قبل لا تخلص! متاحة الآن في ويبوك" if derby else "⚡ متاحة الآن في ويبوك")
    if INCLUDE_LINK:
        lines.append(f"🔗 {url}")

    hashtags = " ".join(([derby[1]] if derby else []) + tags + ["#دوري_روشن", "#تذاكر"])
    # سطر فاضي بين كل صف وصف
    text = "\n\n".join(lines) + "\n\n" + hashtags

    # X يحسب الرابط 23 حرف مهما كان طوله، والإيموجي حرفين
    def x_len(t):
        if INCLUDE_LINK and url in t:
            t = t.replace(url, "x" * 23)
        return sum(2 if ord(c) > 0xFFFF else 1 for c in t)

    if x_len(text) > 280:
        lines = [l for l in lines if not l.startswith("⚡")]
        text = "\n\n".join(lines) + "\n\n" + hashtags
    return text


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
# ---------------- قناة VIP في تيليجرام ----------------
def head_start():
    return VIP_HEAD_START if VIP_BOT_TOKEN else 0


def vip_text(tweet, url):
    text = "⚡ VIP — " + tweet
    if url and url not in text:
        text += "\n\n" + url
    return text


def send_vip(text, reply_to=None):
    """يرسل رسالة لقناة VIP. يرجع رقم الرسالة إذا وصلت."""
    if not VIP_BOT_TOKEN:
        return None
    if DRY_RUN:
        print("🧪 [VIP]\n" + text)
        return 1
    payload = {"chat_id": VIP_CHAT_ID, "text": text}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{VIP_BOT_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.load(r)
                if res.get("ok"):
                    print("📣 وصلت الرسالة لقناة VIP")
                    return res["result"]["message_id"]
        except Exception as e:
            print(f"⚠️ VIP محاولة {i + 1}: {e}")
        time.sleep(2)
    return None


def flush_pending(pending):
    """ينشر في X التنبيهات اللي خلص وقت سبقها لـ VIP."""
    left = []
    for item in pending:
        due, key, tweet, tries = item
        if time.time() < due:
            left.append(item)
            continue
        if post_to_x(tweet):
            continue
        tries += 1
        if tries < MAX_POST_ATTEMPTS:
            left.append([time.time() + 30, key, tweet, tries])
        else:
            print(f"🚫 تركت تغريدة المباراة بعد {MAX_POST_ATTEMPTS} محاولات: {key}")
    return left


# يقرأ الفئات من صفحة المباراة: من نافذة "استكشف المزيد" إذا انفتحت، وإلا من قسم "أنواع التذاكر"
CATS_JS = """
() => {
  let scope = document.querySelector('[role=dialog]');
  if (!scope || !scope.querySelector('h3')) {
    const hd = [...document.querySelectorAll('h2')].find(e => /أنواع التذاكر|Ticket types/i.test(e.innerText));
    scope = hd;
    for (let i = 0; scope && !scope.querySelector('h3') && i < 6; i++) scope = scope.parentElement;
  }
  if (!scope) return [];
  return [...scope.querySelectorAll('h3')].map(h => {
    let row = h;
    for (let i = 0; i < 6 && row && !/\\d/.test(row.innerText.replace(h.innerText, '')); i++) row = row.parentElement;
    if (!row) return [h.innerText.trim(), '', false];
    const sar = !!row.querySelector('svg[viewBox^="0 0 1124"]');
    return [h.innerText.trim(), row.innerText.replace(h.innerText, '').trim(), sar];
  });
}
"""


def fetch_categories(page, url):
    """يفتح صفحة المباراة ويرجع [(الفئة, السعر بالريال)] مرتبة من الأرخص."""
    det = None
    try:
        det = page.context.new_page()
        det.goto(url, wait_until="domcontentloaded", timeout=20000)
        det.get_by_text(re.compile("أنواع التذاكر|Ticket types", re.I)).first.wait_for(timeout=15000)
        more = det.locator("button", has_text=re.compile("استكشف المزيد|Explore more|See more|View more", re.I))
        if more.count():
            more.first.click(timeout=5000)
            det.wait_for_selector("[role=dialog] h3", timeout=5000)
        rows = det.evaluate(CATS_JS)
    except Exception as e:
        print(f"⚠️ ما قدرت أقرأ الفئات: {e}")
        return []
    finally:
        if det:
            try:
                det.close()
            except Exception:
                pass
    cats = []
    for name, raw, sar in rows:
        nums = re.findall(r"\d[\d,٬]*(?:\.\d+)?", raw or "")
        if not name or not nums:
            continue
        try:
            val = float(nums[-1].replace(",", "").replace("٬", ""))
        except ValueError:
            continue
        if "$" in raw or "USD" in raw.upper() or not sar:
            val *= USD_TO_SAR  # سيرفرات GitHub بأمريكا تعرض بالدولار
        cats.append((name, round(val)))
    cats.sort(key=lambda c: c[1])
    print(f"🎟️ لقيت {len(cats)} فئة")
    return cats


def categories_text(cats):
    return "🎟️ الفئات والأسعار:\n" + "\n".join(f"• {n} — {p:,} ريال" for n, p in cats)


# ---------------- مراقبة الدفعات ----------------
# صفحة المباراة العامة (بدون تسجيل دخول): زر "احجز" يعني فيه تذاكر، "أبلغني" يعني خالصة
SNAP_JS = """
() => {
  const h1 = document.querySelector('h1');
  const els = [...document.querySelectorAll('a,button')].map(e => [e.innerText.trim(), e.getAttribute('href') || '']);
  return {
    title: h1 ? h1.innerText.trim() : '',
    book: els.some(([t, h]) => /^(احجز|Book)$/i.test(t) && h.includes('/book')),
    notify: els.some(([t]) => /^(أبلغني|Notify me)$/i.test(t)),
  };
}
"""


def parse_cat_rows(rows):
    cats = []
    for name, raw, sar in rows:
        nums = re.findall(r"\d[\d,٬]*(?:\.\d+)?", raw or "")
        if not name or not nums:
            continue
        try:
            val = float(nums[-1].replace(",", "").replace("٬", ""))
        except ValueError:
            continue
        if "$" in raw or "USD" in raw.upper() or not sar:
            val *= USD_TO_SAR
        cats.append([name, round(val)])
    cats.sort(key=lambda c: c[1])
    return cats


def fetch_snapshot(page, url):
    """يقرأ صفحة المباراة: هل الحجز متاح + الفئات وأسعارها. يرجع None إذا ما حمّلت صح."""
    det = None
    info, rows = {}, []
    try:
        det = page.context.new_page()
        det.goto(url, wait_until="domcontentloaded", timeout=25000)
        det.wait_for_selector("h1", timeout=15000)
        try:
            det.get_by_text(re.compile("أنواع التذاكر|Ticket types|أبلغني", re.I)).first.wait_for(timeout=8000)
        except Exception:
            pass
        info = det.evaluate(SNAP_JS)
        if det.get_by_text(re.compile("أنواع التذاكر|Ticket types", re.I)).count():
            more = det.locator("button", has_text=re.compile("استكشف المزيد|Explore more|See more|View more", re.I))
            if more.count():
                more.first.click(timeout=5000)
                det.wait_for_selector("[role=dialog] h3", timeout=5000)
            rows = det.evaluate(CATS_JS)
    except Exception as e:
        print(f"⚠️ مراقبة الدفعات {url}: {e}")
        return None
    finally:
        if det:
            try:
                det.close()
            except Exception:
                pass
    if not info.get("title") or not (info.get("book") or info.get("notify") or rows):
        return None
    return {"title": info["title"], "bookable": bool(info["book"]) and not info["notify"],
            "cats": parse_cat_rows(rows)}


def load_watch():
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_watch(state):
    try:
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ حفظ المراقبة: {e}")


def batch_text(snap, url, new_cats, reopened):
    derby = derby_of(club_tags(event_key(url)))
    head = f"🔥🔥 {derby[0]} — نزلت دفعة جديدة 🔥🔥" if derby else "🔥 نزلت دفعة تذاكر جديدة"
    lines = [head, f"🏟️ {snap['title']}"]
    if new_cats:
        lines.append("🆕 فئات نزلت:\n" + "\n".join(f"• {n} — {p:,} ريال" for n, p in new_cats))
    elif reopened:
        lines.append("✅ التذاكر رجعت متاحة للحجز")
    if snap["cats"]:
        lines.append(categories_text(snap["cats"]))
    lines.append("⚡ الحق قبل لا تخلص!")
    lines.append(f"🔗 {url}")
    return "\n\n".join(lines)


def watch_check(page, state):
    for url in WATCH_URLS:
        snap = fetch_snapshot(page, url)
        if not snap:
            continue
        prev = state.get(url)
        if prev and prev.get("cats") and not snap["cats"] and snap["bookable"] == prev.get("bookable"):
            continue  # غالباً الفئات ما حمّلت هالمرة، نتجاهل القراءة
        state[url] = snap
        save_watch(state)
        if not prev:
            print(f"👀 بدأت أراقب الدفعات: {snap['title']} — {len(snap['cats'])} فئة، "
                  f"الحجز {'متاح' if snap['bookable'] else 'مقفل'}")
            continue
        old = {n for n, _ in prev.get("cats", [])}
        new_cats = [c for c in snap["cats"] if c[0] not in old]
        reopened = snap["bookable"] and not prev.get("bookable")
        if new_cats or reopened:
            print(f"🔥 دفعة جديدة: {snap['title']} — فئات جديدة: {len(new_cats)}، رجع الحجز: {reopened}")
            send_vip(batch_text(snap, url, new_cats, reopened))


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
    # للتجربة: نحذف مباريات من المحفوظ عشان يغرد فيها كأنها جديدة (FORGET_KEYS=al-ahli-vs-al-taawoun)
    forget = [k.strip().lower() for k in os.getenv("FORGET_KEYS", "").split(",") if k.strip()]
    if seen and forget:
        removed = {k for k in seen if any(f in k for f in forget)}
        seen -= removed
        print(f"🧹 حذفت من المحفوظ: {sorted(removed) or 'ولا شي (ما لقيت تطابق)'}")
    first_run = seen is None
    if first_run:
        seen = set()
        print("🆕 أول تشغيل: بحفظ المباريات الموجودة بدون نشر")

    attempts = {}  # key -> عدد محاولات النشر الفاشلة
    pending = []  # [موعد النشر في X, key, التغريدة, عدد المحاولات]
    watch_state = load_watch()
    next_watch = 0
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
                    if is_excluded(key, text):
                        seen.add(key)  # بطولة مستثناة (تحت 21 / نسائية)
                        print(f"⏭️ تجاهلت بطولة مستثناة: {key}")
                        continue

                    info = parse_card(text)
                    tweet = build_tweet(info, tags, full_url(href))
                    print(f"✨ مباراة جديدة: {info['title']}  ({key})")
                    # VIP أول، وبعد VIP_HEAD_START ثانية ينشر في X
                    url_full = full_url(href)
                    vip_id = send_vip(vip_text(tweet, url_full))
                    if vip_id and VIP_CATEGORIES:
                        cats = fetch_categories(page, url_full)
                        if cats:
                            send_vip(categories_text(cats), reply_to=vip_id)
                    seen.add(key)
                    new_count += 1
                    pending.append([time.time() + head_start(), key, tweet, 0])

                if first_run or new_count:
                    save_seen(seen)

            # ما ننهي "أول تشغيل" إلا إذا الصفحة حمّلت فعلاً ولقينا مباريات
            if first_run and seen:
                first_run = False
                save_seen(seen)
                print(f"💾 تم حفظ {len(seen)} مباراة موجودة. من الحين أي جديد ينتشر فوراً.")

            pending = flush_pending(pending)
            if WATCH_URLS and time.time() >= next_watch:
                watch_check(page, watch_state)
                next_watch = time.time() + WATCH_EVERY

            took = time.time() - t0
            if loop % 20 == 1:
                print(f"🔁 فحص #{loop} خلص في {took:.1f} ث — محفوظ {len(seen)} مباراة")
            wait = max(1.0, POLL_SECONDS + random.uniform(-3, 3) - took)
            time.sleep(wait)

        # ننشر الباقي في X قبل ما نطلع
        end_by = time.time() + head_start() + 120
        while pending and time.time() < end_by:
            time.sleep(5)
            pending = flush_pending(pending)
        save_seen(seen)
        try:
            browser.close()
        except Exception:
            pass
    print("🏁 انتهى وقت التشغيل، ملف التشغيل بيعيده")


if __name__ == "__main__":
    main()
