import os
import re
import csv
import time
import random
import requests
import json
import urllib.parse as up
import threading

import pandas as pd
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, InvalidSessionIdException


# ======================
# CONFIG
# ======================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

YEAR_START = 2019
YEAR_END = 2026

# 2015-2017 ke liye site alag "legacy" static page use karti hai
# (/static/foreign/html/newsletter/ms_overview/ms{year}/...),
# 2018+ ke liye normal /results/newsletter/{year} page.
# (YEAR_START=2019 hai to legacy branch abhi trigger nahi hoga, lekin agar
# kabhi range peeche badhani ho to ye already handle ho jayega.)
LEGACY_LAST_YEAR = 2017

# Sirf inhi categories ke events rakhne hain - baaki (Agility, BH, GHS,
# Gesamt, Körung, Mondioring, WB, wagera) skip. Filtering hamesha PURI
# extraction ke baad hoti hai (neeche scrape_year_events dekho) taake IGP/
# Ring ka koi bhi event kabhi miss na ho.
ALLOWED_CATEGORIES = {"igp", "ring"}

DATA_DIR = os.path.join(BASE_DIR, f"events_result_{YEAR_START}-{YEAR_END}")
os.makedirs(DATA_DIR, exist_ok=True)

CSV_FILE = os.path.join(DATA_DIR, "event_results.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "event_results.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "event_results.json")
FAILED_LOG_FILE = os.path.join(DATA_DIR, "failed.txt")   # jo years scrape nahi ho paate unki list

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "events")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

CHROME_MAIN_VERSION = 150   # <-- apne Chrome ka MAIN version yahan likho
                            # (chrome://settings/help mein dekho)

FIELDNAMES = ["Event UID", "Event Name", "Event URL", "Year", "Category"]


# ======================
# CAPTCHA MONITOR + PAUSE (wahi system jo dusre scrapers mein hai)
# ======================

def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    """
    Background monitor: detect karta hai iframe[src*='turnstile'].
    Agar iframe ajaye -> pause_event.set() AUR khud-b-khud 2captcha se
    solve karne ki koshish karta hai. Agar solve ho jaye -> resume.
    Agar na ho -> manually solve karna hoga; scraper tab tak agle year
    par move nahi hota.
    """
    def _run():
        last_seen = False
        while not stop_event.is_set():
            try:
                if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                    if not last_seen:
                        print("🔍 Turnstile CAPTCHA detect hua — scraper pause + auto-solve try kar rahe hain...")
                        pause_event.set()
                        last_seen = True
                        try:
                            handle_captcha(driver)
                        except Exception as e:
                            print("⚠️ Auto-solve mein masla (manually solve karna hoga):", e)

                    if not driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                        print("✅ CAPTCHA clear ho gaya — scraper resume ho raha hai.")
                        pause_event.clear()
                        last_seen = False
                else:
                    if last_seen:
                        print("✅ Turnstile iframe gone — resuming scraper.")
                        pause_event.clear()
                        last_seen = False
            except Exception as e:
                print("⚠️ CAPTCHA monitor error (ignored):", repr(e))
            time.sleep(poll_interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_if_paused(pause_event, check_interval=2):
    """Jab tak CAPTCHA active hai, yahin ruko - agle year par move nahi hote."""
    first = True
    while pause_event and pause_event.is_set():
        if first:
            print("⏸️ Verification active hai — wait kar rahe hain jab tak clear na ho...")
            first = False
        time.sleep(check_interval)


def nap(a=0.6, b=1.2):
    time.sleep(random.uniform(a, b))


# ======================
# CAPTCHA SOLVER (2captcha)
# ======================

def solve_turnstile(site_key, url, max_attempts=30, poll_interval=5):
    try:
        resp = requests.post(
            "http://2captcha.com/in.php",
            data={"key": CAPTCHA_KEY, "method": "turnstile", "sitekey": site_key, "pageurl": url, "json": 1},
            timeout=30,
        ).json()
        if resp.get("status") != 1:
            print("2captcha IN response:", resp)
            return None
        captcha_id = resp["request"]
        fetch_url = f"http://2captcha.com/res.php?key={CAPTCHA_KEY}&action=get&id={captcha_id}&json=1"
        for _ in range(max_attempts):
            time.sleep(poll_interval)
            result = requests.get(fetch_url, timeout=30).json()
            if result.get("status") == 1:
                return result["request"]
        return None
    except Exception as e:
        print("⚠️ solve_turnstile error:", e)
        return None


def find_turnstile_sitekey(driver):
    try:
        el = driver.find_element(By.CSS_SELECTOR, "[data-sitekey]")
        sk = el.get_attribute("data-sitekey")
        if sk:
            return sk
    except Exception:
        pass
    try:
        for f in driver.find_elements(By.TAG_NAME, "iframe"):
            src = f.get_attribute("src") or ""
            if "turnstile" in src:
                qs = up.parse_qs(up.urlparse(src).query)
                if "sitekey" in qs:
                    return qs["sitekey"][0]
    except Exception:
        pass
    try:
        m = re.search(r"sitekey['\":\s]*([A-Za-z0-9\-_]+)", driver.page_source, flags=re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def handle_captcha(driver):
    try:
        site_key = find_turnstile_sitekey(driver)
        if not site_key:
            print("❌ Could not find sitekey.")
            return
        print(f"🔐 Solving CAPTCHA for sitekey: {site_key}")
        token = solve_turnstile(site_key, driver.current_url)
        if not token:
            print("❌ CAPTCHA solving failed or no token returned.")
            return
        print("✅ CAPTCHA token received — injecting...")
        js = """
        function injectTurnstileToken(token) {
            function setToken() {
                try {
                    if (typeof turnstile !== 'undefined') {
                        var widgetEls = document.querySelectorAll('[data-sitekey]');
                        if (widgetEls.length > 0) {
                            widgetEls.forEach(function(el) {
                                var wid = turnstile.render(el, { sitekey: el.getAttribute('data-sitekey') });
                                if (turnstile.setResponse) { turnstile.setResponse(wid, token); }
                            });
                            return true;
                        }
                    }
                } catch(e) {}
                return false;
            }
            var ok = setToken();
            if (!ok) {
                var iv = setInterval(function(){ if (setToken()) clearInterval(iv); }, 1000);
            }
        }
        injectTurnstileToken(arguments[0]);
        """
        driver.execute_script(js, token)
        try:
            WebDriverWait(driver, 20).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='turnstile']"))
            )
            print("✅ CAPTCHA verification completed.")
        except Exception:
            print("⚠️ iframe still present — site may need API call or profile tweak.")
    except Exception as e:
        print("⚠️ handle_captcha error:", e)


# ======================
# DRIVER INIT (wahi setup jo dusre scrapers mein hai)
# ======================

def init_driver():
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    driver = uc.Chrome(options=options, use_subprocess=True, version_main=CHROME_MAIN_VERSION)
    driver.set_page_load_timeout(120)
    return driver


def safe_get(driver, url, retries=3):
    for i in range(retries):
        try:
            driver.get(url)
            return True
        except Exception as e:
            print(f"⚠️ Attempt {i+1} failed: {e}")
            nap(2, 3)
    return False


def wait_for_page_ready(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    nap(0.4, 0.7)


# ======================
# URL BUILDER (year ke hisaab se sahi template)
# ======================

def build_year_url(year):
    """
    2015-2017 -> legacy static "ms_overview" page
    2018+     -> normal /results/newsletter/{year} page
    """
    if year <= LEGACY_LAST_YEAR:
        return f"https://www.working-dog.com/static/foreign/html/newsletter/ms_overview/ms{year}/ms_overview_wd_en.html"
    return f"https://www.working-dog.com/results/newsletter/{year}"


# ======================
# EVENT EXTRACTION
# ======================

def extract_event_uid_from_url(url):
    """
    URL se stable unique id nikalne ki koshish:
      - .../results-category/<id>/slug          -> "cat-<id>"
      - .../results/...-<digits>                -> trailing digits
      - .../results/...<digits>                 -> trailing digits (no hyphen)
    Kuch nahi mile to "N/A" (dedupe tab URL par fallback karega).
    """
    if not url:
        return "N/A"

    m = re.search(r'/results-category/(\d+)/', url)
    if m:
        return f"cat-{m.group(1)}"

    m = re.search(r'-(\d+)/?$', url)
    if m:
        return m.group(1)

    m = re.search(r'(\d+)/?$', url)
    if m:
        return m.group(1)

    return "N/A"


def scrape_modern_year_events(driver, year):
    """
    2018+ pages ke liye scraper.

    Site ka structure:
        <div class="row padding-top border-bottom">      <-- ek category block
            <div class="col-xs-12">
                <a id="agility" href="#agility" class="menu">Agility</a>
            </div>
            <div class="col-xs-12">
                <table>...<a href="EVENT_URL" target="_blank">Event Name</a>...</table>
            </div>
        </div>

    NOTE: ye function HAMESHA saari categories ke saare events nikalta hai
    (koi category-level filtering yahan nahi hoti) - IGP/Ring filter
    scrape_year_events() mein extraction ke BAAD lagta hai, taake
    extraction hamesha complete rahe.
    """
    url = build_year_url(year)
    if not safe_get(driver, url):
        return None  # page load hi nahi hua - fail

    wait_for_page_ready(driver)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.content"))
        )
    except TimeoutException:
        pass  # page shayad load ho chuki hai, sirf is specific div ka intezar tha

    events = []
    try:
        blocks = driver.find_elements(By.CSS_SELECTOR, "div.row.padding-top.border-bottom")
    except Exception:
        blocks = []

    for block in blocks:
        try:
            cat_anchor = block.find_element(By.CSS_SELECTOR, "a[id]")
        except Exception:
            continue  # ye category-section wala block nahi hai (quick-nav bar)

        category_name = (cat_anchor.get_attribute("textContent") or "").strip()
        if not category_name:
            category_name = (cat_anchor.get_attribute("id") or "N/A").strip()

        event_links = block.find_elements(By.CSS_SELECTOR, "a[target='_blank']")
        for a in event_links:
            href = a.get_attribute("href")
            name = (a.get_attribute("textContent") or "").strip()
            if not href:
                continue
            events.append({
                "Event UID": extract_event_uid_from_url(href),
                "Event Name": name if name else "N/A",
                "Event URL": href,
                "Year": str(year),
                "Category": category_name,
            })

    return events


# JS jo legacy (2015-2017) page ke DOM ko walk karke events nikalta hai.
_LEGACY_EXTRACT_JS = r"""
var navLinks = document.querySelectorAll("a[href^='#']");
var categoryIds = new Set();
navLinks.forEach(function(a){
    var href = a.getAttribute('href') || '';
    if (href.length > 1) categoryIds.add(href.substring(1).toLowerCase());
});

var results = [];
var currentCategory = null;

function walk(node) {
    if (node.nodeType === 1) {
        var id = (node.id || '').toLowerCase();
        if (id && categoryIds.has(id)) {
            currentCategory = (node.textContent || id).trim() || id;
        }
        if (node.tagName === 'A') {
            var href = node.getAttribute('href');
            if (href && (href.indexOf('/results/') !== -1 || href.indexOf('/results-category/') !== -1)) {
                results.push({
                    category: currentCategory || 'N/A',
                    name: (node.textContent || '').trim(),
                    href: node.href
                });
            }
        }
        for (var i = 0; i < node.childNodes.length; i++) {
            walk(node.childNodes[i]);
        }
    }
}
walk(document.body);
return results;
"""


def scrape_legacy_year_events(driver, year):
    """2015-2017 ke liye alag static page + alag DOM structure."""
    url = build_year_url(year)
    if not safe_get(driver, url):
        return None

    wait_for_page_ready(driver)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href^='#']"))
        )
    except TimeoutException:
        pass

    try:
        raw = driver.execute_script(_LEGACY_EXTRACT_JS)
    except Exception as e:
        print("⚠️ Legacy extraction JS error:", e)
        return None

    events = []
    for item in (raw or []):
        href = item.get("href")
        if not href:
            continue
        events.append({
            "Event UID": extract_event_uid_from_url(href),
            "Event Name": item.get("name") or "N/A",
            "Event URL": href,
            "Year": str(year),
            "Category": item.get("category") or "N/A",
        })

    return events


def scrape_year_events(driver, year):
    """
    Year ke hisaab se legacy ya modern extractor par dispatch karta hai,
    phir sirf ALLOWED_CATEGORIES (IGP, Ring) wale events rakhta hai.

    IMPORTANT: filtering hamesha PURI extraction ke BAAD hoti hai - matlab
    extractor pehle us page ke SAARE categories ke SAARE events nikal leta
    hai (bilkul comprehensive), aur sirf us complete list mein se IGP/Ring
    wale rakhe jate hain. Isse guarantee milti hai ke IGP/Ring ka koi bhi
    event kabhi miss nahi hoga - filtering ki wajah se extraction
    incomplete nahi hoti.
    """
    if year <= LEGACY_LAST_YEAR:
        events = scrape_legacy_year_events(driver, year)
    else:
        events = scrape_modern_year_events(driver, year)

    if events is None:
        return None

    all_count = len(events)
    filtered = [
        ev for ev in events
        if (ev.get("Category") or "").strip().lower() in ALLOWED_CATEGORIES
    ]
    print(f"   🔎 Total {all_count} events mile (sab categories) - inme se {len(filtered)} IGP/Ring ke hain.")
    return filtered


# ======================
# SAVE / LOAD
# ======================

def make_dedupe_key(event):
    """
    Dedupe key = Year + (Event UID ya URL).

    Year isliye shamil hai kyunke kai links (khaas-taur par legacy
    /results-category/... pages) ek "recurring series" ki taraf point
    karte hain, jo har saal ke newsletter mein dobara list hoti hai.
    Sirf UID/URL par dedupe karne se dusre saal ki legit listing bhi
    "duplicate" samajh kar skip ho jati thi. Same year ke andar agar
    wahi UID/URL dobara mile (site khud kabhi kabhi ek row do dafa
    deti hai), tab hi wo asli duplicate hai.
    """
    key = event.get("Event UID")
    if not key or key == "N/A":
        key = event.get("Event URL")
    if not key:
        return None
    year = event.get("Year") or "N/A"
    return f"{year}::{key}"


def load_existing_data():
    all_events = []
    by_key = {}
    if os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for k in FIELDNAMES:
                        if k not in row:
                            row[k] = "N/A"
                    all_events.append(row)
                    key = make_dedupe_key(row)
                    if key:
                        by_key[key] = row
        except Exception:
            pass
    return all_events, by_key


def save_results(all_events):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in all_events:
            writer.writerow(r)

    try:
        df = pd.DataFrame(all_events)
        df.to_excel(EXCEL_FILE, index=False)
    except Exception as e:
        print("⚠️ Error writing Excel:", e)

    with open(JSON_FILE, "w", encoding="utf-8") as jf:
        json.dump(all_events, jf, ensure_ascii=False, indent=2)


def append_event(all_events, by_key, new_event):
    """Duplicate-safe insert - key = Year + (Event UID ya URL), see make_dedupe_key()."""
    key = make_dedupe_key(new_event)
    if not key:
        return False
    if key in by_key:
        return False
    all_events.append(new_event)
    by_key[key] = new_event
    return True


def log_failed_year(year, reason="Unknown error"):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(FAILED_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] Year {year} | Reason: {reason}\n")
        print(f"📝 Year {year} failed.txt mein likh diya gaya.")
    except Exception as e:
        print("⚠️ failed.txt likhne mein masla:", e)


# ======================
# MAIN
# ======================

def main():
    driver = init_driver()

    pause_event = threading.Event()
    stop_event = threading.Event()
    persistent_captcha_monitor(driver, pause_event, stop_event)

    all_events, by_key = load_existing_data()
    print(f"ℹ️ Pehle se {len(all_events)} events ka data mojood hai.")
    print(f"📂 Output folder: {DATA_DIR}")
    print(f"🎯 Sirf categories: {sorted(ALLOWED_CATEGORIES)}")

    try:
        for year in range(YEAR_START, YEAR_END + 1):
            wait_if_paused(pause_event)
            print(f"\n📅 Year {year} scrape ho raha hai...")

            events = None
            last_error = "Unknown error"
            for attempt in range(1, 4):
                wait_if_paused(pause_event)
                try:
                    events = scrape_year_events(driver, year)
                except (InvalidSessionIdException, WebDriverException) as e:
                    print(f"💥 Browser session crash ho gaya: {e}")
                    print("💾 Ab tak ka data save kar rahe hain aur naya browser session start kar rahe hain...")
                    save_results(all_events)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    stop_event.set()
                    driver = init_driver()
                    stop_event = threading.Event()
                    persistent_captcha_monitor(driver, pause_event, stop_event)
                    events = None
                    last_error = str(e)
                except Exception as e:
                    last_error = str(e)
                    events = None

                if events is not None:
                    break
                print(f"⚠️ Attempt {attempt}/3 failed for year {year}: {last_error}")
                wait_if_paused(pause_event)
                nap(1.5, 2.5)

            if events is None:
                print(f"⚠️ Year {year} scrape nahi ho saka - failed.txt mein add kar rahe hain.")
                log_failed_year(year, last_error)
                continue

            new_count = 0
            for ev in events:
                if append_event(all_events, by_key, ev):
                    new_count += 1

            print(f"✅ Year {year}: {len(events)} IGP/Ring events mile ({new_count} naye, total ab {len(all_events)}).")
            save_results(all_events)   # har year ke baad turant save - data safe rehta hai
            nap(0.5, 1.0)

        print(f"\n🎯 Sab years ({YEAR_START}-{YEAR_END}) complete ho gaye. Total events: {len(all_events)}")
        print(f"💾 Files: \n  {CSV_FILE}\n  {EXCEL_FILE}\n  {JSON_FILE}")

    finally:
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()