"""
working-dog.com Kennel Scraper (v2 - with CAPTCHA bypass + interactive breed input)
=====================================================================================
Har breed ke liye https://www.working-dog.com/breedstation/overview page se
kennel names scrape karta hai. Breed ka naam RUN TIME par pucha jata hai
(hardcoded nahi hai) — jitni breeds chaho utni ek ek karke likh sakte ho.

Isme wahi CAPTCHA-bypass system hai jo tumhare dusre scraper mein tha:
    - undetected_chromedriver (bot-detection se bachne ke liye)
    - Background monitor jo Turnstile CAPTCHA detect karke scraper ko pause karta hai
    - Manual solve karne ka mauka deta hai, phir khud resume ho jata hai
    - 2captcha auto-solve function (handle_captcha) bhi maujood hai agar automate karna ho

Zaroorat:
    pip install undetected-chromedriver selenium requests

Chalane ka tareeqa:
    python kennel_scraper.py
"""

import os
import re
import csv
import time
import random
import json
import threading
import urllib.parse as up

import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ============================== CONFIG ==============================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "kennel_data")
os.makedirs(DATA_DIR, exist_ok=True)

TXT_FILE = os.path.join(DATA_DIR, "breed_kennels.txt")
CSV_FILE = os.path.join(DATA_DIR, "breed_kennels.csv")
JSON_FILE = os.path.join(DATA_DIR, "breed_kennels.json")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "kennel_scraper")
os.makedirs(PROFILE_DIR, exist_ok=True)

BASE_URL = "https://www.working-dog.com/breedstation/overview"

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"   # 2captcha API key (agar auto-solve chahiye)

MIN_DELAY = 1.2
MAX_DELAY = 2.5

# =====================================================================


def nap(a=MIN_DELAY, b=MAX_DELAY):
    time.sleep(random.uniform(a, b))


# ======================
# CAPTCHA MONITOR (tumhare dusre scraper wala hi system)
# ======================

def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    """
    Background thread: sirf iframe[src*='turnstile'] detect karta hai.
    CAPTCHA aaye -> pause_event.set() (scraper ruk jayega)
    CAPTCHA gayab ho -> pause_event.clear() (scraper khud resume hoga)
    """
    def _run():
        last_seen = False
        while not stop_event.is_set():
            try:
                if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                    if not last_seen:
                        print("🔍 Turnstile CAPTCHA detect hua — scraper pause ho raha hai. Browser mein manually solve karo.")
                        pause_event.set()
                        last_seen = True
                else:
                    if last_seen:
                        print("✅ CAPTCHA gayab ho gaya — scraper resume ho raha hai.")
                        pause_event.clear()
                        last_seen = False
            except Exception as e:
                print("⚠️ CAPTCHA monitor error (ignore kar rahe hain):", repr(e))
            time.sleep(poll_interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_if_paused(pause_event):
    """Jab tak CAPTCHA pause active hai, yahin ruko"""
    while pause_event and pause_event.is_set():
        print("⏸️ Scraper paused (CAPTCHA maujood hai). Browser mein manually solve karo. Wait kar rahe hain...")
        time.sleep(3)


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


def solve_turnstile(site_key, url, max_attempts=30, poll_interval=5):
    """2captcha service ke zariye Turnstile CAPTCHA solve karta hai (paid service - API key chahiye)"""
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


def handle_captcha(driver):
    """
    Optional: agar chaho to CAPTCHA ko khud-b-khud 2captcha se solve karwa sakte ho.
    Manual solve karna hi zyada reliable/free hai - ye function sirf zaroorat par use karo.
    """
    try:
        site_key = find_turnstile_sitekey(driver)
        if not site_key:
            print("❌ Sitekey nahi mila.")
            return
        print(f"🔐 CAPTCHA solve ho raha hai (sitekey: {site_key})")
        token = solve_turnstile(site_key, driver.current_url)
        if not token:
            print("❌ CAPTCHA solve nahi ho saka.")
            return
        print("✅ Token mil gaya — inject kar rahe hain...")
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
            print("✅ CAPTCHA verify ho gaya.")
        except Exception:
            print("⚠️ iframe abhi bhi present hai.")
    except Exception as e:
        print("⚠️ handle_captcha error:", e)


# ======================
# DRIVER INIT (undetected_chromedriver - bot detection se bachne ke liye)
# ======================

CHROME_MAIN_VERSION = 150   # <-- apne Chrome ka MAIN version yahan likho
                            # (chrome://settings/help mein dekho, e.g. "150.0.7871.125" -> 150)


def init_driver():
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    # version_main dene se uc.Chrome tumhare installed Chrome ke EXACT matching
    # driver version download karega - "session not created" error isi se aata hai
    driver = uc.Chrome(options=options, use_subprocess=True, version_main=CHROME_MAIN_VERSION)
    driver.set_page_load_timeout(120)
    return driver


# ======================
# COOKIE CONSENT (banner clicks ko intercept karta hai - isko hatana zaroori hai)
# ======================

def dismiss_cookie_consent(driver):
    """
    Cookie-consent popup ko band karta hai. Pehle asli 'accept' button dhoondta hai,
    agar na mile to JS se zabardasti modal/backdrop hata deta hai taake wo
    aage koi bhi click intercept na kare.
    """
    selectors = [
        "#cookie-consent__modal button",
        ".cookie-consent__accept",
        ".cookie-consent button",
        "button[id*='accept']",
        "button[class*='accept']",
        "button[class*='consent']",
    ]
    for sel in selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                btn.click()
                time.sleep(0.5)
                return
        except Exception:
            continue

    # Fallback: JS se zabardasti hata do (accept button na mile tab bhi kaam chalega)
    try:
        driver.execute_script("""
            var modal = document.getElementById('cookie-consent__modal');
            if (modal) { modal.parentNode.removeChild(modal); }
            document.querySelectorAll("[class*='cookie-consent']").forEach(function(el){
                el.remove();
            });
        """)
    except Exception:
        pass


# ======================
# SEARCH + SCRAPE FUNCTIONS
# ======================

def wait_for_search_button_ready(driver, timeout=8):
    """
    Search button ke andar total kennels ka number hota hai (jaise '2278').
    Breed select karne ke baad ye AJAX se update hota hai. Hum thora wait karte
    hain jab tak ye number 2 baar consecutively same na aa jaye - matlab update
    ho chuka hai, ab sahi count ke sath click karna safe hai.
    """
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.ID, "search_button"))
        )
    except TimeoutException:
        return

    last_val = None
    stable_count = 0
    start = time.time()
    while time.time() - start < timeout:
        try:
            current_val = btn.text.strip()
        except Exception:
            current_val = None
        if current_val and current_val == last_val:
            stable_count += 1
            if stable_count >= 2:
                return
        else:
            stable_count = 0
        last_val = current_val
        time.sleep(0.3)


def search_breed(driver, wait, breed_name, pause_event):
    """
    1) Overview page kholta hai
    2) Cookie consent banner hata deta hai (agar aaye)
    3) Breed input mein har letter alag se type karta hai
    4) Site ki apni dropdown (.ddlistbox.breedlist) se TOP wala row select karta hai
    5) Search button ka number stabilize hone ka wait karta hai, phir click karta hai
    """
    wait_if_paused(pause_event)
    driver.get(BASE_URL)
    nap(0.8, 1.2)
    dismiss_cookie_consent(driver)

    breed_input = wait.until(EC.presence_of_element_located((By.ID, "raceTextField")))
    breed_input.clear()

    for ch in breed_name:
        breed_input.send_keys(ch)
        time.sleep(0.1)

    # Site ki asli dropdown: <div class="ddlistbox ddlistboxinit breedlist"> ke andar
    # har breed ek <div class="ddlistrow clickable"> hoti hai. Hamesha TOP wali row
    # select karni hai (jo tumne bataya).
    try:
        first_option = WebDriverWait(driver, 6).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, ".ddlistbox.breedlist .ddlistrow")
            )
        )
        first_option.click()
    except TimeoutException:
        # Dropdown nahi khula - is breed ko GALAT search karne se behtar hai
        # error raise kar dein taake saari (sab breeds ka mixed) data scrape na ho
        raise RuntimeError(
            f"'{breed_name}' ke liye dropdown nahi khula - breed ka spelling check karo "
            f"ya dubara try karo (typing speed / internet slow ho sakta hai)"
        )

    nap(0.5, 1.0)
    wait_if_paused(pause_event)
    dismiss_cookie_consent(driver)   # dobara check - kabhi kabhi banner der se aata hai

    # Breed select hone ke baad button ka number update hone do (warna ghalat/purana
    # count use hoke saari site ke kennels scrape ho sakte hain)
    wait_for_search_button_ready(driver)

    driver.find_element(By.ID, "search_button").click()

    wait.until(EC.presence_of_element_located((By.ID, "breedList")))
    nap()


def set_page_size_80(driver, wait):
    try:
        select_el = wait.until(EC.presence_of_element_located((By.NAME, "listitems")))
        Select(select_el).select_by_value("80")
        nap()
        wait.until(EC.presence_of_element_located((By.ID, "breedList")))
    except (NoSuchElementException, TimeoutException):
        print("  [warning] listitems dropdown nahi mila - default page size use ho rahi hai")


def get_total_pages(driver):
    try:
        page_input = driver.find_element(By.CSS_SELECTOR, "form[name='current_page'] input[type='submit']")
        value = page_input.get_attribute("value")   # e.g. "1 / 63"
        return int(value.split("/")[-1].strip())
    except (NoSuchElementException, ValueError, IndexError):
        return 1


def scrape_current_page(driver):
    kennels = []
    rows = driver.find_elements(By.CSS_SELECTOR, "#breedList tbody tr")
    for row in rows:
        try:
            name_el = row.find_element(By.CSS_SELECTOR, "dl.datasheet a.greenHighlight")
            name = name_el.text.strip()
            if name:
                kennels.append(name)
        except NoSuchElementException:
            continue   # advertising row - skip
    return kennels


def go_to_next_page(driver, wait, pause_event):
    wait_if_paused(pause_event)
    dismiss_cookie_consent(driver)
    try:
        next_btn = driver.find_element(By.CSS_SELECTOR, "input.rightArrow[value='next']")
        next_btn.click()
        wait.until(EC.presence_of_element_located((By.ID, "breedList")))
        return True
    except NoSuchElementException:
        return False


def scrape_breed(driver, wait, breed_name, pause_event, all_data):
    """
    all_data yahan pass hota hai taake HAR PAGE scrape hone ke baad
    turant save_results() call karke file update ki ja sake.
    """
    print(f"\n=== Scraping breed: {breed_name} ===")
    search_breed(driver, wait, breed_name, pause_event)
    set_page_size_80(driver, wait)

    total_pages = get_total_pages(driver)
    print(f"  Total pages: {total_pages}")

    all_kennels = []
    for page_num in range(1, total_pages + 1):
        wait_if_paused(pause_event)
        print(f"  Page {page_num}/{total_pages} ...", end=" ")
        page_kennels = scrape_current_page(driver)
        all_kennels.extend(page_kennels)
        print(f"{len(page_kennels)} kennels mile")

        # ✅ har page ke baad turant save - duplicates hata kar
        all_data[breed_name] = list(dict.fromkeys(all_kennels))
        save_results(all_data)

        if page_num < total_pages:
            if not go_to_next_page(driver, wait, pause_event):
                print("  [warning] next page nahi mila, ruk raha hoon")
                break
            nap()

    unique_kennels = list(dict.fromkeys(all_kennels))
    print(f"  '{breed_name}' complete: {len(unique_kennels)} unique kennels")
    return unique_kennels


# ======================
# SAVE / LOAD (breed-wise data persist hota hai, purana data delete nahi hota)
# ======================

def load_existing():
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_results(all_data):
    with open(TXT_FILE, "w", encoding="utf-8") as f:
        for breed, kennels in all_data.items():
            f.write(f"Breed: {breed}\n")
            for i, k in enumerate(kennels, start=1):
                f.write(f"{i}. {k}\n")
            f.write("\n")

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Breed", "Kennel Name"])
        for breed, kennels in all_data.items():
            for k in kennels:
                writer.writerow([breed, k])

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Saved:\n  {TXT_FILE}\n  {CSV_FILE}\n  {JSON_FILE}")


# ======================
# MAIN
# ======================

def main():
    driver = init_driver()
    wait = WebDriverWait(driver, 20)

    pause_event = threading.Event()   # CAPTCHA hone par set hota hai
    stop_event = threading.Event()    # program band hone par set hota hai
    persistent_captcha_monitor(driver, pause_event, stop_event)

    all_data = load_existing()
    print(f"ℹ️ Pehle se {len(all_data)} breeds ka data mojood hai.")

    try:
        driver.get("https://www.working-dog.com/")
        nap(1, 2)
        dismiss_cookie_consent(driver)
        print("🔑 Browser khul gaya. Agar CAPTCHA aaye to scraper khud pause hoke tumhe manually solve karne ka mauka dega.")

        while True:
            breed_name = input("\n👉 Breed ka naam likho (band karne ke liye 'exit' likho): ").strip()

            if breed_name.lower() == "exit":
                break
            if not breed_name:
                print("⚠️ Khali naam - dubara likho.")
                continue

            try:
                kennels = scrape_breed(driver, wait, breed_name, pause_event, all_data)
                all_data[breed_name] = kennels
            except Exception as e:
                print(f"  [ERROR] '{breed_name}' scrape karte hue masla aaya: {e}")
            finally:
                save_results(all_data)   # confirm final save bhi ho jaye

        print("👋 Scraper band ho raha hai.")

    finally:
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()