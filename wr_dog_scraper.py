import os
import re
import csv
import time
import random
import requests
import json
import urllib.parse as up
import shutil
import pandas as pd
import threading
import urllib



import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, InvalidSessionIdException


# ======================
# CONFIG
# ======================
import os

# Base folder of this script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ Naya folder for data
DATA_DIR = os.path.join(BASE_DIR, "working_dog_data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ✅ File paths inside working_dog_data folder
CSV_FILE = os.path.join(DATA_DIR, "working_dogs_data.csv")
EXCEL_FILE = os.path.join(DATA_DIR, "working_dogs_data.xlsx")
JSON_FILE = os.path.join(DATA_DIR, "working_dogs_data.json")
FAILED_LOG_FILE = os.path.join(DATA_DIR, "failed.txt")   # jo dogs scrape nahi ho paate unki list


PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "working_dog")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

REAL_USER_DATA_DIR = None   # e.g. r"C:\Users\<YOU>\AppData\Local\Google\Chrome\User Data"
REAL_PROFILE_NAME = "Default"

# Har kitne dogs ke baad Excel file dobara likhein (Excel likhna sabse slow
# operation hai jab dataset bada ho jaye - CSV/JSON hamesha turant save hote
# hain, Excel sirf har N dogs ke baad - taake speed barh jaye lekin data
# CSV/JSON mein kabhi miss na ho)
EXCEL_WRITE_EVERY_N_DOGS = 5

# Site per page kitne dogs dikhati hai (pagination math isi se hoti hai)
DOGS_PER_PAGE = 240


def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    """
    Background monitor: detect karta hai iframe[src*='turnstile'].
    Agar iframe ajaye -> pause_event.set() (scraper pause ho jata hai) AUR
    khud-b-khud 2captcha se solve karne ki koshish karta hai (handle_captcha).
    Agar auto-solve kaam kar jaye -> pause_event.clear() (scraper resume).
    Agar auto-solve fail ho jaye -> pause_event set hi rehta hai, taake
    manually browser mein solve kiya ja sake; scraper tab tak agle dog
    par move nahi hota (scrape_dog_with_captcha_retry / wait_if_paused dekho).
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

                    # auto-solve ke baad (ya agar wo already gaya ho) dobara check karo
                    if not driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                        print("✅ CAPTCHA clear ho gaya — scraper resume ho raha hai.")
                        pause_event.clear()
                        last_seen = False
                    # warna pause_event set hi rehta hai - user manually solve karega,
                    # aur agla loop-cycle usko detect kar lega (neeche wala else block)
                else:
                    if last_seen:
                        print("✅ Turnstile iframe gone — resuming scraper.")
                        pause_event.clear()
                        last_seen = False
            except Exception as e:
                # don't crash monitor thread on occasional driver errors
                print("⚠️ CAPTCHA monitor error (ignored):", repr(e))
            time.sleep(poll_interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_if_paused(pause_event, check_interval=2):
    """
    Jab tak CAPTCHA/verification active hai (pause_event set), yahin ruko -
    agle dog par bilkul move nahi hote. Verification clear hote hi (khud-b-khud
    ya manually) turant aage barhte hain.
    """
    first = True
    while pause_event and pause_event.is_set():
        if first:
            print("⏸️ Verification active hai — isi dog par ruk kar wait kar rahe hain jab tak clear na ho...")
            first = False
        time.sleep(check_interval)



# ======================
# HELPERS
# ======================

def nap(a=0.5, b=1.0):
    time.sleep(random.uniform(a, b))

def safe_text(driver, xpath, default="N/A"):
    try:
        return driver.find_element(By.XPATH, xpath).text.strip()
    except Exception:
        return default

def extract_uid_from_url(url):
    """Extract numeric U-ID from working-dog profile URL."""
    try:
        if not url or url == "N/A":
            return "N/A"
        m = re.search(r"/dogs-details/(\d+)", url)
        if m:
            return m.group(1)
    except:
        pass
    return "N/A"


# ======================
# FAST FIELD LOOKUP (Dog Information table)
# ======================

def get_dog_info_field(driver, xpath, default="N/A"):
    """
    Dog Information table ke fields (Breed, Nickname, Variety, wagera) ke liye.

    IMPORTANT (speed fix): Ye function WebDriverWait/polling BILKUL use nahi
    karta. Jab tak hum ye function call karte hain, page pehle hi poori tarah
    load ho chuki hoti hai (wait_for_page_ready + h1 wait ke baad) - is liye
    agar field DOM mein maujood hai to WAPAS TURANT mil jayegi, aur agar
    maujood nahi hai to bhi TURANT pata chal jayega (bina 8-16 second wait
    kiye).
    """
    try:
        el = driver.find_element(By.XPATH, xpath)
        txt = (el.text or "").strip()
        if not txt:
            # kabhi kabhi .text khali hota hai agar element abhi visible
            # nahi hua - textContent se try karo (visibility independent)
            txt = (el.get_attribute("textContent") or "").strip()
        if txt:
            if txt.lower() == "no data":
                return default
            return txt
    except Exception:
        pass
    return default


# ======================
# CAPTCHA SOLVER
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
        except:
            print("⚠️ iframe still present — site may need API call or profile tweak.")

    except Exception as e:
        print("⚠️ handle_captcha error:", e)

# ======================
# DRIVER INIT
# ======================

CHROME_MAIN_VERSION = 150   # <-- apne Chrome ka MAIN version yahan likho
                            # (chrome://settings/help mein dekho)

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

# ======================
# CLEANER (fallback filtering - kept for backward compatibility / fallback use)
# ======================

def clean_owner_breeder_text(text):
    if not text:
        return "N/A"
    txt = text.replace('\xa0', ' ').strip()
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return "N/A"

    junk_indicators = [
        "you are the owner",
        "add owner now",
        "invite a friend",
        "hasn’t registered",
        "hasn't registered",
        "give information",
        "possibility to give",
        "you can invite",
        "register with working-dog",
        "log in to",
        "click here",
        "add breeder",
        "add owner",
        "invite him",
        "working-dog",
        "owner details",
        "breeder details",
        "you are the breeder or you know him?",
        "no data",
        "diversity traits"
    ]

    header_indicators = set([
        "owner", "owner:", "breeder", "breeder:",
        "breeder association", "owner association",
        "owner association:", "breeder association:"
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
    """Extract Date of Birth and Date of Death (if available, and filter out age info)."""
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
            # Agar 'year' word hai to ye sirf age hai, DOD nahi
            if second_val and not re.search(r'year', second_val, re.I):
                # Agar dd.mm.yyyy format hai to hi DOD assign karein
                if re.match(r'\d{2}\.\d{2}\.\d{4}', second_val):
                    dod = second_val
    except:
        pass

    return dob, dod


# ======================
# ROBUST OWNER/BREEDER EXTRACTOR (multi-owner / multi-breeder support)
# ======================

def extract_owner_or_breeder_multi(driver, role="owner"):
    """
    Extract ALL owner(s) or breeder(s) name(s) from the profile tabs.
    If there is only 1 owner/breeder -> returns that 1 name.
    If there are 2+ owners/breeders  -> returns "Name1, Name2".
    """
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
    Pedigree tree (1st generation) se Sire (father) aur Dam (mother) ka
    naam + URL + litter-date (DOB) nikalta hai. .text ke bajaye textContent
    use hota hai (visibility-independent, isliye naam kabhi khaali nahi aata).
    """
    sire_name, dam_name = "N/A", "N/A"
    sire_url, dam_url = "N/A", "N/A"
    sire_dob, dam_dob = "N/A", "N/A"

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

                dob_val = "N/A"
                try:
                    container = p.find_element(
                        By.XPATH, "./ancestor::div[contains(@class,'rows')][1]"
                    )
                    litter_el = container.find_element(By.CSS_SELECTOR, ".gen-0__dog-litter span")
                    dob_val = (litter_el.get_attribute("textContent") or "").strip() or "N/A"
                except Exception:
                    pass

                if "mdi-gender-male" in gender_class:
                    sire_name = name if name else "N/A"
                    sire_url = link
                    sire_dob = dob_val
                elif "mdi-gender-female" in gender_class:
                    dam_name = name if name else "N/A"
                    dam_url = link
                    dam_dob = dob_val
            except Exception:
                continue
    except Exception:
        pass

    return (sire_name, sire_url, sire_dob), (dam_name, dam_url, dam_dob)


def robust_safe_text(driver, xpath, default="N/A", retries=2, timeout=8):
    """
    Ye function ab sirf un jagah use hoti hai jaha element GENUINELY
    asynchronously load ho sakta hai. Dog-Information table ke static
    fields ke liye get_dog_info_field() use karo (fast).
    """
    for i in range(retries):
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            txt = el.text.strip()
            if txt:
                return txt
        except:
            pass
        nap(0.4, 0.7)
    return default


def wait_for_page_ready(driver, timeout=15):
    """Wait until JS ready + chhota sa buffer."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    nap(0.3, 0.5)


def scroll_page_fast(driver):
    """4 tez JS jumps (~1 second) - lazy-loading trigger karne ke liye."""
    try:
        total_height = driver.execute_script("return document.body.scrollHeight") or 3000
        steps = 4
        for i in range(1, steps + 1):
            y = int(total_height * i / steps)
            driver.execute_script(f"window.scrollTo(0, {y});")
            time.sleep(0.25)
    except Exception:
        pass


# ======================
# HEALTH TABLE EXTRACTOR (4 tabs: VetResults, LabResults, Body Traits, Diversity Traits)
# ======================

def _clean_ws(text):
    """Collapse whitespace/newlines into single spaces."""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


def extract_health_data(driver):
    """
    Health table has 4 tabs, har tab link ka data-items-count attribute
    batata hai kitne items hain. 0 hone par kuch bhi extract nahi karte
    (yahi wo jagah hai jaha promotional junk text hoti hai).
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


def scrape_dog_profile(driver, url, rows, by_url, error_holder=None):
    try:
        if not safe_get(driver, url):
            return None

        wait_for_page_ready(driver)

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, "//h1"))
        )

        scroll_page_fast(driver)

        owner_name = extract_owner_or_breeder_multi(driver, role="owner")
        breeder_name = extract_owner_or_breeder_multi(driver, role="breeder")
        (sire_name, sire_url, sire_dob), (dam_name, dam_url, dam_dob) = get_sire_dam(driver)
        dob, dod = extract_dob_dod(driver)

        sire_id = extract_uid_from_url(sire_url)
        dam_id = extract_uid_from_url(dam_url)

        health_data = extract_health_data(driver)

        nickname = get_dog_info_field(driver, "//div[@title='Nickname']/following-sibling::div")
        breeders_association = get_dog_info_field(
            driver, "//div[contains(@title,'Breeders') and contains(@title,'association')]/following-sibling::div"
        )
        result_field = get_dog_info_field(driver, "//div[@title='Result']/following-sibling::div")
        working_cert = get_dog_info_field(driver, "//div[@title='Working cert.']/following-sibling::div")
        second_pedigree_number = get_dog_info_field(driver, "//div[@title='2nd Pedigree number']/following-sibling::div")

        import urllib.parse

        dog_name = "N/A"
        try:
            h1_el = driver.find_element(By.TAG_NAME, "h1")
            dog_name = (h1_el.get_attribute("textContent") or "").strip()
        except:
            pass

        if not dog_name or dog_name == "N/A":
            try:
                dog_name = driver.title.strip()
                if " - working-dog" in dog_name:
                    dog_name = dog_name.replace(" - working-dog", "").strip()
            except:
                pass

        if not dog_name or dog_name == "N/A":
            try:
                url_part = url.rstrip("/").split("/")[-1]
                dog_name = urllib.parse.unquote(url_part).replace("-", " ").strip()
            except:
                dog_name = f"Dog-{extract_uid_from_url(url)}"

        dog_info = {
            "U-ID": extract_uid_from_url(url),
            "URL": url,
            "Name": dog_name,
            "Nickname": nickname,
            "Breed": get_dog_info_field(driver, "//div[@title='Breed']/following-sibling::div"),
            "DOB": dob,
            "Date of Death": dod,
            "Bred In": get_dog_info_field(driver, "//div[@title='Bred in']/following-sibling::div"),
            "Sire": sire_name,
            "Sire ID": sire_id,
            "Dam": dam_name,
            "Dam ID": dam_id,
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
        }

        append_or_update_row_in_memory(rows, by_url, dog_info)

        if sire_url != "N/A":
            sire_entry = _blank_entry(sire_url, sire_name, sire_dob)
            append_or_update_row_in_memory(rows, by_url, sire_entry)

        if dam_url != "N/A":
            dam_entry = _blank_entry(dam_url, dam_name, dam_dob)
            append_or_update_row_in_memory(rows, by_url, dam_entry)

        return dog_info

    except Exception as e:
        msg = str(e)
        print("⚠️ scrape_dog_profile error:", msg)
        if error_holder is not None:
            error_holder.append(msg)
        return None


def _blank_entry(url, name, dob="N/A"):
    """Placeholder row (sire/dam stub) - URL/UID/Name (aur mile to DOB) hamesha bharay jate hain."""
    entry = {k: "N/A" for k in FIELDNAMES}
    entry["U-ID"] = extract_uid_from_url(url)
    entry["URL"] = url
    entry["Name"] = name if name and name != "N/A" else "N/A"
    entry["DOB"] = dob if dob and dob != "N/A" else "N/A"
    return entry


# ======================
# WORKER TAB MANAGEMENT (crash fix - ek hi tab reuse hota hai, open/close nahi)
# ======================

def ensure_worker_tab(driver, worker_window):
    """Worker tab zinda hai to wahi return karo, warna naya bana do."""
    try:
        if worker_window and worker_window in driver.window_handles:
            driver.switch_to.window(worker_window)
            return worker_window
    except Exception:
        pass

    driver.execute_script("window.open('');")
    new_handle = driver.window_handles[-1]
    driver.switch_to.window(new_handle)
    return new_handle


# ======================
# URL-BASED PAGINATION (Next-button dependency hata di gayi)
# ======================
#
# 🔧 WAJAH: "Next" button DOM mein find karne ki koshish unreliable thi -
# session/AJAX state ki wajah se kabhi milta, kabhi nahi, aur milne par bhi
# click ka effect hamesha sahi navigate nahi karta tha.
#
# ✅ FIX: Manual search ke baad jo URL milta hai
#   https://www.working-dog.com/dog/search?...&searchBtn=
# uske sare query-parameters nikaal kar hum khud URL banate hain:
#   https://www.working-dog.com/dog/extended-search?...&animal_page_number=N
# Site ka convention: animal_page_number 0-indexed hai, matlab
#   page 1 = koi param nahi (jo already manual search se load hai)
#   page 2 = animal_page_number=1
#   page 3 = animal_page_number=2   ...soon
#
def get_search_base_and_query(driver):
    """Current (page 1) URL se domain aur query-parameters nikalta hai."""
    current = driver.current_url
    parsed = up.urlsplit(current)
    query_params = up.parse_qsl(parsed.query, keep_blank_values=True)
    # animal_page_number agar pehle se ho to hata do (hum khud add karenge)
    query_params = [(k, v) for k, v in query_params if k != "animal_page_number"]
    base_domain = f"{parsed.scheme}://{parsed.netloc}"
    return base_domain, query_params


def build_extended_search_url(base_domain, query_params, page_num):
    """page_num >= 2 ke liye extended-search URL banata hai (0-indexed param)."""
    params = list(query_params) + [("animal_page_number", str(page_num - 1))]
    query_string = up.urlencode(params, doseq=True)
    return f"{base_domain}/dog/extended-search?{query_string}"


def estimate_total_pages(driver, per_page=DOGS_PER_PAGE):
    """
    Search-results page kabhi kabhi total dogs ki count dikhati hai
    (e.g. "480 dogs" / "480 results"). Agar mil jaye to total pages
    calculate kar lete hain (per_page dogs/page ke hisaab se) - isi
    number ka istemal pagination loop control karne ke liye hota hai.
    """
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'([\d,]+)\s+(?:dogs|results|animals)', body_text, re.I)
        if m:
            total = int(m.group(1).replace(",", ""))
            total_pages = max(1, -(-total // per_page))  # ceil division
            return total, total_pages
    except Exception:
        pass
    return None, None


def scrape_search_results_with_pause(driver, rows, by_url, resume_from=None, pause_event=None):
    resume_mode = bool(resume_from)
    found_resume = False
    dog_counter = 0     # Excel batching ke liye
    global_index = 0    # progress print ke liye ("5/480")

    listing_window = driver.current_window_handle

    driver.execute_script("window.open('');")
    worker_window = driver.window_handles[-1]
    driver.switch_to.window(listing_window)

    # Page 1 (jo manual search se already loaded hai) ka URL/query capture karo
    base_domain, query_params = get_search_base_and_query(driver)

    total_dogs, total_pages_est = estimate_total_pages(driver)
    if total_dogs:
        print(f"ℹ️ Total dogs mile: {total_dogs} — {total_pages_est} pages honge ({DOGS_PER_PAGE} dogs/page ke hisaab se).")
    else:
        print("ℹ️ Total dogs count nahi mil saka - jab tak koi page khaali na aaye tab tak scrape karte rahenge.")

    page_num = 1
    try:
        while True:
            page_label = f"{page_num}/{total_pages_est}" if total_pages_est else f"{page_num}"
            print(f"\n📄 Scraping page {page_label} ...")

            if page_num == 1:
                # Page 1 pehle se hi load hai (manual search se) - koi navigation nahi
                driver.switch_to.window(listing_window)
            else:
                page_url = build_extended_search_url(base_domain, query_params, page_num)
                print(f"🌐 Loading: {page_url}")
                driver.switch_to.window(listing_window)
                if not safe_get(driver, page_url):
                    print("⚠️ Page load nahi ho saka. Ruk rahe hain.")
                    break

            try:
                results_container = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".overviewList.overviewList_search.overviewList_searchAnimal")
                    )
                )
            except TimeoutException:
                print(f"✅ Page {page_num} par results nahi mile - scraping khatam samajhte hain.")
                break

            dog_items = results_container.find_elements(By.TAG_NAME, "li")
            dog_links = []
            for item in dog_items:
                try:
                    link = item.find_element(By.TAG_NAME, "a").get_attribute("href")
                    name = item.text.strip()
                    if link:
                        dog_links.append((name, link))
                except Exception:
                    continue

            if not dog_links:
                print(f"✅ Page {page_num} par koi dog nahi mila - scraping khatam.")
                break

            print(f"✅ {len(dog_links)} dogs found on page {page_num}")

            for dog_name, link in dog_links:
                global_index += 1
                wait_if_paused(pause_event)

                if resume_mode:
                    if resume_from.lower() in dog_name.lower() or resume_from.lower() in link.lower():
                        print(f"⏩ Resume point found: {dog_name} ({link})")
                        resume_mode = False
                        found_resume = True
                    else:
                        print(f"⏭️ Skipping {dog_name} (resume not reached yet)")
                        continue

                t_start = time.time()
                progress = f"{global_index}/{total_dogs}" if total_dogs else f"{global_index}/?"
                print(f"🔎 [{progress}] Scraping: {dog_name} | {link}")

                row = None
                try:
                    worker_window = ensure_worker_tab(driver, worker_window)
                    row = scrape_dog_with_captcha_retry(driver, link, rows, by_url, pause_event)
                except (InvalidSessionIdException, WebDriverException) as e:
                    print(f"💥 Browser session crash ho gaya: {e}")
                    print("💾 Ab tak scrape hua data CSV/Excel/JSON mein save kar rahe hain...")
                    write_all(rows, include_excel=True)
                    raise
                finally:
                    try:
                        driver.switch_to.window(listing_window)
                    except Exception:
                        pass

                if not row:
                    print(f"⚠️ Could not scrape {link} - failed.txt mein add ho gaya, agle dog par ja rahe hain")
                    continue

                dog_counter += 1
                write_all(rows, include_excel=(dog_counter % EXCEL_WRITE_EVERY_N_DOGS == 0))
                elapsed = time.time() - t_start
                print(f"   ⏱ {elapsed:.1f}s")
                nap(0.3, 0.6)

            # Agar total_pages ka pata hai aur ye last page tha to yahin ruk jao
            if total_pages_est and page_num >= total_pages_est:
                print(f"✅ Andazan total {total_pages_est} pages complete ho gaye.")
                break

            page_num += 1
    finally:
        try:
            if worker_window and worker_window in driver.window_handles and worker_window != listing_window:
                driver.switch_to.window(worker_window)
                driver.close()
                driver.switch_to.window(listing_window)
        except Exception:
            pass

    if resume_from and not found_resume:
        print(f"⚠️ Resume dog '{resume_from}' not found in results.")
        print("❌ Started from the first dog instead.")


# ======================
# FILE UTILITIES
# ======================
FIELDNAMES = [
    "U-ID", "URL", "Name", "Nickname", "Breed", "DOB", "Date of Death", "Bred In",
    "Sire", "Sire ID", "Dam", "Dam ID",
    "Reg No", "Pedigree Number", "2nd Pedigree Number", "Chip Number", "Variety",
    "Breeders Association", "Result", "Working Cert",
    "Owner Name", "Breeder Name",
    "Health VetResults", "Health LabResults", "Body Traits", "Diversity Traits",
]


def load_existing_data():
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
                    rows.append(row)
                    if row.get("URL"):
                        by_url[row["URL"]] = row
        except Exception:
            pass
    return rows, by_url

def write_all(rows, include_excel=True):
    """CSV/JSON hamesha turant likhte hain (safety). Excel sirf jab include_excel=True (speed)."""
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

def rescrape_range(driver, rows, by_url, start_idx, end_idx, pause_event=None):
    """Re-scrape existing dogs from database by inclusive row-index range [start_idx, end_idx]."""
    total = len(rows)
    if total == 0:
        print("⚠️ Database is empty. Nothing to re-scrape.")
        return

    start_idx = max(0, start_idx)
    end_idx = min(end_idx, total - 1)
    if start_idx > end_idx:
        print("⚠️ Invalid range. Start index is greater than end index.")
        return

    print(f"📊 Database has {total} records.")
    print(f"➡️ Re-scraping records {start_idx + 1} to {end_idx + 1} (1-based).")

    dog_counter = 0
    for i in range(start_idx, end_idx + 1):
        wait_if_paused(pause_event)

        row = rows[i]
        url = (row.get("URL") or "").strip()
        name = row.get("Name", "N/A")
        if not url or url == "N/A":
            print(f"⏭️ Skipping row {i+1}: No URL.")
            continue

        print(f"\n🔎 Re-scraping {i+1}/{total}: {name} | {url}")
        updated = scrape_dog_with_captcha_retry(driver, url, rows, by_url, pause_event)

        if updated:
            print(f"✅ Updated: {updated.get('Name','N/A')} ({updated.get('U-ID','N/A')})")
            dog_counter += 1
            write_all(rows, include_excel=(dog_counter % EXCEL_WRITE_EVERY_N_DOGS == 0))
        else:
            print(f"⚠️ Failed to scrape: {url} - failed.txt mein add ho gaya")

        nap(0.3, 0.6)

    write_all(rows, include_excel=True)
    print("🎯 Re-scraping finished for selected range.")

# ======================
# APPEND/UPDATE ROW
# ======================

SENSITIVE_FIELDS = [
    "Name", "Nickname", "Breed", "DOB", "Variety", "Sire", "Dam",
    "Owner Name", "Breeder Name",
    "Health VetResults", "Health LabResults", "Body Traits", "Diversity Traits",
]


def append_or_update_row_in_memory(rows, by_url, new_row):
    url = (new_row.get("URL") or "").strip()
    if not url:
        return False

    changed = False

    def clean_general_field(val, field_name=None):
        if not val:
            return "N/A"
        txt = str(val).strip().lower()

        if field_name in SENSITIVE_FIELDS:
            if txt == "no data":
                return "N/A"
            return str(val).strip()

        junk_phrases = ["no data", "n/a"]
        if any(txt == j for j in junk_phrases):
            return "N/A"
        return str(val).strip()

    if url in by_url:
        existing = by_url[url]
        for k in FIELDNAMES:
            if k == "URL":
                continue

            new_val_raw = (new_row.get(k) or "").strip()
            existing_val_raw = (existing.get(k) or "").strip()

            if k in ["Owner Name", "Breeder Name"]:
                new_val = clean_owner_breeder_text(new_val_raw) if "\n" in new_val_raw else (new_val_raw or "N/A")
                existing_val_clean = existing_val_raw or "N/A"
                if new_val and new_val != "N/A" and new_val != existing_val_clean:
                    existing[k] = new_val
                    changed = True

            else:
                new_val = clean_general_field(new_val_raw, k)
                if new_val and new_val != "N/A" and new_val != existing_val_raw:
                    existing[k] = new_val
                    changed = True

    else:
        clean_row = {}
        for k in FIELDNAMES:
            val = (new_row.get(k) or "").strip()

            if k in ["Owner Name", "Breeder Name"]:
                val = clean_owner_breeder_text(val) if "\n" in val else (val or "N/A")
            else:
                val = clean_general_field(val, k)

            clean_row[k] = val if val else "N/A"

        clean_row["URL"] = url

        rows.append(clean_row)
        by_url[url] = clean_row
        changed = True

    return changed


def clean_existing_files():
    rows, by_url = load_existing_data()
    fixed = 0

    def clean_general_field(val, field_name=None):
        if not val:
            return "N/A"
        txt = str(val).strip().lower()

        if field_name in SENSITIVE_FIELDS:
            if txt == "no data":
                return "N/A"
            return str(val).strip()

        junk_phrases = ["no data", "n/a"]
        if any(txt == j for j in junk_phrases):
            return "N/A"
        return str(val).strip()

    for row in rows:
        for k in FIELDNAMES:
            if k == "URL":
                continue
            if k in ["Owner Name", "Breeder Name"]:
                original = row.get(k, "N/A")
                cleaned = clean_owner_breeder_text(original) if "\n" in original else (original or "N/A")
                if cleaned != original and cleaned != "N/A":
                    row[k] = cleaned
                    fixed += 1
                elif cleaned == "N/A" and original not in ["N/A", "", None]:
                    row[k] = "N/A"
                    fixed += 1
            else:
                original = row.get(k, "N/A")
                cleaned = clean_general_field(original, k)
                if cleaned != original:
                    row[k] = cleaned
                    fixed += 1

    if fixed > 0:
        write_all(rows)
        print(f"✅ Cleaned {fixed} entries in existing files.")
    else:
        print("✅ No fixes needed. Everything already clean.")


# ======================
# SAFE GET
# ======================

def safe_get(driver, url, retries=3):
    for i in range(retries):
        try:
            driver.get(url)
            return True
        except Exception as e:
            print(f"⚠️ Attempt {i+1} failed: {e}")
            nap(2, 3)
    return False


def log_failed(url, reason="Unknown error"):
    """Jo dogs scrape nahi ho paate unko failed.txt mein likhta hai (duplicate-safe)."""
    try:
        if os.path.exists(FAILED_LOG_FILE):
            with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
                existing_content = f.read()
            if url in existing_content:
                return

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(FAILED_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {url} | Reason: {reason}\n")
        print(f"📝 Failed dog failed.txt mein likh diya gaya: {url}")
    except Exception as e:
        print("⚠️ failed.txt likhne mein masla:", e)


def remove_from_failed_log(url):
    """Agar dog dobara successfully scrape ho jaye to failed.txt se hata deta hai."""
    if not os.path.exists(FAILED_LOG_FILE):
        return
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        kept_lines = [ln for ln in lines if url not in ln]

        if len(kept_lines) != len(lines):
            with open(FAILED_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(kept_lines)
            print(f"🧹 {url} ab successfully scrape ho gaya - failed.txt se hata diya gaya.")
    except Exception as e:
        print("⚠️ failed.txt se entry hatane mein masla:", e)


def parse_failed_log():
    """failed.txt se saari UNIQUE dog URLs (insertion order) return karta hai."""
    urls = []
    seen = set()
    if not os.path.exists(FAILED_LOG_FILE):
        return urls
    try:
        with open(FAILED_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = re.search(r'(https?://\S+)', line)
                if m:
                    url = m.group(1).strip()
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
    except Exception as e:
        print("⚠️ failed.txt parse karne mein masla:", e)
    return urls


def scrape_dog_with_captcha_retry(driver, link, rows, by_url, pause_event, max_retries=3):
    """
    1) CAPTCHA active ho to isi dog par ruka rehta hai (wait_if_paused).
    2) Clear hone ke baad usi dog ko dobara try karta hai (max_retries dafa).
    3) Sab retries fail hon to failed.txt mein likh kar None return karta hai.
    4) Successful hone par (agar pehle failed.txt mein tha) usse hata deta hai.
    """
    last_error = "Unknown error"

    for attempt in range(1, max_retries + 1):
        wait_if_paused(pause_event)

        error_holder = []
        try:
            row = scrape_dog_profile(driver, link, rows, by_url, error_holder=error_holder)
        except (InvalidSessionIdException, WebDriverException):
            raise
        except Exception as e:
            row = None
            error_holder.append(str(e))

        if row:
            remove_from_failed_log(link)
            return row

        last_error = error_holder[0] if error_holder else "scrape_dog_profile returned None"
        print(f"⚠️ Attempt {attempt}/{max_retries} failed for {link}: {last_error}")

        wait_if_paused(pause_event)
        nap(1.5, 2.5)

    log_failed(link, last_error)
    return None

# ======================
# MAIN
# ======================

def main():
    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                         real_user_data_dir=REAL_USER_DATA_DIR,
                         profile_name=REAL_PROFILE_NAME)

    pause_event = threading.Event()
    stop_event = threading.Event()

    monitor_thread = persistent_captcha_monitor(driver, pause_event, stop_event)

    try:
        if not safe_get(driver, "https://www.working-dog.com/"):
            print("❌ Could not open homepage")
            return

        print("🔑 Browser opened. If CAPTCHA appears, the monitor will pause the scraper for manual solving.")
        rows, by_url = load_existing_data()
        print(f"ℹ️ Loaded {len(rows)} existing records from CSV (if any).")

        while True:
            print("\n--- MENU ---")
            print("1) Search dogs from website (manual form fill)")
            print("2) Re-scrape existing database by range")
            print("3) Scrape single dog by URL / Name / U-ID")
            print("4) Retry failed dogs (failed.txt) by range")
            print("5) Exit")

            choice = input("👉 Select option: ").strip()

            if choice == "1":
                if not safe_get(driver, "https://www.working-dog.com/dog/search"):
                    print("❌ Could not open search page")
                    continue

                print("📝 Please manually fill out the search form and press 'Search'.")
                print("⌛ Waiting for results to load ...")

                try:
                    WebDriverWait(driver, 120).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, ".overviewList.overviewList_search.overviewList_searchAnimal")
                        )
                    )
                    print("✅ Search results detected. Beginning scrape...")
                except TimeoutException:
                    print("⚠️ Timeout: No results detected after search. Try again.")
                    continue

                resume_from = input(
                    "➡️ Enter dog name to resume from "
                    "(leave empty to start from the first profile): "
                ).strip()

                if resume_from:
                    print(f"🔄 Resuming scrape from dog: {resume_from}")
                else:
                    print("🔄 Starting scrape from the first profile in search results...")
                    resume_from = None

                try:
                    scrape_search_results_with_pause(driver, rows, by_url, resume_from, pause_event)
                except (InvalidSessionIdException, WebDriverException) as e:
                    print(f"💥 Browser crash ho gaya scraping ke doran: {e}")
                    print("💾 Jo bhi data scrape ho chuka tha wo pehle hi CSV/Excel/JSON mein save ho chuka hai.")
                    print("🔁 Naya browser session start kar rahe hain...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    stop_event.set()

                    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                                         real_user_data_dir=REAL_USER_DATA_DIR,
                                         profile_name=REAL_PROFILE_NAME)
                    stop_event = threading.Event()
                    monitor_thread = persistent_captcha_monitor(driver, pause_event, stop_event)
                    safe_get(driver, "https://www.working-dog.com/")
                    print("✅ Naya browser session ready hai. Option 1 se dobara search karo (resume-name daal kar wahan se jari rakh sakte ho).")

                print(f"🎯 Finished scraping this search. Total records: {len(rows)}")
                write_all(rows, include_excel=True)
                print("🔄 Returning to main menu.\n")
                nap(1, 2)


            elif choice == "2":
                rows, by_url = load_existing_data()
                print(f"📊 Loaded {len(rows)} existing records.")

                try:
                    s = int(input("➡️ Enter starting index (1-based): ").strip())
                    e = int(input("➡️ Enter ending index (1-based, inclusive): ").strip())
                except ValueError:
                    print("⚠️ Invalid input for range.")
                    continue

                start_idx = max(0, s - 1)
                end_idx = max(0, e - 1)

                rescrape_range(driver, rows, by_url, start_idx, end_idx, pause_event=pause_event)
                print(f"📦 Records now: {len(rows)}")
                print("🔄 Returning to main menu.\n")
                nap(1, 2)

            elif choice == "3":
                    manual_url = input("🔗 Enter Dog Profile URL: ").strip().strip('"').strip("'")

                    if not manual_url.startswith("http"):
                          print("⚠️ Invalid URL format. Please enter a valid profile link.")
                    else:
                        rows, by_url = load_existing_data()
                        print(f"🔎 Scraping profile: {manual_url}")

                        dog_info = scrape_dog_with_captcha_retry(driver, manual_url, rows, by_url, pause_event)

                        if dog_info:
                           print(f"✅ Scraped: {dog_info.get('Name','N/A')} ({dog_info.get('U-ID','N/A')})")
                           write_all(rows, include_excel=True)
                        else:
                           print("❌ Failed to scrape this profile (failed.txt mein add ho gaya).")

            elif choice == "4":
                failed_urls = parse_failed_log()

                if not failed_urls:
                    print("✅ failed.txt khali hai / mojood nahi - koi failed dog nahi hai.")
                    nap(1, 2)
                    continue

                print(f"📊 failed.txt mein {len(failed_urls)} unique (abhi tak fail) dogs mile.")
                try:
                    s = int(input(f"➡️ Enter starting index (1-based, 1 to {len(failed_urls)}): ").strip())
                    e = int(input(f"➡️ Enter ending index (1-based, inclusive, 1 to {len(failed_urls)}): ").strip())
                except ValueError:
                    print("⚠️ Invalid input for range.")
                    continue

                start_idx = max(0, s - 1)
                end_idx = min(e - 1, len(failed_urls) - 1)
                if start_idx > end_idx:
                    print("⚠️ Invalid range. Start index end index se bara hai.")
                    continue

                rows, by_url = load_existing_data()
                dog_counter = 0

                for i in range(start_idx, end_idx + 1):
                    wait_if_paused(pause_event)
                    url = failed_urls[i]
                    print(f"\n🔎 Retrying failed dog {i+1}/{len(failed_urls)}: {url}")

                    updated = scrape_dog_with_captcha_retry(driver, url, rows, by_url, pause_event)

                    if updated:
                        print(f"✅ Ab scrape ho gaya: {updated.get('Name','N/A')} ({updated.get('U-ID','N/A')})")
                        dog_counter += 1
                        write_all(rows, include_excel=(dog_counter % EXCEL_WRITE_EVERY_N_DOGS == 0))
                    else:
                        print(f"⚠️ Abhi bhi fail ho raha hai: {url}")

                    nap(0.3, 0.6)

                write_all(rows, include_excel=True)
                print(f"🎯 Failed dogs retry (range {s} to {e}) complete.")
                print("🔄 Returning to main menu.\n")
                nap(1, 2)

            elif choice == "5":
               print("👋 Exiting the scraper.")
               break

            else:
              print("⚠️ Invalid option. Please choose 1, 2, 3, 4, or 5.")

    finally:
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass



if __name__ == "__main__":
    clean_existing_files()
    main()