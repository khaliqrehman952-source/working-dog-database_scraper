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
from selenium.common.exceptions import TimeoutException


# ======================
# CONFIG
# ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "dogs_data.csv")
EXCEL_FILE = os.path.join(BASE_DIR, "dogs_data.xlsx")
JSON_FILE = os.path.join(BASE_DIR, "dogs_data.json")

PROFILE_DIR = os.path.join(BASE_DIR, "selenium_profiles", "working_dog")
os.makedirs(PROFILE_DIR, exist_ok=True)

CAPTCHA_KEY = "7e75cbd7420555cc3c712176aaa15517"

REAL_USER_DATA_DIR = None   # e.g. r"C:\Users\<YOU>\AppData\Local\Google\Chrome\User Data"
REAL_PROFILE_NAME = "Default"




def persistent_captcha_monitor(driver, pause_event, stop_event, poll_interval=3):
    """
    Background monitor: sirf detect karta hai iframe[src*='turnstile'].
    Agar iframe ajaye -> pause_event.set()  (scraper should pause).
    Jab iframe gayab ho -> pause_event.clear() (scraper may resume).
    NOTE: This DOES NOT attempt to solve the CAPTCHA.
    """
    def _run():
        last_seen = False
        while not stop_event.is_set():
            try:
                # lightweight check: only find elements; avoid heavy operations here
                if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='turnstile']"):
                    if not last_seen:
                        print("🔍 Turnstile iframe detected — pausing scraper. Please solve it manually in the browser.")
                        pause_event.set()
                        last_seen = True
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

# ======================
# HELPERS
# ======================

def nap(a=0.8, b=1.5):
    time.sleep(random.uniform(a, b))

def safe_text(driver, xpath, default="N/A"):
    try:
        return driver.find_element(By.XPATH, xpath).text.strip()
    except Exception:
        return default
    
def extract_uid_from_url(url):
    """Extract numeric U-ID from working-dog profile URL."""
    try:
        m = re.search(r"/dogs-details/(\d+)", url)
        if m:
            return m.group(1)
    except:
        pass
    return "N/A"


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

    driver = uc.Chrome(options=options, use_subprocess=True)
    driver.set_page_load_timeout(120)
    return driver

# ======================
# CLEANER (updated only for fallback filtering)
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
            dob = spans[0].text.strip() or "N/A"

        if len(spans) >= 2:
            second_val = spans[1].text.strip()
            # Agar 'year' word hai to ye sirf age hai, DOD nahi
            if second_val and not re.search(r'year', second_val, re.I):
                # Agar dd.mm.yyyy format hai to hi DOD assign karein
                if re.match(r'\d{2}\.\d{2}\.\d{4}', second_val):
                    dod = second_val
    except:
        pass

    return dob, dod



# ======================
# ROBUST OWNER/BREEDER EXTRACTOR
# ======================

def extract_owner_or_breeder(driver, role="owner"):
    """
    Extract Owner or Breeder name strictly from its tab only.
    Only return name if it's in <a href="..."> and has a title attribute.
    """
    assert role in ["owner", "breeder"]

    try:
        tab_index = 1 if role == "owner" else 2
        base_xpath = f'//*[@id="app"]/div[3]/div[2]/div[1]/div[2]/div/div[2]/div/div[2]/div/div[{tab_index}]'
        container = driver.find_element(By.XPATH, base_xpath)
        
        anchor = container.find_element(By.CSS_SELECTOR, "a.font-weight-bold.text-black")
        href = anchor.get_attribute("href")
        title = anchor.get_attribute("title")

        if href and title:
            return title.strip()
        else:
            return "N/A"

    except Exception:
        return "N/A"

def get_sire_dam(driver):
    sire_name, dam_name = "N/A", "N/A"
    sire_url, dam_url = "N/A", "N/A"

    try:
        parents = driver.find_elements(By.CSS_SELECTOR, "div.gen-0__dog-name")
        for p in parents:
            try:
                link_el = p.find_element(By.CSS_SELECTOR, "a")
                name_el = p.find_element(By.CSS_SELECTOR, "span.gen__animal-name")
                gender_icon = p.find_element(By.CSS_SELECTOR, "span.mdi")

                name = name_el.text.strip()
                link = link_el.get_attribute("href")
                gender_class = gender_icon.get_attribute("class")

                if "mdi-gender-male" in gender_class:
                    sire_name, sire_url = name, link
                elif "mdi-gender-female" in gender_class:
                    dam_name, dam_url = name, link
            except:
                continue
    except:
        pass

    return (sire_name, sire_url), (dam_name, dam_url)




def robust_safe_text(driver, xpath, default="N/A", retries=2, timeout=8):
    """Try multiple times to extract text for reliability (optimized)."""
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
        nap(0.8, 1.2)  # reduced wait
    return default


def wait_for_page_ready(driver, timeout=15):
    """Wait until JS ready + small pause for dynamic loads (faster)."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    nap(1, 1.5)  # reduced from 2s+


def scroll_page(driver, stop_xpath='//div[@class="col-12 pedigree-section" and @id="pedigree-section"]'):
    """Scroll until the target element is visible (pedigree section)."""
    last_height = driver.execute_script("return window.scrollY;")
    step = 1000  # pixels to scroll per iteration

    while True:
        # Scroll down by step
        driver.execute_script(f"window.scrollBy(0, {step});")
        nap(0.8, 1.2)

        # Check if target element is visible
        try:
            elem = driver.find_element(By.XPATH, stop_xpath)
            if elem.is_displayed():
                # Scroll exactly to top of element
                driver.execute_script("arguments[0].scrollIntoView(true);", elem)
                nap(0.5, 1)
                print("✅ Pedigree section reached. Stopping scroll.")
                break
        except:
            pass

        # Stop if we cannot scroll further
        new_height = driver.execute_script("return window.scrollY;")
        if new_height == last_height:
            print("⚠️ Reached bottom of page, stopping scroll.")
            break
        last_height = new_height





def scrape_dog_profile(driver, url, rows, by_url):
    try:
        if not safe_get(driver, url):
            return None

        wait_for_page_ready(driver)

        # 🌐 extra wait for dog name (guarantee profile loaded)
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.XPATH, "//h1"))
        )

        nap(1, 1.5)  # reduced pause

        # 🔽 Scroll through profile so all sections (pedigree, owner, breeder) load
        scroll_page(driver)

        # 🐾 Extract info
        owner_name = extract_owner_or_breeder(driver, role="owner")
        breeder_name = extract_owner_or_breeder(driver, role="breeder")
        (sire_name, sire_url), (dam_name, dam_url) = get_sire_dam(driver)
        dob, dod = extract_dob_dod(driver)

        import urllib.parse

        # 🐾 Dog name with multiple fallbacks
        dog_name = "N/A"
        try:
            h1_el = WebDriverWait(driver, 5).until(
                EC.visibility_of_element_located((By.TAG_NAME, "h1"))
            )
            dog_name = h1_el.text.strip()
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

        # 🐾 Build dog_info dictionary
        dog_info = {
            "U-ID": extract_uid_from_url(url),
            "URL": url,
            "Name": dog_name,
            "Breed": robust_safe_text(driver, "//div[@title='Breed']/following-sibling::div"),
            "DOB": dob,
            "Date of Death": dod,
            "Bred In": robust_safe_text(driver, "//div[@title='Bred in']/following-sibling::div"),
            "Sire": sire_name,
            "Dam": dam_name,
            "Reg No": robust_safe_text(driver, "//div[@title='Registration number']/following-sibling::div"),
            "Pedigree Number": robust_safe_text(driver, "//div[@title='Pedigree number']/following-sibling::div"),
            "Chip Number": robust_safe_text(driver, "//div[@title='Chip number']/following-sibling::div"),
            "Variety": robust_safe_text(driver, "//div[@title='Variety']/following-sibling::div"),
            "Owner Name": owner_name,
            "Breeder Name": breeder_name,
        }

        append_or_update_row_in_memory(rows, by_url, dog_info)
        


        # 🐾 Add sire & dam placeholders if found
        if sire_url != "N/A":
            sire_entry = {
                "U-ID": extract_uid_from_url(sire_url),
                "URL": sire_url,
                "Name": sire_name,
                "Breed": "N/A",
                "DOB": "N/A",
                "Date of Death": "N/A",
                "Bred In":"N/A",
                "Sire": "N/A",
                "Dam": "N/A",
                "Reg No": "N/A",
                "Pedigree Number": "N/A",
                "Chip Number": "N/A",
                "Variety": "N/A",
                "Owner Name": "N/A",
                "Breeder Name": "N/A",
            }
            append_or_update_row_in_memory(rows, by_url, sire_entry)

        if dam_url != "N/A":
            dam_entry = {
                "U-ID": extract_uid_from_url(dam_url),
                "URL": dam_url,
                "Name": dam_name,
                "Breed": "N/A",
                "DOB": "N/A",
                "Date of Death": "N/A",
                "Bred In":"N/A",
                "Sire": "N/A",
                "Dam": "N/A",
                "Reg No": "N/A",
                "Pedigree Number": "N/A",
                "Chip Number": "N/A",
                "Variety": "N/A",
                "Owner Name": "N/A",
                "Breeder Name": "N/A",
            }
            append_or_update_row_in_memory(rows, by_url, dam_entry)

        return dog_info

    except Exception as e:
        print("⚠️ scrape_dog_profile error:", e)
        return None


def scrape_search_results_with_pause(driver, rows, by_url, resume_from=None, pause_event=None):
    page_num = 1
    resume_mode = bool(resume_from)   # True only if user entered a resume name
    found_resume = False
    base_url = driver.current_url     # ✅ Use the actual loaded search results URL

    while True:
        print(f"\n📄 Scraping page {page_num} ...")

        # Step 1: Navigate to the current page
        current_url = f"{base_url}&animal_page_number={page_num}"
        driver.get(current_url)
        print(f"🌐 Loaded: {current_url}")

        # Step 2: Wait for search results to load
        try:
            results_container = WebDriverWait(driver, 60).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".overviewList.overviewList_search.overviewList_searchAnimal")
                )
            )
        except TimeoutException:
            print("⚠️ Results container not found. Stopping scrape.")
            break

        # Step 3: Extract dog links
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

        print(f"✅ {len(dog_links)} dogs found on page {page_num}")

        # Step 4: Scrape each dog profile
        for dog_name, link in dog_links:
            # Pause if CAPTCHA detected
            while pause_event and pause_event.is_set():
                print("⏸️ Scraper paused (CAPTCHA present). Please solve it in the browser. Waiting...")
                time.sleep(3)

            if resume_mode:
                if resume_from.lower() in dog_name.lower() or resume_from.lower() in link.lower():
                    print(f"⏩ Resume point found: {dog_name} ({link})")
                    resume_mode = False
                    found_resume = True
                else:
                    print(f"⏭️ Skipping {dog_name} (resume not reached yet)")
                    continue

            # Normal scrape
            print(f"🔎 Scraping: {dog_name} | {link}")
            row = scrape_dog_profile(driver, link, rows, by_url)
            if not row:
                print(f"⚠️ Could not scrape {link}")
                continue

            write_all(rows)
            nap(0.6, 1.2)

        # Step 5: Reload current page to ensure clean pagination click
        print(f"🔄 Reloading page {page_num} to locate Next button ...")
        driver.get(current_url)

        try:
            WebDriverWait(driver, 60).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".overviewList.overviewList_search.overviewList_searchAnimal")
                )
            )

            # Step 6: Try clicking the "Next" button
            next_btn = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[contains(@class,'pagination')]//input[contains(@class,'rightArrow') and @type='submit']")
                )
            )

            if next_btn.is_displayed() and next_btn.is_enabled():
                print("➡️ Clicking Next to go to the next page ...")
                driver.execute_script("arguments[0].click();", next_btn)

                WebDriverWait(driver, 30).until(EC.staleness_of(next_btn))
                WebDriverWait(driver, 60).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".overviewList.overviewList_search.overviewList_searchAnimal")
                    )
                )
                wait_for_page_ready(driver)
                nap(1.5, 2.5)
                page_num += 1
            else:
                print("✅ Next button is disabled. No more pages.")
                break

        except Exception as e:
            print("⚠️ Next button not found or error occurred:", e)
            break

    # Final check for resume
    if resume_from and not found_resume:
        print(f"⚠️ Resume dog '{resume_from}' not found in results.")
        print("❌ Started from the first dog instead.")



# ======================
# FILE UTILITIES
# ======================
FIELDNAMES = [
    "U-ID", "URL", "Name", "Breed", "DOB", "Date of Death", "Bred In", "Sire", "Dam", "Reg No",
    "Pedigree Number", "Chip Number", "Variety",
    "Owner Name", "Breeder Name"
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

def write_all(rows):
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
    """
    Re-scrape existing dogs from database by inclusive row-index range [start_idx, end_idx].
    Updates missing/wrong fields + adds sire/dam records using existing logic.
    """
    total = len(rows)
    if total == 0:
        print("⚠️ Database is empty. Nothing to re-scrape.")
        return

    # clamp & normalize
    start_idx = max(0, start_idx)
    end_idx = min(end_idx, total - 1)
    if start_idx > end_idx:
        print("⚠️ Invalid range. Start index is greater than end index.")
        return

    print(f"📊 Database has {total} records.")
    print(f"➡️ Re-scraping records {start_idx + 1} to {end_idx + 1} (1-based).")

    for i in range(start_idx, end_idx + 1):
        # Respect CAPTCHA pause
        while pause_event and pause_event.is_set():
            print("⏸️ Scraper paused (CAPTCHA present). Please solve it in the browser. Waiting...")
            time.sleep(3)

        row = rows[i]
        url = (row.get("URL") or "").strip()
        name = row.get("Name", "N/A")
        if not url or url == "N/A":
            print(f"⏭️ Skipping row {i+1}: No URL.")
            continue

        print(f"\n🔎 Re-scraping {i+1}/{total}: {name} | {url}")
        updated = scrape_dog_profile(driver, url, rows, by_url)

        if updated:
            print(f"✅ Updated: {updated.get('Name','N/A')} ({updated.get('U-ID','N/A')})")
            write_all(rows)
        else:
            print(f"⚠️ Failed to scrape: {url}")

        nap(0.6, 1.2)

    print("🎯 Re-scraping finished for selected range.")

# ======================
# APPEND/UPDATE ROW
# ======================

def append_or_update_row_in_memory(rows, by_url, new_row):
    url = (new_row.get("URL") or "").strip()
    if not url:
        return False

    changed = False

    def clean_general_field(val, field_name=None):
        """Convert only 'no data' → N/A for sensitive fields,
        but allow broader cleaning for general fields."""
        if not val:
            return "N/A"
        txt = str(val).strip().lower()

        # Fields we want to preserve unless explicitly 'no data'
        sensitive_fields = ["Name", "Breed", "DOB", "Variety", "Sire", "Dam", "Owner Name", "Breeder Name"]

        if field_name in sensitive_fields:
            if txt == "no data":
                return "N/A"
            return str(val).strip()

        # General fields → clean more aggressively
        junk_phrases = ["no data", "n/a"]
        if any(txt == j for j in junk_phrases):
            return "N/A"
        return str(val).strip()

    if url in by_url:
        # 🔄 Update existing row
        existing = by_url[url]
        for k in FIELDNAMES:
            if k == "URL":
                continue  # never modify URL

            new_val_raw = (new_row.get(k) or "").strip()
            existing_val_raw = (existing.get(k) or "").strip()

            if k in ["Owner Name", "Breeder Name"]:
                new_val = clean_owner_breeder_text(new_val_raw)
                existing_val_clean = clean_owner_breeder_text(existing_val_raw)
                if new_val and new_val != "N/A" and new_val != existing_val_clean:
                    existing[k] = new_val
                    changed = True

            else:
                new_val = clean_general_field(new_val_raw, k)
                if new_val and new_val != "N/A" and new_val != existing_val_raw:
                    existing[k] = new_val
                    changed = True
    else:
        # 🆕 New row
        clean_row = {}
        for k in FIELDNAMES:
            val = (new_row.get(k) or "").strip()

            if k in ["Owner Name", "Breeder Name"]:
                val = clean_owner_breeder_text(val)
            else:
                val = clean_general_field(val, k)

            clean_row[k] = val if val else "N/A"

        clean_row["URL"] = url  # enforce URL

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

        sensitive_fields = ["Name", "Breed", "DOB", "Variety", "Sire", "Dam", "Owner Name", "Breeder Name"]

        if field_name in sensitive_fields:
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
                original = row[k]
                cleaned = clean_owner_breeder_text(original)
                if cleaned != original and cleaned != "N/A":
                    row[k] = cleaned
                    fixed += 1
                elif cleaned == "N/A" and original not in ["N/A", "", None]:
                    row[k] = "N/A"
                    fixed += 1
            else:
                original = row[k]
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
            nap(3, 5)
    return False

# ======================
# MAIN
# ======================

def main():
    # init driver (as before)
    driver = init_driver(use_real_profile=bool(REAL_USER_DATA_DIR),
                         real_user_data_dir=REAL_USER_DATA_DIR,
                         profile_name=REAL_PROFILE_NAME)

    # Events for coordination
    pause_event = threading.Event()   # set when CAPTCHA present -> scraper should pause
    stop_event = threading.Event()    # set when program exiting -> monitor stops

    # Start monitor
    monitor_thread = persistent_captcha_monitor(driver, pause_event, stop_event)

    try:
        # open homepage
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
            print("4) Exit")

            choice = input("👉 Select option: ").strip()

            if choice == "1":
                # 🟢 Manual search fill then scrape results
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

                # Resume logic (fixed)
                resume_from = input(
                    "➡️ Enter dog name to resume from "
                    "(leave empty to start from the first profile): "
                ).strip()

                if resume_from:
                    print(f"🔄 Resuming scrape from dog: {resume_from}")
                else:
                    print("🔄 Starting scrape from the first profile in search results...")
                    resume_from = None   # blank means start fresh

                # Run scraper
                scrape_search_results_with_pause(driver, rows, by_url, resume_from, pause_event)

                print(f"🎯 Finished scraping this search. Total records: {len(rows)}")
                write_all(rows)
                print("🔄 Returning to main menu.\n")
                nap(1, 2)


            elif choice == "2":
                # 🟢 Database range re-scrape
                rows, by_url = load_existing_data()
                print(f"📊 Loaded {len(rows)} existing records.")

                try:
                    s = int(input("➡️ Enter starting index (1-based): ").strip())
                    e = int(input("➡️ Enter ending index (1-based, inclusive): ").strip())
                except ValueError:
                    print("⚠️ Invalid input for range.")
                    continue

                # Convert to 0-based
                start_idx = max(0, s - 1)
                end_idx = max(0, e - 1)

                rescrape_range(driver, rows, by_url, start_idx, end_idx, pause_event=pause_event)
                print(f"📦 Records now: {len(rows)}")
                write_all(rows)
                print("🔄 Returning to main menu.\n")
                nap(1, 2)

            elif choice == "3":
                  # 🆕 Manual single dog scrape (only URL)
                    manual_url = input("🔗 Enter Dog Profile URL: ").strip().strip('"').strip("'")

                    if not manual_url.startswith("http"):
                          print("⚠️ Invalid URL format. Please enter a valid profile link.")
                    else:
                        rows, by_url = load_existing_data()
                        print(f"🔎 Scraping profile: {manual_url}")

                        dog_info = scrape_dog_profile(driver, manual_url, rows, by_url)

                        if dog_info:
                           print(f"✅ Scraped: {dog_info.get('Name','N/A')} ({dog_info.get('U-ID','N/A')})")
                           write_all(rows)   # <-- save to CSV
                        else:
                           print("❌ Failed to scrape this profile.")
  
 
            elif choice == "4":
               print("👋 Exiting the scraper.")
               break

            else:
              print("⚠️ Invalid option. Please choose 1, 2, 3, or 4.")

    finally:
        # signal monitor to stop
        stop_event.set()
        try:
            driver.quit()
        except Exception:
            pass



if __name__ == "__main__":
    clean_existing_files()
    main()
