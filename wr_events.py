import os
import re
import csv
import json
import time
import random
import requests
import threading
import urllib.parse as up

import pandas as pd
from bs4 import BeautifulSoup

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
)


# ======================
# CONFIG
# ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "working_dog_events")
os.makedirs(DATA_DIR, exist_ok=True)

CSV_FILE = os.path.join(DATA_DIR, "working_dog_events.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "working_dog_events.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "working_dog_events.json")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "events")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

REAL_USER_DATA_DIR = None   # e.g. r"C:\Users\<YOU>\AppData\Local\Google\Chrome\User Data"
REAL_PROFILE_NAME = "Default"

# apne Chrome ka MAIN version yahan likho (chrome://settings/help mein dekho)
CHROME_MAIN_VERSION = 150

EVENT_CENTER_URL = "https://www.working-dog.com/event-center"

# "none" checkbox ka id - isko hi CHHOR kar baaki sab tick karne hain
NONE_CHECKBOX_ID = "eventDiscipline13"

FIELDNAMES = ["UID", "URL", "Event Name", "Date From", "Date Until", "Town"]

# Har page ke baad Excel bhi likhna hai (events ka data halka hai)
EXCEL_WRITE_EVERY_N_PAGES = 1


# ======================
# CAPTCHA HANDLING (dog scraper jaisi hi - copy/paste consistent)
# ======================

def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    """
    Background monitor: detect karta hai iframe[src*='turnstile'].
    Agar iframe ajaye -> pause_event.set() (scraper pause ho jata hai) AUR
    khud-b-khud 2captcha se solve karne ki koshish karta hai (handle_captcha).
    Agar auto-solve kaam kar jaye -> pause_event.clear() (scraper resume).
    Agar auto-solve fail ho jaye -> pause_event set hi rehta hai, taake
    manually browser mein solve kiya ja sake; scraper tab tak aage nahi
    badhta (wait_if_paused dekho).
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
                            handle_captcha(driver)   # 2captcha se auto-solve ki koshish (ek hi baar)
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
    """Jab tak CAPTCHA/verification active hai (pause_event set), yahin ruko."""
    first = True
    while pause_event and pause_event.is_set():
        if first:
            print("⏸️ Verification active hai — ruk kar wait kar rahe hain jab tak clear na ho...")
            first = False
        time.sleep(check_interval)


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
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for f in iframes:
            src = f.get_attribute("src") or ""
            if "turnstile" in src:
                qs = up.parse_qs(up.urlparse(src).query)
                if "sitekey" in qs:
                    return qs["sitekey"][0]
    except Exception:
        pass
    try:
        src = driver.page_source
        m = re.search(r"sitekey['\":\s]*([A-Za-z0-9\-_]+)", src, flags=re.I)
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
                                if (turnstile.setResponse) {
                                    turnstile.setResponse(wid, token);
                                }
                            });
                            console.log("✅ Token injected into Turnstile");
                            return true;
                        }
                    }
                } catch(e) { console.log("⚠️ Injection error:", e); }
                return false;
            }
            var ok = setToken();
            if (!ok) {
                var iv = setInterval(function(){
                    if (setToken()) clearInterval(iv);
                }, 1000);
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
# HELPERS
# ======================

def nap(a=0.4, b=0.8):
    time.sleep(random.uniform(a, b))


def extract_uid_from_event_url(url):
    """URL ke aakhir mein jo number hota hai (e.g. ...-37539) wahi UID hai.
    Query string (?...) aur fragment (#...) pehle hata dete hain taake
    regex hamesha reliably match kare, chahe URL ke aakhir mein kuch bhi
    extra ho."""
    try:
        if not url or url == "N/A":
            return "N/A"
        clean_url = url.split("?")[0].split("#")[0].rstrip("/")
        m = re.search(r'-(\d+)$', clean_url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


# ======================
# DRIVER INIT (dog scraper jaisa hi)
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

    # version_main dena zaroori hai - warna uc khud galat (naya) driver version
    # download kar leta hai jo installed Chrome se match nahi karta
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


def wait_for_page_ready(driver, timeout=20):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    nap(0.3, 0.5)


# ======================
# FORM FILL + SEARCH (Selenium-driven, real clicks)
# ======================

def perform_search(driver, pause_event=None):
    """
    /event-center par jaake:
      1) 'Please choose the disciplines' ke saare checkboxes tick karta hai,
         SIRF 'none' (id=eventDiscipline13) ko chhor kar.
      2) 1 second wait karta hai.
      3) 'search' button (id=searchBtn) click karta hai.
      4) Results table load hone ka wait karta hai.

    Ye poora AJAX-based hai (address bar change nahi hota), is liye hum
    hamesha DOM state check karte hain, URL nahi.
    """
    wait_if_paused(pause_event)

    if not safe_get(driver, EVENT_CENTER_URL):
        raise RuntimeError("Event-center page open nahi ho saka.")

    wait_for_page_ready(driver)
    wait_if_paused(pause_event)

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='eventDiscipline']"))
    )

    checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[name='eventDiscipline']")
    print(f"🔲 {len(checkboxes)} discipline checkboxes mile — 'none' ko chhor kar sab tick kar rahe hain...")

    for cb in checkboxes:
        cb_id = cb.get_attribute("id")
        if cb_id == NONE_CHECKBOX_ID:
            continue
        if not cb.is_selected():
            driver.execute_script("arguments[0].click();", cb)

    nap(1.0, 1.2)   # 1 sec wait, jaisa bataya gaya
    wait_if_paused(pause_event)

    search_btn = driver.find_element(By.ID, "searchBtn")
    driver.execute_script("arguments[0].click();", search_btn)
    print("🔍 Search button click ho gaya, results ka wait kar rahe hain...")

    WebDriverWait(driver, 60).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#evtsarchresult table.standardTable"))
    )
    wait_for_page_ready(driver)
    nap(0.5, 0.8)
    print("✅ Results load ho gaye.")


def get_total_events_and_pages(driver, count_per_page_hint=10):
    """Header 'events found: 8021' se total events + total pages nikalta hai."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'events found:\s*([\d,]+)', body_text, re.I)
        if m:
            total = int(m.group(1).replace(",", ""))
            total_pages = max(1, -(-total // count_per_page_hint))
            return total, total_pages
    except Exception:
        pass
    return None, None


# ======================
# PAGE NAVIGATION (form-override technique)
# ======================
#
# Site ka pagination AJAX-based hai. Har pagination button (Next / jump /
# Last) apna alag <form> rakhta hai jisme SAARI filter fields + apna khaas
# 'eventcenter_search_page_number' hidden input hota hai. Button click par
# JS is form ke fields serialize karke AJAX call karta hai.
#
# Trick: hum 'Next page' wale form ke hidden page-number field ki VALUE
# JS se apne manchahe target page number se replace kar dete hain, phir
# usi form ka button click kar dete hain - is se site apna khud ka AJAX
# mechanism use karke SEEDHA us page tak le jati hai (chahe wo 500 pages
# door ho), bina baar baar Next click kiye.
#

def _get_results_table_element(driver):
    try:
        return driver.find_element(By.CSS_SELECTOR, "#evtsarchresult table.standardTable")
    except NoSuchElementException:
        return None


def goto_page(driver, target_page, pause_event=None):
    """
    'Next page' form ka hidden page-number input target_page se override
    karke usi form ka button click karta hai - is se seedha target_page
    tak jump ho jata hai (chahe wo current se bahut door ho).

    Return: True (jump successful) / False (Next form hi nahi mila,
    matlab hum already last page par hain).
    """
    wait_if_paused(pause_event)

    try:
        next_form = driver.find_element(By.CSS_SELECTOR, "form[title='Next page']")
    except NoSuchElementException:
        return False

    try:
        page_input = next_form.find_element(By.CSS_SELECTOR, "input[name='eventcenter_search_page_number']")
        submit_btn = next_form.find_element(By.CSS_SELECTOR, "input[type='button']")
    except NoSuchElementException:
        return False

    old_table = _get_results_table_element(driver)

    driver.execute_script("arguments[0].value = arguments[1];", page_input, str(target_page))
    driver.execute_script("arguments[0].click();", submit_btn)

    try:
        if old_table is not None:
            WebDriverWait(driver, 30).until(EC.staleness_of(old_table))
    except (TimeoutException, StaleElementReferenceException):
        pass

    wait_if_paused(pause_event)

    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#evtsarchresult table.standardTable"))
    )
    wait_for_page_ready(driver)
    nap(0.4, 0.7)
    return True


def goto_next_page(driver, pause_event=None):
    """
    Normal 'agla page' - 'Next page' form ke apne (already correct)
    page-number ke sath hi click karta hai, koi override nahi.
    Return: True (agla page mil gaya) / False (last page, aur pages nahi).
    """
    wait_if_paused(pause_event)

    try:
        next_form = driver.find_element(By.CSS_SELECTOR, "form[title='Next page']")
        submit_btn = next_form.find_element(By.CSS_SELECTOR, "input[type='button']")
    except NoSuchElementException:
        return False

    old_table = _get_results_table_element(driver)

    driver.execute_script("arguments[0].click();", submit_btn)

    try:
        if old_table is not None:
            WebDriverWait(driver, 30).until(EC.staleness_of(old_table))
    except (TimeoutException, StaleElementReferenceException):
        pass

    wait_if_paused(pause_event)

    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#evtsarchresult table.standardTable"))
    )
    wait_for_page_ready(driver)
    nap(0.4, 0.7)
    return True


# ======================
# TABLE PARSER
# ======================

def scrape_current_page_events(driver):
    """Currently loaded results table se saare events (list of dicts) nikalta hai."""
    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.select_one("#evtsarchresult table.standardTable")
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    events = []
    for row in tbody.find_all("tr"):
        # Advertisement row mein <td colspan="6" ...> hota hai - skip karo
        if row.find("td", attrs={"colspan": True}):
            continue

        tds = row.find_all("td")
        if len(tds) < 5:
            continue

        try:
            date_from = tds[1].get_text(strip=True)
            date_until = tds[2].get_text(strip=True)

            name_a = tds[3].find("a")
            event_name = name_a.get_text(strip=True) if name_a else "N/A"
            event_url = name_a.get("href") if name_a else "N/A"

            town_a = tds[4].find("a")
            town = "N/A"
            if town_a:
                town = town_a.get_text(strip=True) or town_a.get("title", "N/A")

            uid = extract_uid_from_event_url(event_url)

            events.append({
                "UID": uid or "N/A",
                "URL": event_url or "N/A",
                "Event Name": event_name or "N/A",
                "Date From": date_from or "N/A",
                "Date Until": date_until or "N/A",
                "Town": town or "N/A",
            })
        except Exception as e:
            print("⚠️ Row parse error (skip kar rahe hain):", e)
            continue

    return events


# ======================
# FILE UTILITIES
# ======================

def load_existing_data():
    rows = []
    by_uid = {}
    if os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for k in FIELDNAMES:
                        if k not in row:
                            row[k] = "N/A"
                    rows.append(row)
                    uid = (row.get("UID") or "").strip()
                    if uid and uid != "N/A":
                        by_uid[uid] = row
        except Exception as e:
            print("⚠️ Error loading existing CSV:", e)
    return rows, by_uid


def write_all(rows, include_excel=True):
    try:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print("⚠️ Error writing CSV:", e)

    if include_excel:
        try:
            df = pd.DataFrame(rows)
            df.to_excel(EXCEL_FILE, index=False)
        except Exception as e:
            print("⚠️ Error writing Excel:", e)

    try:
        with open(JSON_FILE, "w", encoding="utf-8") as jf:
            json.dump(rows, jf, ensure_ascii=False, indent=4)
    except Exception as e:
        print("⚠️ Error writing JSON:", e)


def append_or_update_event(rows, by_uid, event):
    """UID hi unique key hai. Same UID ka event dobara mile to naya record
    NAHI banta - sirf mismatch/missing fields update hoti hain (sahi purani
    values chherhi nahi jatin). Naya UID ho to hi add hota hai."""
    uid = (event.get("UID") or "").strip()
    if not uid or uid == "N/A":
        return False, False   # (changed, was_new)

    if uid in by_uid:
        existing = by_uid[uid]
        changed = False
        for k in FIELDNAMES:
            if k == "UID":
                continue
            new_val = (event.get(k) or "N/A").strip()
            old_val = (existing.get(k) or "N/A").strip()
            if new_val and new_val != "N/A" and new_val != old_val:
                existing[k] = new_val
                changed = True
        return changed, False

    clean = {k: (event.get(k) or "N/A") for k in FIELDNAMES}
    rows.append(clean)
    by_uid[uid] = clean
    return True, True


# ======================
# MAIN SCRAPE LOOP (crash-safe: browser crash hone par resume karta hai)
# ======================

def scrape_all_events(continue_from=None):
    rows, by_uid = load_existing_data()
    print(f"ℹ️ Loaded {len(rows)} existing events from database (agar koi hain).")

    start_page = 1
    if continue_from:
        try:
            start_page = int(continue_from)
            if start_page < 1:
                start_page = 1
        except ValueError:
            print("⚠️ Invalid page number diya gaya tha, page 1 se start kar rahe hain.")
            start_page = 1

    page_num = start_page
    page_counter = 0
    total_pages = None

    driver = None
    stop_event = None
    pause_event = threading.Event()

    while True:
        try:
            if driver is None:
                driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                                      real_user_data_dir=REAL_USER_DATA_DIR,
                                      profile_name=REAL_PROFILE_NAME)
                stop_event = threading.Event()
                persistent_captcha_monitor(driver, pause_event, stop_event)

                if not safe_get(driver, "https://www.working-dog.com/"):
                    print("❌ Homepage open nahi ho saki. Ruk rahe hain.")
                    break

                perform_search(driver, pause_event)

                total_events, total_pages = get_total_events_and_pages(driver)
                if total_events:
                    print(f"ℹ️ Total {total_events} events mile — {total_pages} pages honge (10/page ke hisab se).")
                else:
                    print("⚠️ Total events count nahi mil saka - jab tak events milte rahenge scraping jari rahegi.")

                if page_num > 1:
                    print(f"⏩ Seedha page {page_num} par jump kar rahe hain...")
                    jumped = goto_page(driver, page_num, pause_event)
                    if not jumped:
                        print(f"⚠️ Page {page_num} tak jump nahi ho saka (shayad total pages se zyada hai). Ruk rahe hain.")
                        break

            if total_pages and page_num > total_pages:
                print("✅ Saare pages complete ho gaye.")
                break

            page_label = f"{page_num}/{total_pages}" if total_pages else f"{page_num}"
            print(f"\n📄 Scraping page {page_label} ...")

            wait_if_paused(pause_event)
            events = scrape_current_page_events(driver)

            if not events:
                print(f"✅ Page {page_num} par koi event nahi mila. Scraping khatam samjhi ja rahi hai.")
                break

            new_count = 0
            updated_count = 0
            for ev in events:
                changed, was_new = append_or_update_event(rows, by_uid, ev)
                if changed and was_new:
                    new_count += 1
                elif changed and not was_new:
                    updated_count += 1

            page_counter += 1
            write_all(rows, include_excel=(page_counter % EXCEL_WRITE_EVERY_N_PAGES == 0))

            print(f"✅ Page {page_num} se {len(events)} events mile ({new_count} naye, {updated_count} update hue, baaki pehle se sahi thay).")

            next_page = page_num + 1
            if total_pages and next_page > total_pages:
                print(f"🎯 Page {page_num} is complete. Ye aakhri page tha (total {total_pages}).")
                break

            moved = goto_next_page(driver, pause_event)
            if not moved:
                print(f"✅ Page {page_num} ke baad koi 'Next' button nahi mila. Scraping khatam samjhi ja rahi hai.")
                break

            print(f"➡️ Page {page_num} is complete now move to page {next_page}")
            page_num = next_page

            # 🆕 Har page ke baad kam se kam 10 second ka wait - taake site
            # par load na pade aur agla page reliably load ho.
            wait_time = random.uniform(10, 12)
            print(f"⏳ Agle page se pehle {wait_time:.1f}s wait kar rahe hain...")
            time.sleep(wait_time)

        except (InvalidSessionIdException, WebDriverException) as e:
            print(f"💥 Browser session crash ho gaya: {e}")
            print("💾 Ab tak scrape hua data CSV/Excel/JSON mein save kar rahe hain...")
            write_all(rows, include_excel=True)
            if stop_event is not None:
                stop_event.set()
            try:
                driver.quit()
            except Exception:
                pass
            driver = None
            print(f"🔁 Naya browser session start kar ke page {page_num} se dobara try kar rahe hain...")
            nap(2, 3)
            continue   # while loop dobara driver init karega aur page_num se resume karega

    write_all(rows, include_excel=True)
    if stop_event is not None:
        stop_event.set()
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass

    print(f"\n🎯 Scraping finished. Total records in database: {len(rows)}")


# ======================
# MAIN
# ======================

def main():
    continue_from = input(
        "➡️ Kis page number se scraping start karni hai? (blank chorne par page 1 se start hogi): "
    ).strip()
    continue_from = continue_from if continue_from else None

    scrape_all_events(continue_from=continue_from)


if __name__ == "__main__":
    main()