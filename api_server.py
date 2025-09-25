# server.py
from flask import Flask, request, jsonify, render_template
import threading, collections, datetime, traceback, os, sys, subprocess

# import your existing scraper module (save your big scraper as wr_dog_scraper.py)
import wr_dog_scraper as wd

# Selenium helpers used for waiting/selecting elements during search-fill
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

app = Flask(__name__, template_folder="templates")

class ScraperController:
    def __init__(self):
        self.thread = None
        self.running = False
        self.last_action = "-"
        self.logs = collections.deque(maxlen=2000)
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.driver = None
        self.lock = threading.Lock()

    def log(self, msg):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        print(entry)
        self.logs.appendleft(entry)

    def start_driver_if_needed(self):
        if self.driver is None:
            try:
                self.log("Starting browser (undetected-chromedriver)...")
                self.driver = wd.init_driver(
                    use_real_profile=bool(wd.REAL_USER_DATA_DIR),
                    real_user_data_dir=wd.REAL_USER_DATA_DIR,
                    profile_name=wd.REAL_PROFILE_NAME
                )
                wd.persistent_captcha_monitor(self.driver, self.pause_event, self.stop_event)
                self.log("Browser started and CAPTCHA monitor launched.")
            except Exception as e:
                self.log(f"Failed to start browser: {e}")
                traceback.print_exc()
                self.driver = None

    def stop_driver(self):
        try:
            self.stop_event.set()
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
            self.log("Browser stopped.")
        except Exception as e:
            self.log(f"Error while stopping driver: {e}")

    def start_task(self, target, *args, **kwargs):
        with self.lock:
            if self.running:
                return False, "Another task is already running."
            self.running = True
            self.last_action = target.__name__
            self.stop_event.clear()
            t = threading.Thread(target=self._task_wrapper, args=(target, args, kwargs), daemon=True)
            self.thread = t
            t.start()
            return True, "Task started."

    def _task_wrapper(self, target, args, kwargs):
        try:
            target(*args, **kwargs)
        except Exception as e:
            self.log(f"Task error: {e}")
            traceback.print_exc()
        finally:
            self.running = False
            self.log("Task finished.")

    # --- tasks ---

    def task_search_scrape(self, dog_name=None, resume_from=None):
        try:
            self.start_driver_if_needed()
            if not self.driver:
                self.log("Browser not available.")
                return

            search_url = "https://www.working-dog.com/dog/search"
            self.log(f"Opening search page: {search_url}")
            if not wd.safe_get(self.driver, search_url):
                self.log("Could not open search page.")
                return

            try:
                name_box = WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input#name"))
                )
                name_box.clear()
                if dog_name:
                    name_box.send_keys(dog_name)
                    self.log(f"Dog name '{dog_name}' entered in search form.")
                else:
                    self.log("No dog_name provided; leaving search form blank.")

                try:
                    search_btn = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
                except Exception:
                    search_btn = self.driver.find_element(By.CSS_SELECTOR, "form button[type='submit']")
                search_btn.click()
                self.log("Search button clicked, waiting for results...")
            except TimeoutException:
                self.log("Search form not found on site (input#name). Waiting for manual fill instead.")
            except Exception as e:
                self.log(f"Exception while auto-filling search form: {e}")

            try:
                WebDriverWait(self.driver, 120).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR,
                        ".overviewList.overviewList_search.overviewList_searchAnimal"))
                )
                self.log("Search results detected — beginning scrape.")
            except TimeoutException:
                self.log("Timeout waiting for search results.")
                return

            rows, by_url = wd.load_existing_data()
            wd.scrape_search_results_with_pause(self.driver, rows, by_url, resume_from, self.pause_event)
            wd.write_all(rows)
            self.log(f"Search-scrape finished. Total records now: {len(rows)}")

        except Exception as e:
            self.log(f"Exception in search scrape: {e}")
            traceback.print_exc()

    def task_rescrape_range(self, start_idx, end_idx):
        try:
            self.start_driver_if_needed()
            if not self.driver:
                self.log("Browser not available.")
                return
            rows, by_url = wd.load_existing_data()
            self.log(f"Starting re-scrape range: {start_idx+1} to {end_idx+1}")
            wd.rescrape_range(self.driver, rows, by_url, start_idx, end_idx, pause_event=self.pause_event)
            wd.write_all(rows)
            self.log("Re-scrape range finished.")
        except Exception as e:
            self.log(f"Exception in rescrape range: {e}")
            traceback.print_exc()

    def task_scrape_single(self, url):
        try:
            self.start_driver_if_needed()
            if not self.driver:
                self.log("Browser not available.")
                return
            rows, by_url = wd.load_existing_data()
            self.log(f"Scraping single profile: {url}")
            info = wd.scrape_dog_profile(self.driver, url, rows, by_url)
            if info:
                wd.write_all(rows)
                self.log(f"Scraped: {info.get('Name','N/A')} ({info.get('U-ID','N/A')})")
            else:
                self.log("Failed to scrape single profile.")
        except Exception as e:
            self.log(f"Exception in scrape single: {e}")
            traceback.print_exc()

    def task_clean_files(self):
        try:
            self.log("Cleaning existing files...")
            wd.clean_existing_files()
            self.log("Cleaning finished.")
        except Exception as e:
            self.log(f"Exception while cleaning: {e}")
            traceback.print_exc()

controller = ScraperController()

# --- Flask routes ---

@app.route("/")
def index():
    return render_template("scrape_form.html")

@app.route("/api/start_search", methods=["POST"])
def api_start_search():
    data = request.get_json() or {}
    dog_name = (data.get("dog_name") or "").strip() or None
    resume_from = (data.get("resume_from") or "").strip() or None
    ok, msg = controller.start_task(controller.task_search_scrape, dog_name, resume_from)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/rescrape_range", methods=["POST"])
def api_rescrape_range():
    data = request.get_json() or {}
    try:
        s = int(data.get("start"))
        e = int(data.get("end"))
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid start/end"}), 400
    ok, msg = controller.start_task(controller.task_rescrape_range, s-1, e-1)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/scrape_single", methods=["POST"])
def api_scrape_single():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"ok": False, "msg": "Invalid URL"}), 400
    ok, msg = controller.start_task(controller.task_scrape_single, url)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/clean", methods=["POST"])
def api_clean():
    ok, msg = controller.start_task(controller.task_clean_files)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/open_file", methods=["POST"])
def api_open_file():
    data = request.get_json() or {}
    fname = data.get("file")
    if not fname:
        return jsonify({"ok": False, "msg": "No filename provided"}), 400
    try:
        base = os.path.abspath(os.path.dirname(__file__))
        target = os.path.abspath(os.path.join(base, fname))
        if not target.startswith(base):
            return jsonify({"ok": False, "msg": "Invalid filename"}), 400
        if not os.path.exists(target):
            return jsonify({"ok": False, "msg": f"File not found: {fname}"}), 404

        if sys.platform.startswith("win"):
            os.startfile(target)
        elif sys.platform.startswith("darwin"):
            subprocess.call(["open", target])
        else:
            subprocess.call(["xdg-open", target])
        return jsonify({"ok": True, "msg": f"Opening {fname}..."})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error: {str(e)}"})

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"running": controller.running, "last_action": controller.last_action})

@app.route("/api/logs", methods=["GET"])
def api_logs():
    return jsonify(list(controller.logs))

@app.route("/api/stop", methods=["POST"])
def api_stop():
    controller.stop_driver()
    return jsonify({"ok": True, "msg": "Stop signal sent, browser stopped."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
