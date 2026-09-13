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

# Phase 1 ka output (events_result_scraper.py se aya hua) - isi se URLs uthayenge
INPUT_DIR = os.path.join(BASE_DIR, "events_result_2019-2026")
INPUT_CSV = os.path.join(INPUT_DIR, "event_results.csv")

# Phase 2 ka apna output folder
DATA_DIR = os.path.join(BASE_DIR, "results_events_detail_2019-2026")
os.makedirs(DATA_DIR, exist_ok=True)

CSV_FILE = os.path.join(DATA_DIR, "results_events_detail.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "results_events_detail.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "results_events_detail.json")
FAILED_LOG_FILE = os.path.join(DATA_DIR, "failed.txt")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "event_details")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

CHROME_MAIN_VERSION = 150   # <-- apne Chrome ka MAIN version yahan likho

# CSV/Excel ke liye flat columns.
CSV_FIELDNAMES = [
    "Event UID", "Event Name", "Event URL", "Year", "Discipline",
    "Category of Event", "Results List Name", "Date", "Event Location",
    "Judges B", "Judges Total", "Judges C", "Helpers C", "Helpers Total",
    "Extra Fields", "Results Count", "Results",
]

# Page ke dt label (lowercase, trimmed) -> hamare column ka naam
LABEL_MAP = {
    "category of event": "Category of Event",
    "results list name": "Results List Name",
    "date": "Date",
    "event location": "Event Location",
    "judges b": "Judges B",
    "judges total": "Judges Total",
    "judges c": "Judges C",
    "helpers c": "Helpers C",
    "helpers total": "Helpers Total",
}
MULTI_JOIN = " | "   # multiple names (jaise 2 Helpers C) is se join honge CSV/Excel mein

# Update-mode ke liye: in fields mein se agar SAB "N/A"/khaali hon, ya
# Results Count 0 ho, to us event ko "incomplete" maana jata hai.
IMPORTANT_HEADER_FIELDS = ["Category of Event", "Results List Name", "Date", "Event Location"]


# ======================
# CAPTCHA MONITOR + PAUSE (phase 1 wala hi system)
# ======================

def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
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
    first = True
    while pause_event and pause_event.is_set():
        if first:
            print("⏸️ Verification active hai — wait kar rahe hain jab tak clear na ho...")
            first = False
        time.sleep(check_interval)


def nap(a=0.6, b=1.2):
    time.sleep(random.uniform(a, b))


# ======================
# CAPTCHA SOLVER (2captcha) - phase 1 wala hi
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
# DRIVER INIT
# ======================

REAL_USER_DATA_DIR = None
REAL_PROFILE_NAME = "Default"


def init_driver(use_real_profile=False, real_user_data_dir=None, profile_name="Default"):
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    if use_real_profile and real_user_data_dir:
        options.add_argument(f"--user-data-dir={real_user_data_dir}")
        options.add_argument(f"--profile-directory={profile_name}")
    else:
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
# DETAIL PAGE EXTRACTION (header dt/dd + results list)
# ======================

_DETAIL_EXTRACT_JS = r"""
var out = {header: {}, results: []};

var dl = document.querySelector('dl.datasheet');
if (dl) {
    var children = Array.prototype.slice.call(dl.children);
    var lastLabel = null;
    children.forEach(function(el){
        if (el.tagName === 'DT') {
            lastLabel = (el.textContent || '').trim().replace(/:\s*$/, '');
        } else if (el.tagName === 'DD' && lastLabel) {
            var nobrEls = el.querySelectorAll('.nobr');
            var values = [];
            if (nobrEls.length > 0) {
                nobrEls.forEach(function(n){
                    var a = n.querySelector('a');
                    var txt = a ? a.textContent : n.textContent;
                    txt = (txt || '').trim();
                    if (txt) values.push(txt);
                });
            } else {
                var txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
                if (txt) values.push(txt);
            }
            if (!out.header[lastLabel]) { out.header[lastLabel] = []; }
            out.header[lastLabel] = out.header[lastLabel].concat(values);
            lastLabel = null;
        }
    });
}

var rows = document.querySelectorAll('#resultList > div.row');
rows.forEach(function(row){
    var dogUrl = '', dogName = '';
    var anchors = row.querySelectorAll('a[href*="/dogs-details/"]');
    for (var i = 0; i < anchors.length; i++) {
        var txt = (anchors[i].textContent || '').trim();
        if (txt) {
            dogName = txt;
            dogUrl = anchors[i].getAttribute('href') || '';
            break;
        }
    }
    if (!dogUrl && anchors.length > 0) {
        dogUrl = anchors[0].getAttribute('href') || '';
    }
    out.results.push({
        dog_name: dogName || 'N/A',
        dog_url: dogUrl || 'N/A'
    });
});

return out;
"""


def extract_event_detail(driver):
    return driver.execute_script(_DETAIL_EXTRACT_JS)


def build_records(base_event, raw):
    """
    base_event needs: Event UID, Event Name, Event URL, Year, Category
    (Category = Discipline, IGP/Ring, matching phase 1's column name)
    """
    header = raw.get("header", {}) or {}
    results = raw.get("results", []) or []

    known_flat = {}
    known_nested = {}
    extra_fields = {}

    for label, values in header.items():
        norm = label.strip().lower()
        if norm == "event":
            continue
        if norm in LABEL_MAP:
            col = LABEL_MAP[norm]
            known_flat[col] = MULTI_JOIN.join(values) if values else "N/A"
            known_nested[col] = values if values else []
        else:
            extra_fields[label] = values

    flat_row = {
        "Event UID": base_event.get("Event UID", "N/A"),
        "Event Name": base_event.get("Event Name", "N/A"),
        "Event URL": base_event.get("Event URL", "N/A"),
        "Year": base_event.get("Year", "N/A"),
        "Discipline": base_event.get("Category", "N/A"),
        "Category of Event": known_flat.get("Category of Event", "N/A"),
        "Results List Name": known_flat.get("Results List Name", "N/A"),
        "Date": known_flat.get("Date", "N/A"),
        "Event Location": known_flat.get("Event Location", "N/A"),
        "Judges B": known_flat.get("Judges B", "N/A"),
        "Judges Total": known_flat.get("Judges Total", "N/A"),
        "Judges C": known_flat.get("Judges C", "N/A"),
        "Helpers C": known_flat.get("Helpers C", "N/A"),
        "Helpers Total": known_flat.get("Helpers Total", "N/A"),
        "Extra Fields": json.dumps(extra_fields, ensure_ascii=False) if extra_fields else "",
        "Results Count": len(results),
        "Results": json.dumps(results, ensure_ascii=False),
    }

    nested_row = {
        "Event UID": base_event.get("Event UID", "N/A"),
        "Event Name": base_event.get("Event Name", "N/A"),
        "Event URL": base_event.get("Event URL", "N/A"),
        "Year": base_event.get("Year", "N/A"),
        "Discipline": base_event.get("Category", "N/A"),
        "Category of Event": known_nested.get("Category of Event", []),
        "Results List Name": known_nested.get("Results List Name", []),
        "Date": known_nested.get("Date", []),
        "Event Location": known_nested.get("Event Location", []),
        "Judges B": known_nested.get("Judges B", []),
        "Judges Total": known_nested.get("Judges Total", []),
        "Judges C": known_nested.get("Judges C", []),
        "Helpers C": known_nested.get("Helpers C", []),
        "Helpers Total": known_nested.get("Helpers Total", []),
        "Extra Fields": extra_fields,
        "Results Count": len(results),
        "Results": results,
    }

    return flat_row, nested_row


# ======================
# LOAD INPUT (Phase 1 output)
# ======================

def load_all_input_events():
    if not os.path.exists(INPUT_CSV):
        raise FileNotFoundError(f"Phase 1 ka input file nahi mila: {INPUT_CSV}")

    all_rows = []
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            all_rows.append(row)
    return all_rows


def index_input_events(all_events):
    """Phase 1 events ko Event UID aur Event URL dono se lookup karne ke liye index banata hai."""
    by_uid = {}
    by_url = {}
    for row in all_events:
        uid = row.get("Event UID")
        url = row.get("Event URL")
        if uid and uid != "N/A":
            by_uid[uid] = row
        if url:
            by_url[url] = row
    return by_uid, by_url


def prompt_row_range(total_rows):
    print(f"📊 event_results.csv mein total {total_rows} events hain.")
    while True:
        s_raw = input(f"➡️ Starting row (1-based, 1 se {total_rows} tak, empty = 1): ").strip()
        e_raw = input(f"➡️ Ending row (1-based, inclusive, empty = {total_rows}): ").strip()
        try:
            start = int(s_raw) if s_raw else 1
            end = int(e_raw) if e_raw else total_rows
        except ValueError:
            print("⚠️ Sirf number likho (jaise 1 ya 250). Dobara try karo.")
            continue

        start = max(1, start)
        end = min(total_rows, end)

        if start > end:
            print("⚠️ Starting row, ending row se bara hai - dobara try karo.")
            continue

        return start, end


def prompt_mode():
    print("\n--- MODE ---")
    print("1) Normal scrape (naye events scrape honge; jo events pehle se")
    print("   scrape ho chuke hain unhe bhi dobara check kiya jayega aur")
    print("   fresh data se update kar diya jayega)")
    print("2) Retry failed.txt (jo events fail ho gaye thay unhe dobara try karo)")
    while True:
        choice = input("👉 Mode select karo (1/2): ").strip()
        if choice in ("1", "2"):
            return choice
        print("⚠️ Sirf 1 ya 2 likho.")


def make_dedupe_key(event):
    key = event.get("Event UID")
    if not key or key == "N/A":
        key = event.get("Event URL")
    return key


# ======================
# INCOMPLETE-EVENT DETECTION (info/logging ke liye)
# ======================

def is_incomplete(nested_row):
    """
    Ek scrape hui event ka data 'incomplete' consider hota hai agar:
      - Results Count 0 hai (koi dog record nahi hua - almost hamesha ye
        galat/adhoori scrape ka sign hai, kyunke har real event mein
        kam se kam 1 dog hota hai), YA
      - IMPORTANT_HEADER_FIELDS (Category of Event, Results List Name,
        Date, Event Location) mein se koi bhi field mein koi meaningful
        value nahi hai (sab khaali/N-A) - matlab us waqt dl.datasheet
        theek se load/parse nahi hua tha.
    """
    try:
        if int(nested_row.get("Results Count", 0) or 0) == 0:
            return True
    except (TypeError, ValueError):
        return True

    for field in IMPORTANT_HEADER_FIELDS:
        vals = nested_row.get(field, [])
        if vals and any(v and v != "N/A" for v in vals):
            return False   # kam se kam ek important field mein value mili - complete maano

    return True   # koi bhi important field mein kuch nahi mila


def base_event_from_nested(nested_row):
    """Reference ke liye: pehle se stored nested_row se hi base_event reconstruct kar lo (Phase 1 CSV dobara padhne ki zaroorat nahi)."""
    return {
        "Event UID": nested_row.get("Event UID", "N/A"),
        "Event Name": nested_row.get("Event Name", "N/A"),
        "Event URL": nested_row.get("Event URL", "N/A"),
        "Year": nested_row.get("Year", "N/A"),
        "Category": nested_row.get("Discipline", "N/A"),
    }


# ======================
# SAVE / LOAD (resume support) - by_key ab INDEX store karta hai (row object nahi),
# taake update_event() list mein sahi jagah safely overwrite kar sake.
# ======================

def load_existing_data():
    nested_events = []
    by_key = {}
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                nested_events = json.load(f)
            for i, ev in enumerate(nested_events):
                key = make_dedupe_key(ev)
                if key:
                    by_key[key] = i
        except Exception as e:
            print("⚠️ Existing JSON load karne mein masla (fresh start ho jayega):", e)
            nested_events = []
            by_key = {}
    return nested_events, by_key


def flatten_for_csv(nested_row):
    flat = {
        "Event UID": nested_row.get("Event UID", "N/A"),
        "Event Name": nested_row.get("Event Name", "N/A"),
        "Event URL": nested_row.get("Event URL", "N/A"),
        "Year": nested_row.get("Year", "N/A"),
        "Discipline": nested_row.get("Discipline", "N/A"),
        "Category of Event": MULTI_JOIN.join(nested_row.get("Category of Event", []) or []) or "N/A",
        "Results List Name": MULTI_JOIN.join(nested_row.get("Results List Name", []) or []) or "N/A",
        "Date": MULTI_JOIN.join(nested_row.get("Date", []) or []) or "N/A",
        "Event Location": MULTI_JOIN.join(nested_row.get("Event Location", []) or []) or "N/A",
        "Judges B": MULTI_JOIN.join(nested_row.get("Judges B", []) or []) or "N/A",
        "Judges Total": MULTI_JOIN.join(nested_row.get("Judges Total", []) or []) or "N/A",
        "Judges C": MULTI_JOIN.join(nested_row.get("Judges C", []) or []) or "N/A",
        "Helpers C": MULTI_JOIN.join(nested_row.get("Helpers C", []) or []) or "N/A",
        "Helpers Total": MULTI_JOIN.join(nested_row.get("Helpers Total", []) or []) or "N/A",
        "Extra Fields": json.dumps(nested_row.get("Extra Fields", {}), ensure_ascii=False) if nested_row.get("Extra Fields") else "",
        "Results Count": nested_row.get("Results Count", 0),
        "Results": json.dumps(nested_row.get("Results", []), ensure_ascii=False),
    }
    return flat


def save_results(nested_events):
    with open(JSON_FILE, "w", encoding="utf-8") as jf:
        json.dump(nested_events, jf, ensure_ascii=False, indent=2)

    flat_rows = [flatten_for_csv(ev) for ev in nested_events]

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for r in flat_rows:
            writer.writerow(r)

    try:
        df = pd.DataFrame(flat_rows, columns=CSV_FIELDNAMES)
        df.to_excel(EXCEL_FILE, index=False)
    except Exception as e:
        print("⚠️ Error writing Excel:", e)


def append_event(nested_events, by_key, nested_row):
    """Naya event add karta hai. Agar key pehle se maujood hai to kuch nahi karta (skip)."""
    key = make_dedupe_key(nested_row)
    if not key or key in by_key:
        return False
    nested_events.append(nested_row)
    by_key[key] = len(nested_events) - 1
    return True


def update_event(nested_events, by_key, nested_row):
    """
    append_event ke ulta kaam karta hai: agar key pehle se maujood hai
    to us purani entry ko NAYI (dobara-scrape ki hui) entry se OVERWRITE
    kar deta hai - taake pehle jo incomplete/galat data save ho gaya tha
    wo sahi ho jaye. Agar key naya hai (kabhi scrape nahi hua) to normal
    add kar deta hai.
    """
    key = make_dedupe_key(nested_row)
    if not key:
        return False
    if key in by_key:
        nested_events[by_key[key]] = nested_row
    else:
        nested_events.append(nested_row)
        by_key[key] = len(nested_events) - 1
    return True


def log_failed_event(event_uid, event_url, reason="Unknown error"):
    try:
        # duplicate check - dobara wahi URL failed.txt mein na ho
        if os.path.exists(FAILED_LOG_FILE):
            with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
                existing = f.read()
            if event_url in existing:
                return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(FAILED_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] Event UID {event_uid} | URL: {event_url} | Reason: {reason}\n")
        print(f"📝 Event {event_uid} failed.txt mein likh diya gaya.")
    except Exception as e:
        print("⚠️ failed.txt likhne mein masla:", e)


def remove_from_failed_log(event_url):
    """Jab event dobara successfully scrape ho jaye to failed.txt se uski entry hata deta hai."""
    if not event_url or not os.path.exists(FAILED_LOG_FILE):
        return
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept = [ln for ln in lines if event_url not in ln]
        if len(kept) != len(lines):
            with open(FAILED_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(kept)
            print(f"🧹 {event_url} ab successfully scrape ho gaya - failed.txt se hata diya gaya.")
    except Exception as e:
        print("⚠️ failed.txt se entry hatane mein masla:", e)


def parse_failed_log():
    """failed.txt se Event UID + URL entries nikalta hai (unique, insertion order)."""
    entries = []
    seen = set()
    if not os.path.exists(FAILED_LOG_FILE):
        return entries
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m_uid = re.search(r'Event UID\s+(\S+)', line)
                m_url = re.search(r'URL:\s+(\S+)', line)
                url = m_url.group(1) if m_url else None
                uid = m_uid.group(1) if m_uid else "N/A"
                if url and url not in seen:
                    seen.add(url)
                    entries.append({"Event UID": uid, "Event URL": url})
    except Exception as e:
        print("⚠️ failed.txt parse karne mein masla:", e)
    return entries


# ======================
# SHARED SCRAPE-ONE-EVENT HELPER
# ======================

def scrape_one_event(driver, base_event, pause_event, state):
    """
    Ek event ko (CAPTCHA-aware retries ke sath) scrape karta hai.
    state = {"driver": driver} - browser crash hone par naya driver isi
    dict ke andar update ho jata hai (mutable reference, caller ko naya
    driver wapas milta hai state['driver'] se).

    Return: (flat_row, nested_row) ya (None, None) agar fail ho gaya.
    """
    event_url = base_event.get("Event URL", "N/A")
    last_error = "Unknown error"
    raw = None

    for attempt in range(1, 4):
        wait_if_paused(pause_event)
        driver = state["driver"]
        try:
            if not safe_get(driver, event_url):
                raise Exception("Page load nahi hua (safe_get failed)")
            wait_for_page_ready(driver)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "dl.datasheet"))
                )
            except TimeoutException:
                pass
            raw = extract_event_detail(driver)
        except (InvalidSessionIdException, WebDriverException) as e:
            print(f"💥 Browser session crash ho gaya: {e}")
            print("🔁 Naya browser session start kar rahe hain...")
            try:
                driver.quit()
            except Exception:
                pass
            new_driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                                      real_user_data_dir=REAL_USER_DATA_DIR,
                                      profile_name=REAL_PROFILE_NAME)
            state["driver"] = new_driver
            state["stop_event"].set()
            new_stop = threading.Event()
            state["stop_event"] = new_stop
            persistent_captcha_monitor(new_driver, pause_event, new_stop)
            raw = None
            last_error = str(e)
        except Exception as e:
            last_error = str(e)
            raw = None

        if raw is not None:
            break
        print(f"⚠️ Attempt {attempt}/3 failed: {last_error}")
        wait_if_paused(pause_event)
        nap(1.5, 2.5)

    if raw is None:
        log_failed_event(base_event.get("Event UID", "N/A"), event_url, last_error)
        return None, None

    return build_records(base_event, raw)


# ======================
# MAIN
# ======================

def main():
    all_events = load_all_input_events()
    total_rows = len(all_events)
    if total_rows == 0:
        print("⚠️ event_results.csv khali hai - kuch scrape karne ko nahi hai.")
        return

    mode = prompt_mode()
    print(f"📂 Output folder: {DATA_DIR}")

    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                          real_user_data_dir=REAL_USER_DATA_DIR,
                          profile_name=REAL_PROFILE_NAME)

    pause_event = threading.Event()
    stop_event = threading.Event()
    persistent_captcha_monitor(driver, pause_event, stop_event)
    state = {"driver": driver, "stop_event": stop_event}

    nested_events, by_key = load_existing_data()
    print(f"ℹ️ Pehle se {len(nested_events)} events ki detail scrape ho chuki hai.")

    try:
        # -------------------- MODE 1: Normal scrape (hamesha check + update/add) --------------------
        if mode == "1":
            start, end = prompt_row_range(total_rows)
            input_events = all_events[start - 1: end]
            print(f"✅ {len(input_events)} events select huay (row {start} se row {end} tak).")

            added_count = 0
            updated_count = 0
            failed_count = 0

            for idx, base_event in enumerate(input_events, start=1):
                dedupe_key = make_dedupe_key(base_event)
                existing_row = None
                if dedupe_key and dedupe_key in by_key:
                    existing_row = nested_events[by_key[dedupe_key]]

                wait_if_paused(pause_event)
                if existing_row is not None:
                    print(f"\n🔄 [{idx}/{len(input_events)}] Pehle se maujood tha - dobara check kar rahe hain: {base_event.get('Event Name')} ({base_event.get('Event URL')})")
                else:
                    print(f"\n📌 [{idx}/{len(input_events)}] Scrape ho raha hai: {base_event.get('Event Name')} ({base_event.get('Event URL')})")

                flat_row, nested_row = scrape_one_event(driver, base_event, pause_event, state)
                driver = state["driver"]

                if nested_row is None:
                    print("⚠️ Ye event scrape nahi ho saka - failed.txt mein add ho gaya, agle par ja rahe hain.")
                    failed_count += 1
                    continue

                if existing_row is not None:
                    update_event(nested_events, by_key, nested_row)
                    save_results(nested_events)
                    updated_count += 1
                    if is_incomplete(nested_row):
                        print(f"⚠️ Dobara scrape ke baad bhi data incomplete lag raha hai ({nested_row['Results Count']} dogs) - shayad waqai kam dogs hain, ya page ka format alag hai.")
                    else:
                        print(f"✅ Check/update ho gaya: {nested_row['Results Count']} dogs mile.")
                else:
                    if append_event(nested_events, by_key, nested_row):
                        added_count += 1
                        print(f"✅ Saved: {nested_row['Results Count']} dogs mile is event mein. (Total events ab {len(nested_events)})")
                        save_results(nested_events)

                nap(0.5, 1.0)

            print(f"\n📊 Summary (is run ke liye): {added_count} naye add huay, {updated_count} dobara-check/update huay, "
                  f"{failed_count} fail huay.")

        # -------------------- MODE 2: Retry failed.txt --------------------
        elif mode == "2":
            _, phase1_by_url = index_input_events(all_events)
            failed_entries = parse_failed_log()

            if not failed_entries:
                print("✅ failed.txt khali hai - koi failed event nahi hai.")
            else:
                print(f"📋 failed.txt mein {len(failed_entries)} events hain.")
                for idx, fe in enumerate(failed_entries, start=1):
                    event_url = fe["Event URL"]
                    # Phase 1 CSV se poori detail (Name/Year/Category) uthane ki koshish karo
                    base_event = phase1_by_url.get(event_url)
                    if not base_event:
                        base_event = {
                            "Event UID": fe.get("Event UID", "N/A"),
                            "Event Name": "N/A",
                            "Event URL": event_url,
                            "Year": "N/A",
                            "Category": "N/A",
                        }

                    wait_if_paused(pause_event)
                    print(f"\n🔎 [{idx}/{len(failed_entries)}] Retry: {base_event.get('Event Name')} ({event_url})")

                    flat_row, nested_row = scrape_one_event(driver, base_event, pause_event, state)
                    driver = state["driver"]

                    if nested_row is None:
                        print("⚠️ Abhi bhi fail ho raha hai - failed.txt mein rahega.")
                        continue

                    update_event(nested_events, by_key, nested_row)
                    save_results(nested_events)
                    remove_from_failed_log(event_url)
                    print(f"✅ Ab scrape ho gaya: {nested_row['Results Count']} dogs mile.")

                    nap(0.5, 1.0)

        print(f"\n🎯 Complete ho gaya. Total events with detail: {len(nested_events)}")
        print(f"💾 Files: \n  {CSV_FILE}\n  {EXCEL_FILE}\n  {JSON_FILE}")

    finally:
        state["stop_event"].set()
        try:
            state["driver"].quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()