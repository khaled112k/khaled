import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CACHE_FILE = "seen_events.json"

TARGET_URL = "https://webook.com/ar/explore?tag=football"
TARGET_CLUBS = ["الاتحاد", "الهلال", "النصر", "الاهلي", "الأهلي", "القادسية"]

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ خطأ: لم يتم العثور على TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload)
        print(f"📲 حالة إرسال تليجرام: {res.status_code}")
    except Exception as e:
        print(f"❌ فشل إرسال التليجرام: {e}")

def load_seen_events():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen_events(seen_events):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_events), f, ensure_ascii=False, indent=2)

def run_single_check(page, seen_events):
    new_found = 0
    try:
        print(f"🔍 جاري إعادة تحميل وفحص الصفحة...")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        
        links = page.query_selector_all("a")
        print(f"📊 إجمالي الروابط بالصفحة: {len(links)}")
        
        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            
            if any(path in href for path in ['/e/', '/events/', '/matches/', '/sports/']):
                full_url = href if href.startswith("http") else f"https://webook.com{href}"
                event_id = full_url.split("?")[0]
                
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    title = link.inner_text().strip().replace("\n", " ")
                    display_name = title if title else "مباراة / فعالية كرة قدم جديدة"
                    
                    if any(club in display_name for club in TARGET_CLUBS):
                        new_found += 1
                        msg = f"⚽ {display_name}\n🔗 <b>الرابط:</b> {full_url}"
                        print(f"✨ تم كشف مباراة لأحد الأندية المستهدفة: {display_name}")
                        send_telegram_message(msg)
                        
    except Exception as e:
        print(f"❌ خطأ أثناء جولة الفحص: {e}")
        
    return new_found

def run_monitor():
    seen_events = load_seen_events()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        
        # تنفيذ 10 محولات فحص متتالية في نفس الجلسة بفاصل عشوائي تقريبي (30 ثانية)
        for i in range(1, 11):
            print(f"\n--- بدء محاولة الفحص رقم ({i}/10) ---")
            run_single_check(page, seen_events)
            save_seen_events(seen_events)
            
            if i < 10:
                sleep_time = random.randint(25, 40)
                print(f"⏳ انتظار عشوائي لمدة {sleep_time} ثانية...")
                time.sleep(sleep_time)
                
        browser.close()

if __name__ == "__main__":
    run_monitor()
