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

# Phase 2 ka output (events_detail_scraper.py se aya hua) - do kaamon ke liye use hoga:
#   1) INPUT_CSV se hum row-range select karenge (isi mein se dogs uthenge)
#   2) INPUT_JSON PUREY dataset ka lookup banega (dog ke "Participated Events"
#      ko match karne ke liye - ye lookup HAMESHA poora dataset hota hai,
#      chahe abhi hum CSV ka chhota range process kar rahe hon)
PHASE2_DIR = os.path.join(BASE_DIR, "results_events_detail_2019-2026")
INPUT_CSV = os.path.join(PHASE2_DIR, "results_events_detail.csv")
INPUT_JSON = os.path.join(PHASE2_DIR, "results_events_detail.json")

# Phase 3 ka apna output folder
DATA_DIR = os.path.join(BASE_DIR, "German Shepherd (Events Dogs)")
os.makedirs(DATA_DIR, exist_ok=True)

CSV_FILE = os.path.join(DATA_DIR, "german_shepherd_events_dog.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "german_shepherd_events_dog.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "german_shepherd_events_dog.json")
FAILED_LOG_FILE = os.path.join(DATA_DIR, "failed.txt")

# Jo dogs check kiye ja chuke hain aur German Shepherd NAHI nikle, unki URL
# yahan likh dete hain - taake agli baar script chalane par unhe dobara
# (waqt zaya karke) na kholna pade, seedha skip ho jayein.
NON_GSD_LOG_FILE = os.path.join(DATA_DIR, "checked_non_german_shepherd.txt")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "gsd_events_dogs")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

REAL_USER_DATA_DIR = None   # e.g. r"C:\Users\<YOU>\AppData\Local\Google\Chrome\User Data"
REAL_PROFILE_NAME = "Default"

CHROME_MAIN_VERSION = 152  # <-- apne Chrome ka MAIN version yahan likho

# Har kitne dogs (successfully saved GSD) ke baad Excel dobara likhein -
# CSV/JSON hamesha turant save hote hain, Excel sirf har N dogs ke baad
EXCEL_WRITE_EVERY_N_DOGS = 5

BREED_FILTER = "german shepherd"   # case-insensitive substring match


# ======================
# CAPTCHA MONITOR + PAUSE (wahi system jo pehle scripts mein hai)
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
            print("⏸️ Verification active hai — isi dog par ruk kar wait kar rahe hain jab tak clear na ho...")
            first = False
        time.sleep(check_interval)


def nap(a=0.5, b=1.0):
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
# DRIVER INIT
# ======================

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
    nap(0.3, 0.5)


def scroll_page_fast(driver):
    """Chaar tez JS jumps (~1 second total) taake lazy-load hone wale sections trigger ho jayein."""
    try:
        total_height = driver.execute_script("return document.body.scrollHeight") or 3000
        steps = 4
        for i in range(1, steps + 1):
            y = int(total_height * i / steps)
            driver.execute_script(f"window.scrollTo(0, {y});")
            time.sleep(0.25)
    except Exception:
        pass


def extract_uid_from_url(url):
    """Dog profile URL se numeric U-ID nikalta hai (/dogs-details/<id>)."""
    try:
        if not url or url == "N/A":
            return "N/A"
        m = re.search(r"/dogs-details/(\d+)", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


def extract_event_uid_from_url(url):
    """
    Event result URL se trailing numeric UID nikalta hai
    (jaise phase 1/2 mein use hota hai) - taake participated-events ko
    phase 2 ke dataset se match kiya ja sake.
    """
    if not url or url == "N/A":
        return "N/A"
    m = re.search(r"-(\d+)/?$", url)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)/?$", url)
    if m:
        return m.group(1)
    return "N/A"


# ======================
# FAST FIELD LOOKUP (Dog Information table)
# ======================

def get_dog_info_field(driver, xpath, default="N/A"):
    """
    Dog Information table ke fields (Breed, Nickname, Variety, wagera) ke liye.
    WebDriverWait/polling nahi karta - page pehle hi poori tarah load ho
    chuki hoti hai (h1 wait ke baad), is liye field turant mil jati hai ya
    turant pata chal jata hai ke maujood nahi hai.
    """
    try:
        el = driver.find_element(By.XPATH, xpath)
        txt = (el.text or "").strip()
        if not txt:
            txt = (el.get_attribute("textContent") or "").strip()
        if txt:
            if txt.lower() == "no data":
                return default
            return txt
    except Exception:
        pass
    return default


# ======================
# CLEANER (owner/breeder fallback)
# ======================

def clean_owner_breeder_text(text):
    if not text:
        return "N/A"
    txt = text.replace('\xa0', ' ').strip()
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return "N/A"

    junk_indicators = [
        "you are the owner", "add owner now", "invite a friend",
        "hasn’t registered", "hasn't registered", "give information",
        "possibility to give", "you can invite", "register with working-dog",
        "log in to", "click here", "add breeder", "add owner", "invite him",
        "working-dog", "owner details", "breeder details",
        "you are the breeder or you know him?", "no data", "diversity traits",
    ]
    header_indicators = set([
        "owner", "owner:", "breeder", "breeder:",
        "breeder association", "owner association",
        "owner association:", "breeder association:",
    ])

    candidates = []
    for line in lines:
        low = line.lower().strip()
        if low.startswith("you are") or "invite" in low:
            continue
        if any(phrase in low for phrase in junk_indicators):
            continue
        if low in header_indicators or low.rstrip(':') in header_indicators:
            continue
        if len(line) <= 2:
            continue
        candidates.append(line)

    if candidates:
        for c in candidates:
            if re.search(r'\d', c):
                continue
            words = [w for w in c.split() if w]
            if any(w[0].isupper() for w in words if w):
                return c
        return candidates[0]

    m = re.search(r'(?:owner|breeder)[\s]*[:\-–]\s*(.+)', txt, flags=re.I)
    if m:
        name = m.group(1).strip().splitlines()[0].strip()
        if name and not any(j in name.lower() for j in junk_indicators) and len(name) > 1:
            return name

    if len(lines) >= 2 and lines[0].lower().rstrip(':') in header_indicators:
        second = lines[1].strip()
        if second and not any(j in second.lower() for j in junk_indicators):
            return second

    return "N/A"


def extract_dob_dod(driver):
    """Date of Birth aur Date of Death (agar mile) nikalta hai."""
    dob, dod = "N/A", "N/A"
    try:
        el = driver.find_element(
            By.XPATH,
            "//div[@title='Date of birth' or @title='Date of birth / Date of death']/following-sibling::div"
        )
        spans = el.find_elements(By.TAG_NAME, "span")

        if len(spans) >= 1:
            dob = (spans[0].get_attribute("textContent") or "").strip() or "N/A"

        if len(spans) >= 2:
            second_val = (spans[1].get_attribute("textContent") or "").strip()
            if second_val and not re.search(r'year', second_val, re.I):
                if re.match(r'\d{2}\.\d{2}\.\d{4}', second_val):
                    dod = second_val
    except Exception:
        pass

    return dob, dod


def extract_owner_or_breeder_multi(driver, role="owner"):
    """Owner/Breeder tabs se (multi-value support) naam nikalta hai."""
    assert role in ["owner", "breeder"]
    label = "Owner" if role == "owner" else "Breeder"

    try:
        li_xpath = (
            f"//ul[contains(@class,'nav-tabs')]"
            f"//li[.//a//span[normalize-space(text())='{label}']]"
        )
        li = driver.find_element(By.XPATH, li_xpath)
        anchors = li.find_elements(By.CSS_SELECTOR, "a.nav-link")

        names = []
        for a in anchors:
            href = a.get_attribute("href")
            if not href or "#" not in href:
                continue
            tab_id = href.split("#")[-1].strip()
            if not tab_id:
                continue
            try:
                pane = driver.find_element(By.ID, tab_id)
            except Exception:
                continue
            try:
                name_anchor = pane.find_element(By.CSS_SELECTOR, "a.font-weight-bold.text-black")
                href2 = name_anchor.get_attribute("href")
                title2 = name_anchor.get_attribute("title")
                if href2 and title2:
                    name = title2.strip()
                    if name and name not in names:
                        names.append(name)
            except Exception:
                continue

        if names:
            return ", ".join(names)
        return "N/A"
    except Exception:
        return "N/A"


def get_sire_dam(driver):
    """
    Pedigree tree (1st generation) se Sire/Dam ka naam + URL + litter-date
    nikalta hai. NOTE: is phase mein sire/dam ke liye ALAG record NAHI
    banate (jaisa purane script mein hota tha) - sirf isi dog ke record
    mein Sire/Dam Name+URL fields ke tor par save hote hain.
    """
    sire_name, dam_name = "N/A", "N/A"
    sire_url, dam_url = "N/A", "N/A"

    try:
        parents = driver.find_elements(By.CSS_SELECTOR, "div.gen-0__dog-name")
        for p in parents:
            try:
                link_el = p.find_element(By.CSS_SELECTOR, "a")
                name_el = p.find_element(By.CSS_SELECTOR, "span.gen__animal-name")
                gender_icon = p.find_element(By.CSS_SELECTOR, "span.mdi")

                name = (name_el.get_attribute("textContent") or "").strip()
                link = link_el.get_attribute("href") or "N/A"
                gender_class = gender_icon.get_attribute("class") or ""

                if "mdi-gender-male" in gender_class:
                    sire_name = name if name else "N/A"
                    sire_url = link
                elif "mdi-gender-female" in gender_class:
                    dam_name = name if name else "N/A"
                    dam_url = link
            except Exception:
                continue
    except Exception:
        pass

    return (sire_name, sire_url), (dam_name, dam_url)


def extract_dog_image_url(driver):
    """
    Dog ki main image ka URL nikalta hai. Pehle img.animal__image-blur ka
    src try karta hai, agar wo na mile to .animal__image div ke inline
    background-image style se URL nikal leta hai (fallback).
    """
    try:
        img = driver.find_element(By.CSS_SELECTOR, "img.animal__image-blur")
        src = img.get_attribute("src")
        if src:
            return src
    except Exception:
        pass
    try:
        div = driver.find_element(By.CSS_SELECTOR, ".animal__image")
        style = div.get_attribute("style") or ""
        m = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


def _clean_ws(text):
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


def extract_health_data(driver):
    """
    Health table ke 4 tabs (VetResults, LabResults, Body Traits,
    Diversity Traits). Har tab ka data-items-count check karte hain -
    agar 0 hai (empty tab) to us mein se kuch bhi extract nahi karte
    (wahi jagah junk/promotional text hoti hai).
    """
    health_data = {
        "Health VetResults": "N/A",
        "Health LabResults": "N/A",
        "Body Traits": "N/A",
        "Diversity Traits": "N/A",
    }

    try:
        tab_links = driver.find_elements(By.CSS_SELECTOR, ".health-tab__list a.tab-link")
        counts = []
        for t in tab_links:
            cnt_raw = t.get_attribute("data-items-count")
            try:
                counts.append(int(cnt_raw))
            except (TypeError, ValueError):
                counts.append(0)

        if not tab_links:
            return health_data

        content_divs = driver.find_elements(By.CSS_SELECTOR, ".tab-content-1 > div")
        keys = ["Health VetResults", "Health LabResults", "Body Traits", "Diversity Traits"]

        for i, key in enumerate(keys):
            if i >= len(counts) or i >= len(content_divs):
                continue

            item_count = counts[i]
            if item_count <= 0:
                health_data[key] = "N/A"
                continue

            panel = content_divs[i]

            if key == "Health VetResults":
                items = []
                rows = panel.find_elements(By.CSS_SELECTOR, ".btv4-health-list .row.border-bottom")
                for row in rows:
                    try:
                        title = _clean_ws(row.find_element(By.CSS_SELECTOR, ".item__title").get_attribute("textContent"))
                        result = _clean_ws(row.find_element(By.CSS_SELECTOR, ".item__result").get_attribute("textContent"))
                        desc = ""
                        try:
                            desc = _clean_ws(row.find_element(By.CSS_SELECTOR, ".item__description").get_attribute("textContent"))
                        except Exception:
                            pass
                        if title:
                            entry = f"{title}: {result}" if result else title
                            if desc:
                                entry += f" ({desc})"
                            items.append(entry)
                    except Exception:
                        continue
                health_data[key] = "; ".join(items) if items else "N/A"

            elif key == "Health LabResults":
                items = []
                rows = panel.find_elements(By.CSS_SELECTOR, ".health-item-wrap")
                for row in rows:
                    try:
                        name = _clean_ws(row.find_element(By.CSS_SELECTOR, ".health-merkmal .w-100").get_attribute("textContent"))
                        genotyp = ""
                        try:
                            genotyp = _clean_ws(row.find_element(By.CSS_SELECTOR, ".genotyp").get_attribute("textContent"))
                        except Exception:
                            pass
                        result = ""
                        try:
                            result = _clean_ws(row.find_element(By.CSS_SELECTOR, ".results").get_attribute("textContent"))
                        except Exception:
                            pass
                        if name:
                            entry = name
                            if genotyp:
                                entry += f" ({genotyp})"
                            if result:
                                entry += f": {result}"
                            items.append(entry)
                    except Exception:
                        continue
                health_data[key] = "; ".join(items) if items else "N/A"

            else:
                items = []
                rows = panel.find_elements(By.CSS_SELECTOR, ".btv4-health-list .row.border-bottom")
                if not rows:
                    rows = panel.find_elements(By.CSS_SELECTOR, ".health-item-wrap")
                for row in rows:
                    try:
                        text = _clean_ws(row.get_attribute("textContent"))
                        if text:
                            items.append(text)
                    except Exception:
                        continue
                health_data[key] = "; ".join(items) if items else "N/A"

    except Exception as e:
        print("⚠️ extract_health_data error:", e)

    return health_data


# ======================
# PARTICIPATED EVENTS EXTRACTOR (dog profile page ke neeche wala section)
# ======================

_PARTICIPATED_EVENTS_JS = r"""
// IMPORTANT FIX: is page par id="resultList" DO JAGAH aata hai - ek
// desktop version (wrapper #animalResultLists ke andar) aur ek mobile
// version (alag jagah, dono hidden/visible CSS se control hote hain
// lekin DOM mein dono hamesha maujood rehte hain). Agar hum seedha
// "#resultList > div.row" use karte to dono duplicate containers se
// rows mil jate (isi wajah se pehle events 2x aa rahe the aur kai
// fields N/A aa rahi thi - kyunke mobile version ka structure desktop
// se mukhtalif hai). Ab hum SIRF desktop wrapper (#animalResultLists)
// ke andar wale #resultList ko target karte hain - is se hamesha
// sirf EK (sahi, poori tarah structured) copy milti hai.
var wrapper = document.querySelector('#animalResultLists');
var container = wrapper ? wrapper.querySelector('#resultList') : document.querySelector('#resultList');
var rows = container ? container.querySelectorAll(':scope > div.row') : [];

var results = [];
rows.forEach(function(row){
    var eventLink = row.querySelector('.event a');
    if (!eventLink) return;   // separator/non-data row - skip

    function textOf(sel) {
        var el = row.querySelector(sel);
        return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    }
    function valOf(sel) {
        var el = row.querySelector(sel + ' .val');
        return el ? (el.textContent || '').trim() : '';
    }

    var place = '';
    var placeEl = row.querySelector('.v1');
    if (placeEl) {
        var img = placeEl.querySelector('img');
        place = img ? (img.getAttribute('alt') || '') : (placeEl.textContent || '').trim();
    }

    var eventUrl = eventLink.getAttribute('href') || '';
    var eventName = (eventLink.getAttribute('title') || eventLink.textContent || '').trim();

    var date = textOf('.evt_date');
    var location = textOf('.evt_location');

    // "dog leader" wahi column hai jise website khud isi naam se
    // dikhati hai (table header: "dog leader") - isliye hum bhi wahi
    // naam use kar rahe hain, "handler" ki bajaye.
    var leaderName = '', leaderUrl = '', country = '';
    var r2 = row.querySelector('.r2');
    if (r2) {
        var a2 = r2.querySelector('a');
        if (a2) {
            leaderUrl = a2.getAttribute('href') || '';
            leaderName = (a2.getAttribute('title') || a2.textContent || '').trim();
        }
        var flag = r2.querySelector('img.flag');
        if (flag) country = flag.getAttribute('title') || '';
    }

    results.push({
        place: place,
        event_name: eventName,
        event_url: eventUrl,
        participation_date: date,
        participation_location: location,
        dog_leader_name: leaderName || 'N/A',
        dog_leader_url: leaderUrl || 'N/A',
        country: country || 'N/A',
        score_a: valOf('.v2'),
        score_b: valOf('.v3'),
        score_c: valOf('.v4'),
        total: valOf('.v5'),
        mark: valOf('.v6')
    });
});
return results;
"""


def extract_participated_events(driver, phase2_lookup):
    """
    Dog profile page ke "Participated Events" section se saare events
    nikalta hai. Har entry mein DO tarah ki cheezein hoti hain, aur
    duplication se bachne ke liye dono ko ALAG rakha gaya hai:

      1) Dog-specific fields (top level)  - sirf isi dog se related:
         Place, Dog Leader Name/URL, Score A/B/C, Total, Mark, Country.

      2) Event-level fields ("Event Details" ke andar) - Event Name,
         URL, Date, Location, Category, Judges, Helpers, wagera. Ye
         saare dogs ke liye SAME hote (same event), isliye inhe sirf
         EK jagah rakha hai, do baar nahi:
           - Agar us Event UID ka match Phase 2 dataset (results_events_
             detail.json) mein mil jaye -> "Event Details" mein Phase 2
             ki POORI rich detail (Category, Judges, Helpers, wagera)
             aati hai, "Matched": true ke sath.
           - Agar match NA mile (kabhi event Phase 2 ke scrape range
             mein nahi aaya) -> "Event Details" mein sirf profile-page
             se mile basic fields (Event Name, URL, Date, Location)
             fallback ki tarah aate hain, "Matched": false ke sath -
             taake pata chale ye basic hai ya rich.

    Pehle Event Name/URL/Date/Location TOP LEVEL par bhi thay AUR
    "Matched Event Details" ke andar bhi - isse data 2x duplicate ho
    raha tha. Ab har field ki EK hi jagah hai.

    NOTE: "Results" (us event ke SAARE dogs ki list) jaan-boojh kar
    Event Details mein NAHI daalte - wo Phase 2 mein already alag se
    save ho chuki hai, yahan dobara daalna sirf mess badhata.
    """
    try:
        raw_events = driver.execute_script(_PARTICIPATED_EVENTS_JS) or []
    except Exception as e:
        print("⚠️ extract_participated_events error:", e)
        raw_events = []

    events = []
    seen_keys = set()   # safety-net dedup (event_url + place + total combo)

    for ev in raw_events:
        event_url = ev.get("event_url", "")
        place = ev.get("place") or "N/A"
        total = ev.get("total") or "N/A"

        dedupe_key = (event_url, place, total)
        if dedupe_key in seen_keys:
            continue   # extra safety - agar kabhi koi duplicate row phir bhi aa jaye
        seen_keys.add(dedupe_key)

        event_uid = extract_event_uid_from_url(event_url)
        matched_raw = phase2_lookup.get(event_uid, {}) if event_uid != "N/A" else None

        if matched_raw:
            # Rich detail Phase 2 se mila - "Results" hata kar baaki sab rakho
            event_details = {k: v for k, v in matched_raw.items() if k != "Results"}
            event_details["Matched"] = True
        else:
            # Match nahi mila - profile page se mile basic fields hi fallback ke tor par
            event_details = {
                "Event Name": ev.get("event_name") or "N/A",
                "Event URL": event_url or "N/A",
                "Date": ev.get("participation_date") or "N/A",
                "Event Location": ev.get("participation_location") or "N/A",
                "Matched": False,
            }

        events.append({
            "Event UID": event_uid,
            "Place": place,
            "Dog Leader Name": ev.get("dog_leader_name") or "N/A",
            "Dog Leader URL": ev.get("dog_leader_url") or "N/A",
            "Country": ev.get("country") or "N/A",
            "Score A": ev.get("score_a") or "N/A",
            "Score B": ev.get("score_b") or "N/A",
            "Score C": ev.get("score_c") or "N/A",
            "Total": total,
            "Mark": ev.get("mark") or "N/A",
            "Event Details": event_details,
        })

    return events



# ======================
# PHASE 2 LOOKUP (poora dataset, matching ke liye)
# ======================

def load_phase2_lookup():
    """
    Phase 2 ke results_events_detail.json se PUREY dataset ka lookup
    banata hai (Event UID -> poori event detail). Ye hamesha POORA
    dataset hota hai (chahe abhi CSV ka chhota range process ho raha ho),
    taake kisi bhi dog ke Participated Events mein us dataset ka koi bhi
    event match ho sake.
    """
    if not os.path.exists(INPUT_JSON):
        print(f"⚠️ Phase 2 ka JSON file nahi mila ({INPUT_JSON}) - matching kaam nahi karegi.")
        return {}

    try:
        with open(INPUT_JSON, "r", encoding="utf-8") as f:
            all_events = json.load(f)
    except Exception as e:
        print("⚠️ Phase 2 JSON load karne mein masla:", e)
        return {}

    lookup = {}
    for ev in all_events:
        uid = ev.get("Event UID")
        if uid and uid != "N/A":
            lookup[uid] = ev

    print(f"ℹ️ Phase 2 dataset se {len(lookup)} events ka lookup ban gaya (matching ke liye).")
    return lookup


# ======================
# INPUT: PHASE 2 CSV SE ROW-RANGE + DOGS QUEUE BANANA
# ======================

def load_phase2_csv_rows():
    if not os.path.exists(INPUT_CSV):
        raise FileNotFoundError(f"Phase 2 ka CSV nahi mila: {INPUT_CSV}")
    rows = []
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def prompt_row_range(total_rows):
    """Terminal mein pooch leta hai kis row se kis row tak (events_result_detail.csv ke hisaab se)."""
    print(f"📊 results_events_detail.csv mein total {total_rows} events hain.")
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


def build_dog_queue(selected_rows):
    """
    Selected event-rows ke "Results" column (JSON string) se saare
    (dog_name, dog_url) pairs nikalta hai, duplicate URLs remove karta
    hai (agar koi dog kai events mein appear ho raha ho is range mein).
    """
    dog_queue = []
    seen = set()
    for row in selected_rows:
        raw = row.get("Results") or "[]"
        try:
            dogs = json.loads(raw)
        except Exception:
            dogs = []
        for d in dogs:
            durl = (d.get("dog_url") or "").strip()
            if not durl or durl == "N/A" or durl in seen:
                continue
            seen.add(durl)
            dog_queue.append({"dog_name": d.get("dog_name", "N/A"), "dog_url": durl})
    return dog_queue


# ======================
# CORE: DOG PROFILE SCRAPE (breed-gated)
# ======================

FIELDNAMES = [
    "U-ID", "URL", "Name", "Image URL", "Nickname", "Breed", "DOB", "Date of Death", "Bred In",
    "Sire", "Sire ID", "Sire URL", "Dam", "Dam ID", "Dam URL",
    "Reg No", "Pedigree Number", "2nd Pedigree Number", "Chip Number", "Variety",
    "Breeders Association", "Result", "Working Cert",
    "Owner Name", "Breeder Name",
    "Health VetResults", "Health LabResults", "Body Traits", "Diversity Traits",
    "Participated Events Count", "Participated Events",
]


def scrape_dog_profile_gsd(driver, url, phase2_lookup, error_holder=None):
    """
    Ek dog profile scrape karta hai. Return values:
      ("ok", dog_info_dict)   -> German Shepherd mila, poora data ready hai
      ("not_gsd", None)       -> Breed confidently kuch aur hai (GSH nahi)
      ("error", None)         -> Page load/extraction mein masla (retry lazim)

    IMPORTANT: Breed field sabse PEHLE check karte hain - agar GSH nahi
    hai to baaki (health/owner/pedigree/participated-events) BILKUL
    extract nahi karte, taake speed barhe (zyadatar dogs shayad German
    Shepherd nahi honge). Agar Breed field khud hi "N/A" mile (matlab
    field abhi load nahi hua / page adhoora hai), to isay "not_gsd" nahi
    maante - "error" maan kar retry karte hain (taake koi asli GSH dog
    ghalti se skip na ho jaye).
    """
    try:
        if not safe_get(driver, url):
            if error_holder is not None:
                error_holder.append("Page load nahi hua (safe_get failed)")
            return "error", None

        wait_for_page_ready(driver)
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, "//h1")))
        scroll_page_fast(driver)

        breed = get_dog_info_field(driver, "//div[@title='Breed']/following-sibling::div")

        if breed == "N/A":
            # Breed field hi nahi mila - page shayad adhoora load hua,
            # confidently "not GSH" nahi keh sakte - retry karwate hain
            if error_holder is not None:
                error_holder.append("Breed field nahi mila (page adhoora ho sakta hai)")
            return "error", None

        if BREED_FILTER not in breed.strip().lower():
            return "not_gsd", None

        # ---- Yahan se ab confirm ho chuka hai ke ye German Shepherd hai ----

        owner_name = extract_owner_or_breeder_multi(driver, role="owner")
        breeder_name = extract_owner_or_breeder_multi(driver, role="breeder")
        (sire_name, sire_url), (dam_name, dam_url) = get_sire_dam(driver)
        dob, dod = extract_dob_dod(driver)
        sire_id = extract_uid_from_url(sire_url)
        dam_id = extract_uid_from_url(dam_url)

        health_data = extract_health_data(driver)
        image_url = extract_dog_image_url(driver)

        nickname = get_dog_info_field(driver, "//div[@title='Nickname']/following-sibling::div")
        breeders_association = get_dog_info_field(
            driver, "//div[contains(@title,'Breeders') and contains(@title,'association')]/following-sibling::div"
        )
        result_field = get_dog_info_field(driver, "//div[@title='Result']/following-sibling::div")
        working_cert = get_dog_info_field(driver, "//div[@title='Working cert.']/following-sibling::div")
        second_pedigree_number = get_dog_info_field(driver, "//div[@title='2nd Pedigree number']/following-sibling::div")

        # Dog name (multiple fallbacks)
        dog_name = "N/A"
        try:
            h1_el = driver.find_element(By.TAG_NAME, "h1")
            dog_name = (h1_el.get_attribute("textContent") or "").strip()
        except Exception:
            pass
        if not dog_name or dog_name == "N/A":
            try:
                dog_name = driver.title.strip()
                if " - working-dog" in dog_name:
                    dog_name = dog_name.replace(" - working-dog", "").strip()
            except Exception:
                pass
        if not dog_name or dog_name == "N/A":
            try:
                url_part = url.rstrip("/").split("/")[-1]
                dog_name = up.unquote(url_part).replace("-", " ").strip()
            except Exception:
                dog_name = f"Dog-{extract_uid_from_url(url)}"

        participated_events = extract_participated_events(driver, phase2_lookup)

        dog_info = {
            "U-ID": extract_uid_from_url(url),
            "URL": url,
            "Name": dog_name,
            "Image URL": image_url,
            "Nickname": nickname,
            "Breed": breed,
            "DOB": dob,
            "Date of Death": dod,
            "Bred In": get_dog_info_field(driver, "//div[@title='Bred in']/following-sibling::div"),
            "Sire": sire_name,
            "Sire ID": sire_id,
            "Sire URL": sire_url,
            "Dam": dam_name,
            "Dam ID": dam_id,
            "Dam URL": dam_url,
            "Reg No": get_dog_info_field(driver, "//div[@title='Registration number']/following-sibling::div"),
            "Pedigree Number": get_dog_info_field(driver, "//div[@title='Pedigree number']/following-sibling::div"),
            "2nd Pedigree Number": second_pedigree_number,
            "Chip Number": get_dog_info_field(driver, "//div[@title='Chip number']/following-sibling::div"),
            "Variety": get_dog_info_field(driver, "//div[@title='Variety']/following-sibling::div"),
            "Breeders Association": breeders_association,
            "Result": result_field,
            "Working Cert": working_cert,
            "Owner Name": owner_name,
            "Breeder Name": breeder_name,
            "Health VetResults": health_data.get("Health VetResults", "N/A"),
            "Health LabResults": health_data.get("Health LabResults", "N/A"),
            "Body Traits": health_data.get("Body Traits", "N/A"),
            "Diversity Traits": health_data.get("Diversity Traits", "N/A"),
            "Participated Events Count": len(participated_events),
            "Participated Events": participated_events,
        }

        return "ok", dog_info

    except Exception as e:
        if error_holder is not None:
            error_holder.append(str(e))
        return "error", None


def scrape_dog_with_retry_gsd(driver, url, phase2_lookup, pause_event, max_retries=3):
    """
    scrape_dog_profile_gsd() ko retry ke saath wrap karta hai (sirf
    "error" status par retry karta hai - "ok" aur "not_gsd" dono
    confident/final outcomes hain, unko retry karne ki zaroorat nahi).
    Session-crash exceptions upar (calling code) tak propagate hoti hain
    taake wahan driver reinit ho sake.
    """
    last_error = "Unknown error"

    for attempt in range(1, max_retries + 1):
        wait_if_paused(pause_event)

        error_holder = []
        try:
            status, data = scrape_dog_profile_gsd(driver, url, phase2_lookup, error_holder=error_holder)
        except (InvalidSessionIdException, WebDriverException):
            raise
        except Exception as e:
            status, data = "error", None
            error_holder.append(str(e))

        if status in ("ok", "not_gsd"):
            return status, data

        last_error = error_holder[0] if error_holder else "Unknown error"
        print(f"⚠️ Attempt {attempt}/{max_retries} failed for {url}: {last_error}")
        wait_if_paused(pause_event)
        nap(1.5, 2.5)

    return "error", last_error


# ======================
# SAVE / LOAD (resume support)
# ======================

def load_existing_data():
    """Pehle se JSON file mein jitna GSH data hai wahan se resume karne ke liye."""
    nested_dogs = []
    by_url = {}
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                nested_dogs = json.load(f)
            for d in nested_dogs:
                u = d.get("URL")
                if u:
                    by_url[u] = d
        except Exception as e:
            print("⚠️ Existing JSON load karne mein masla (fresh start ho jayega):", e)
            nested_dogs = []
            by_url = {}
    return nested_dogs, by_url


def flatten_for_csv(dog):
    flat = {k: dog.get(k, "N/A") for k in FIELDNAMES}
    flat["Participated Events"] = json.dumps(dog.get("Participated Events", []), ensure_ascii=False)
    flat["Participated Events Count"] = dog.get("Participated Events Count", 0)
    return flat


def save_results(nested_dogs):
    with open(JSON_FILE, "w", encoding="utf-8") as jf:
        json.dump(nested_dogs, jf, ensure_ascii=False, indent=2)

    flat_rows = [flatten_for_csv(d) for d in nested_dogs]

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in flat_rows:
            writer.writerow(r)

    try:
        df = pd.DataFrame(flat_rows, columns=FIELDNAMES)
        df.to_excel(EXCEL_FILE, index=False)
    except Exception as e:
        print("⚠️ Error writing Excel:", e)


def append_dog(nested_dogs, by_url, dog_info):
    url = dog_info.get("URL")
    if not url or url in by_url:
        return False
    nested_dogs.append(dog_info)
    by_url[url] = dog_info
    return True


# ======================
# FAILED / NON-GSD LOG FILES
# ======================

def _log_url_line(path, url, extra=""):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                if url in f.read():
                    return  # already logged, avoid duplicate
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {url}" + (f" | {extra}" if extra else "") + "\n")
    except Exception as e:
        print(f"⚠️ {path} likhne mein masla:", e)


def log_failed(url, reason="Unknown error"):
    _log_url_line(FAILED_LOG_FILE, url, f"Reason: {reason}")
    print(f"📝 Failed dog failed.txt mein likh diya gaya: {url}")


def log_non_gsd(url):
    _log_url_line(NON_GSD_LOG_FILE, url)


def load_non_gsd_cache():
    urls = set()
    if not os.path.exists(NON_GSD_LOG_FILE):
        return urls
    try:
        with open(NON_GSD_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'(https?://\S+)', line)
                if m:
                    urls.add(m.group(1).strip())
    except Exception as e:
        print("⚠️ checked_non_german_shepherd.txt parse karne mein masla:", e)
    return urls


# ======================
# MAIN
# ======================

def main():
    phase2_lookup = load_phase2_lookup()

    csv_rows = load_phase2_csv_rows()
    total_rows = len(csv_rows)
    if total_rows == 0:
        print("⚠️ results_events_detail.csv khali hai - kuch scrape karne ko nahi hai.")
        return

    start, end = prompt_row_range(total_rows)
    selected_rows = csv_rows[start - 1: end]
    print(f"✅ {len(selected_rows)} events select huay (row {start} se row {end} tak).")

    dog_queue = build_dog_queue(selected_rows)
    print(f"🐕 In events se {len(dog_queue)} unique dogs mile (duplicates already removed).")
    print(f"📂 Output folder: {DATA_DIR}")

    if not dog_queue:
        print("⚠️ Is range mein koi dog nahi mila.")
        return

    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                          real_user_data_dir=REAL_USER_DATA_DIR,
                          profile_name=REAL_PROFILE_NAME)

    pause_event = threading.Event()
    stop_event = threading.Event()
    persistent_captcha_monitor(driver, pause_event, stop_event)

    nested_dogs, by_url = load_existing_data()
    non_gsd_cache = load_non_gsd_cache()
    print(f"ℹ️ Pehle se {len(nested_dogs)} German Shepherd dogs save ho chuke hain (resume ho jayega).")
    print(f"ℹ️ {len(non_gsd_cache)} dogs pehle check ho chuke hain aur GSH nahi nikle (skip ho jayenge).")

    gsd_counter = 0

    try:
        for idx, dog in enumerate(dog_queue, start=1):
            url = dog["dog_url"]

            if url in by_url:
                continue  # already saved as GSD
            if url in non_gsd_cache:
                continue  # already confirmed - not GSH

            wait_if_paused(pause_event)
            print(f"\n🔎 [{idx}/{len(dog_queue)}] Checking: {dog['dog_name']} | {url}")

            status, data = None, None
            for crash_attempt in range(2):   # ek dafa browser-crash recovery allow karte hain
                try:
                    status, data = scrape_dog_with_retry_gsd(driver, url, phase2_lookup, pause_event)
                    break
                except (InvalidSessionIdException, WebDriverException) as e:
                    print(f"💥 Browser session crash ho gaya: {e}")
                    print("💾 Ab tak ka data save kar rahe hain aur naya browser session start kar rahe hain...")
                    save_results(nested_dogs)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    stop_event.set()
                    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                                          real_user_data_dir=REAL_USER_DATA_DIR,
                                          profile_name=REAL_PROFILE_NAME)
                    stop_event = threading.Event()
                    persistent_captcha_monitor(driver, pause_event, stop_event)
                    status, data = None, None
                    continue

            if status is None:
                log_failed(url, "Browser crash - dobara try karne ke baad bhi recover nahi hua")
                continue

            if status == "ok":
                if append_dog(nested_dogs, by_url, data):
                    gsd_counter += 1
                    print(f"🐕✅ German Shepherd mila: {data['Name']} - saved. (Total GSD ab {len(nested_dogs)})")
                    save_results(nested_dogs)
            elif status == "not_gsd":
                print(f"⏭️ {dog['dog_name']} German Shepherd nahi hai - skip (cache mein add ho gaya).")
                log_non_gsd(url)
                non_gsd_cache.add(url)
            else:  # "error"
                print(f"⚠️ Scrape nahi ho saka: {data}")
                log_failed(url, data if isinstance(data, str) else "Unknown error")

            nap(0.3, 0.6)

        print(f"\n🎯 Is range ke saare dogs check ho gaye. Total German Shepherd dogs: {len(nested_dogs)}")
        print(f"💾 Files: \n  {CSV_FILE}\n  {EXCEL_FILE}\n  {JSON_FILE}")

    finally:
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()