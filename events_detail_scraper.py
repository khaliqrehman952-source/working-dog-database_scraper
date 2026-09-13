import os
import re
import csv
import time
import random
import json
import threading
import requests
import urllib.parse as up
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

SOURCE_DIR = os.path.join(BASE_DIR, "working_dog_events")
SOURCE_JSON = os.path.join(SOURCE_DIR, "working_dog_events.json")
SOURCE_CSV = os.path.join(SOURCE_DIR, "working_dog_events.csv")

SOURCE_FIELD_MAP = {
    "Event URL": "URL",
    "UID": "UID",
    "Event Name": "Event Name",
    "Date From": "Date From",
    "Date Until": "Date Until",
    "Town": "Town",
}
SOURCE_FIELDS = list(SOURCE_FIELD_MAP.keys())

DATA_DIR = os.path.join(BASE_DIR, "working_dog_events_detail")
os.makedirs(DATA_DIR, exist_ok=True)

CSV_FILE = os.path.join(DATA_DIR, "working_dog_events_detail.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "working_dog_events_detail.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "working_dog_events_detail.json")
FAILED_LOG_FILE = os.path.join(DATA_DIR, "failed_events.txt")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "events_detail")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"
CHROME_MAIN_VERSION = 150

EXCEL_WRITE_EVERY_N = 5

# 🆕 "Your status" hata diya gaya hai (jaisa request kiya gaya tha)
DETAIL_FIELD_LABELS = [
    "Period", "Registration deadline", "Participants / attendees",
    "Participation fee", "Entry price", "Category", "Discipline", "breed",
    "Event website", "Street", "Post code / Town", "Organiser",
    "E-mail", "Telephone number",
]

FIELDNAMES = SOURCE_FIELDS + DETAIL_FIELD_LABELS + ["Event Information"]


# ======================
# BASIC HELPERS
# ======================
def nap(a=0.15, b=0.3):
    time.sleep(random.uniform(a, b))


def safe_get(driver, url, retries=3):
    for i in range(retries):
        try:
            driver.get(url)
            return True
        except Exception as e:
            print(f"⚠️ Attempt {i+1} failed: {e}")
            nap(1.5, 2.5)
    return False


def init_driver():
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")

    options.page_load_strategy = "eager"

    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)

    driver = uc.Chrome(options=options, use_subprocess=True, version_main=CHROME_MAIN_VERSION)
    driver.set_page_load_timeout(90)
    return driver


# ======================
# CAPTCHA
# ======================
def solve_turnstile(site_key, url, max_attempts=30, poll_interval=5):
    try:
        resp = requests.post(
            "http://2captcha.com/in.php",
            data={"key": CAPTCHA_KEY, "method": "turnstile", "sitekey": site_key, "pageurl": url, "json": 1},
            timeout=30,
        ).json()
        if resp.get("status") != 1:
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
    return None


def handle_captcha(driver):
    try:
        site_key = find_turnstile_sitekey(driver)
        if not site_key:
            return
        token = solve_turnstile(site_key, driver.current_url)
        if not token:
            return
        js = """
        function injectTurnstileToken(token) {
            function setToken() {
                try {
                    if (typeof turnstile !== 'undefined') {
                        var widgetEls = document.querySelectorAll('[data-sitekey]');
                        if (widgetEls.length > 0) {
                            widgetEls.forEach(function(el) {
                                var wid = turnstile.render(el, { sitekey: el.getAttribute('data-sitekey') });
                                if (turnstile.setResponse) turnstile.setResponse(wid, token);
                            });
                            return true;
                        }
                    }
                } catch(e) {}
                return false;
            }
            var ok = setToken();
            if (!ok) { var iv = setInterval(function(){ if (setToken()) clearInterval(iv); }, 1000); }
        }
        injectTurnstileToken(arguments[0]);
        """
        driver.execute_script(js, token)
        try:
            WebDriverWait(driver, 20).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='turnstile']"))
            )
        except Exception:
            pass
    except Exception as e:
        print("⚠️ handle_captcha error:", e)


def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    def _run():
        last_seen = False
        while not stop_event.is_set():
            try:
                if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                    if not last_seen:
                        print("🔍 CAPTCHA detect hua — pause + auto-solve try kar rahe hain...")
                        pause_event.set()
                        last_seen = True
                        try:
                            handle_captcha(driver)
                        except Exception as e:
                            print("⚠️ Auto-solve fail (manually solve karo):", e)
                    if not driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                        print("✅ CAPTCHA clear — resuming.")
                        pause_event.clear()
                        last_seen = False
                else:
                    if last_seen:
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
            print("⏸️ Verification active — isi event par ruk kar wait kar rahe hain...")
            first = False
        time.sleep(check_interval)


# ======================
# EMAIL DECODER (Cloudflare email-protection)
# ======================
def cf_decode_email(encoded):
    try:
        r = int(encoded[:2], 16)
        return "".join(
            chr(int(encoded[i:i + 2], 16) ^ r) for i in range(2, len(encoded), 2)
        )
    except Exception:
        return "N/A"


def extract_email_from_dd(dd_element):
    """
    🆕 FIXED: Ab poori page mein globally dhoondne ki bajaye SIRF is
    particular dd_element ke andar email dhoondte hain — aur 3 fallback
    tareeqon se try karte hain (jo bhi mil jaye):

    1) Cloudflare-protected span (data-cfemail hex encoded)
    2) Cloudflare ne pehle hi decode kar diya ho -> <a href="mailto:...">
    3) Plain visible text jisme "@" ho (rare fallback)

    Pehle wala function poori driver mein globally
    "span.__cf_email__" dhoondta tha jo galat/pehla-mila span pick kar
    leta tha ya kuch events par bilkul miss ho jata tha jab Cloudflare ne
    already decode kar diya hota tha - isi wajah se email N/A aa rahi thi.
    """
    # 1) Cloudflare-protected span
    try:
        span = dd_element.find_element(By.CSS_SELECTOR, "span.__cf_email__")
        enc = span.get_attribute("data-cfemail")
        if enc:
            decoded = cf_decode_email(enc)
            if decoded and decoded != "N/A":
                return decoded
    except Exception:
        pass

    # 2) Already-decoded mailto link
    try:
        link = dd_element.find_element(By.CSS_SELECTOR, "a[href*='mailto:']")
        href = link.get_attribute("href") or ""
        if "mailto:" in href:
            email = href.split("mailto:", 1)[1].strip()
            email = email.split("?")[0].strip()
            if email:
                return email
    except Exception:
        pass

    # 3) Plain visible text fallback
    try:
        txt = (dd_element.get_attribute("textContent") or "").strip()
        if txt and "@" in txt:
            return txt
    except Exception:
        pass

    return "N/A"


# ======================
# EVENT DATASHEET EXTRACTOR (dl#eventData)
# ======================
def extract_event_datasheet(driver):
    data = {}
    try:
        dl = driver.find_element(By.CSS_SELECTOR, "dl#eventData")
        dts = dl.find_elements(By.TAG_NAME, "dt")
        dds = dl.find_elements(By.TAG_NAME, "dd")

        for dt, dd in zip(dts, dds):
            label = (dt.get_attribute("textContent") or "").strip().rstrip(":")
            if not label:
                continue

            low = label.lower()

            if low == "e-mail":
                value = extract_email_from_dd(dd)   # 🆕 dd-specific extraction

            elif low.startswith("participants"):
                try:
                    value = (dd.find_element(By.TAG_NAME, "a").get_attribute("textContent") or "").strip()
                except Exception:
                    value = (dd.get_attribute("textContent") or "").strip()

            elif low.startswith("event website"):
                try:
                    value = dd.find_element(By.TAG_NAME, "a").get_attribute("href") or "N/A"
                except Exception:
                    value = (dd.get_attribute("textContent") or "").strip()

            else:
                value = (dd.get_attribute("textContent") or "").strip()

            value = re.sub(r"\s+", " ", value).strip() if value else "N/A"
            data[label] = value if value else "N/A"

    except Exception as e:
        print("⚠️ eventData extract error:", e)

    return data


def extract_event_information(driver):
    try:
        headers = driver.find_elements(By.TAG_NAME, "h2")
        for h in headers:
            htext = (h.get_attribute("textContent") or "").strip().lower()
            if "event information" in htext:
                section = h.find_element(By.XPATH, "./ancestor::section[1]")
                container = section.find_element(By.CSS_SELECTOR, "div.container")
                inner_html = container.get_attribute("innerHTML") or ""

                text = re.sub(r"<br\s*/?>", "\n", inner_html, flags=re.I)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"[ \t]+", " ", text)
                text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
                return text.strip() if text.strip() else "N/A"
    except Exception as e:
        print("⚠️ event information extract error:", e)
    return "N/A"


# ======================
# SCRAPE ONE EVENT
# ======================
def scrape_event_profile(driver, url, source_meta, error_holder=None):
    try:
        if not safe_get(driver, url):
            return None

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "dl#eventData"))
        )

        datasheet = extract_event_datasheet(driver)
        event_info = extract_event_information(driver)

        row = {}
        for f in SOURCE_FIELDS:
            row[f] = source_meta.get(f, "N/A")

        for label in DETAIL_FIELD_LABELS:
            row[label] = datasheet.get(label, "N/A")

        row["Event Information"] = event_info
        return row

    except Exception as e:
        msg = str(e)
        print("⚠️ scrape_event_profile error:", msg)
        if error_holder is not None:
            error_holder.append(msg)
        return None


def scrape_event_with_captcha_retry(driver, url, source_meta, pause_event, max_retries=3):
    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        wait_if_paused(pause_event)
        error_holder = []
        try:
            row = scrape_event_profile(driver, url, source_meta, error_holder=error_holder)
        except (InvalidSessionIdException, WebDriverException):
            raise
        except Exception as e:
            row = None
            error_holder.append(str(e))

        if row:
            remove_from_failed_log(url)
            return row

        last_error = error_holder[0] if error_holder else "scrape_event_profile returned None"
        print(f"⚠️ Attempt {attempt}/{max_retries} failed for {url}: {last_error}")
        wait_if_paused(pause_event)
        nap(1.0, 1.8)

    event_name = source_meta.get("Event Name", "N/A")
    log_failed(url, event_name, last_error)
    return None


# ======================
# SOURCE FILE LOADER (JSON priority, CSV fallback)
# ======================
def load_source_events():
    events = []
    raw_rows = []

    if os.path.exists(SOURCE_JSON):
        try:
            with open(SOURCE_JSON, "r", encoding="utf-8") as f:
                raw_rows = json.load(f)
            print(f"ℹ️ Source JSON se load kiya: {SOURCE_JSON}")
        except Exception as e:
            print("⚠️ Source JSON load karne mein masla:", e)

    elif os.path.exists(SOURCE_CSV):
        try:
            with open(SOURCE_CSV, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                raw_rows = list(reader)
            print(f"ℹ️ Source CSV se load kiya: {SOURCE_CSV}")
        except Exception as e:
            print("⚠️ Source CSV load karne mein masla:", e)

    else:
        print(f"❌ Source file nahi mili: {SOURCE_JSON} ya {SOURCE_CSV}")
        return events

    for row in raw_rows:
        mapped = {}
        for canonical, actual_header in SOURCE_FIELD_MAP.items():
            val = row.get(actual_header, "N/A")
            val = str(val).strip() if val else "N/A"
            mapped[canonical] = val if val else "N/A"
        events.append(mapped)

    return events


# ======================
# OUTPUT FILE UTILITIES
# ======================
def load_existing_detail_data():
    rows = []
    by_url = {}
    if os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for k in FIELDNAMES:
                        if k not in row:
                            row[k] = "N/A"
                    # 🆕 agar purani row mein "Your status" jaisa extra
                    # column tha, wo yahan simply ignore ho jayega (hum
                    # sirf FIELDNAMES ki keys rakhte hain)
                    row = {k: row.get(k, "N/A") for k in FIELDNAMES}
                    rows.append(row)
                    url = row.get("Event URL")
                    if url:
                        by_url[url] = row
        except Exception:
            pass
    return rows, by_url


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


def append_or_update_row_in_memory(rows, by_url, new_row):
    url = (new_row.get("Event URL") or "").strip()
    if not url:
        return False

    changed = False

    if url in by_url:
        existing = by_url[url]
        for k in FIELDNAMES:
            if k == "Event URL":
                continue
            new_val = (new_row.get(k) or "").strip() or "N/A"
            existing_val = (existing.get(k) or "").strip() or "N/A"
            if new_val != "N/A" and new_val != existing_val:
                existing[k] = new_val
                changed = True
    else:
        clean_row = {}
        for k in FIELDNAMES:
            val = (new_row.get(k) or "").strip()
            clean_row[k] = val if val else "N/A"
        clean_row["Event URL"] = url
        rows.append(clean_row)
        by_url[url] = clean_row
        changed = True

    return changed


# ======================
# FAILED LOG
# ======================
def log_failed(url, event_name, reason="Unknown error"):
    try:
        if os.path.exists(FAILED_LOG_FILE):
            with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
                existing_content = f.read()
            if url in existing_content:
                return

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(FAILED_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {event_name} | {url}\n")
        print(f"📝 Failed event failed_events.txt mein likh diya: {event_name} | {url}")
    except Exception as e:
        print("⚠️ failed_events.txt likhne mein masla:", e)


def remove_from_failed_log(url):
    if not os.path.exists(FAILED_LOG_FILE):
        return
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        kept_lines = [ln for ln in lines if url not in ln]

        if len(kept_lines) != len(lines):
            with open(FAILED_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(kept_lines)
            print(f"🧹 {url} scrape ho gaya — failed_events.txt se hata diya.")
    except Exception as e:
        print("⚠️ failed_events.txt se entry hatane mein masla:", e)


def parse_failed_log():
    entries = []
    seen = set()
    if not os.path.exists(FAILED_LOG_FILE):
        return entries
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                try:
                    after_ts = line.split("]", 1)[1].strip()
                except IndexError:
                    after_ts = line
                name_part, url_part = after_ts.rsplit("|", 1)
                name_part = name_part.strip()
                url_part = url_part.strip()
                if url_part and url_part not in seen:
                    seen.add(url_part)
                    entries.append((name_part, url_part))
    except Exception as e:
        print("⚠️ failed_events.txt parse karne mein masla:", e)
    return entries


# ======================
# RANGE SCRAPER
# ======================
def scrape_range(driver, items, rows, by_url, pause_event, source="main"):
    total = len(items)
    counter = 0

    for idx, item in enumerate(items, start=1):
        wait_if_paused(pause_event)

        if source == "main":
            source_meta = item
            url = item.get("Event URL", "N/A")
            name = item.get("Event Name", "N/A")
        else:
            name, url = item
            source_meta = {"Event URL": url, "Event Name": name}
            if url in by_url:
                for f in SOURCE_FIELDS:
                    source_meta.setdefault(f, by_url[url].get(f, "N/A"))

        if not url or url == "N/A":
            print(f"⏭️ Skip [{idx}/{total}]: URL missing")
            continue

        t_start = time.time()
        print(f"🔎 [{idx}/{total}] Scraping: {name} | {url}")

        try:
            updated = scrape_event_with_captcha_retry(driver, url, source_meta, pause_event)
        except (InvalidSessionIdException, WebDriverException) as e:
            print(f"💥 Browser session crash ho gaya: {e}")
            print("💾 Ab tak scrape hua data save kar rahe hain...")
            write_all(rows, include_excel=True)
            raise

        if not updated:
            print(f"⚠️ Scrape fail: {name} — failed_events.txt mein add ho gaya")
            continue

        append_or_update_row_in_memory(rows, by_url, updated)
        counter += 1
        write_all(rows, include_excel=(counter % EXCEL_WRITE_EVERY_N == 0))

        elapsed = time.time() - t_start
        print(f"   ✅ Done in {elapsed:.1f}s")
        nap()

    write_all(rows, include_excel=True)
    print(f"🎯 Range scraping complete. Total processed: {counter}/{total}")


# ======================
# MAIN
# ======================
def main():
    driver = init_driver()
    pause_event = threading.Event()
    stop_event = threading.Event()
    persistent_captcha_monitor(driver, pause_event, stop_event)

    try:
        if not safe_get(driver, "https://www.working-dog.com/"):
            print("❌ Homepage open nahi ho saki")
            return

        print("🔑 Browser open ho gaya. Agar CAPTCHA aaye to monitor khud pause/solve karega.")

        rows, by_url = load_existing_detail_data()
        print(f"ℹ️ Existing detail records: {len(rows)}")

        while True:
            print("\n--- MENU ---")
            print("1) Source file (working_dog_events) se range scrape karo")
            print("2) Failed events (failed_events.txt) se range scrape karo")
            print("3) Exit")

            choice = input("👉 Select option: ").strip()

            if choice == "1":
                source_events = load_source_events()
                total = len(source_events)
                if total == 0:
                    print("⚠️ Source file khaali hai ya nahi mili.")
                    continue

                print(f"📊 Source file mein {total} events hain.")
                try:
                    s = int(input(f"➡️ Starting index (1-based, 1 to {total}): ").strip())
                    e = int(input(f"➡️ Ending index (1-based inclusive, 1 to {total}): ").strip())
                except ValueError:
                    print("⚠️ Invalid input.")
                    continue

                start_idx = max(0, s - 1)
                end_idx = min(e - 1, total - 1)
                if start_idx > end_idx:
                    print("⚠️ Invalid range.")
                    continue

                selected = source_events[start_idx:end_idx + 1]
                scrape_range(driver, selected, rows, by_url, pause_event, source="main")

            elif choice == "2":
                failed_entries = parse_failed_log()
                total = len(failed_entries)
                if total == 0:
                    print("✅ failed_events.txt khaali hai — koi failed event nahi.")
                    continue

                print(f"📊 failed_events.txt mein {total} unique events hain.")
                try:
                    s = int(input(f"➡️ Starting index (1-based, 1 to {total}): ").strip())
                    e = int(input(f"➡️ Ending index (1-based inclusive, 1 to {total}): ").strip())
                except ValueError:
                    print("⚠️ Invalid input.")
                    continue

                start_idx = max(0, s - 1)
                end_idx = min(e - 1, total - 1)
                if start_idx > end_idx:
                    print("⚠️ Invalid range.")
                    continue

                selected = failed_entries[start_idx:end_idx + 1]
                scrape_range(driver, selected, rows, by_url, pause_event, source="failed")

            elif choice == "3":
                print("👋 Exiting.")
                break

            else:
                print("⚠️ Invalid option.")

    finally:
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()