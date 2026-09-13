# 🐶 Working Dog Scraper Suite

A multi-phase Python scraping pipeline for [Working-Dog](https://www.working-dog.com/).
The project started as a single dog-profile scraper with a web control panel, and has
since grown into a full pipeline that walks event listings → event results → individual
dog profiles (with pedigree, health, and competition history), filtered down to German
Shepherds.

> ⚠️ **Please confirm / edit before publishing:** a few notes below are marked
> `(needs confirmation)` — fill these in with the correct details before this goes live.

## 📂 Pipeline Overview

| Phase | Script | What it does | Output |
|---|---|---|---|
| 1 | `events_result_scraper.py` | Scrapes yearly event-listing pages (2019–2026), keeps only IGP / Ring discipline events. | `events_result_2019-2026/event_results.csv` `.json` `.xlsx` |
| 2 | `events_result_detail_scraper.py` | Opens every event's result page, scrapes header info (Category, Judges, Helpers, Date, Location) and the list of dogs that competed. | `results_events_detail_2019-2026/results_events_detail.csv` `.json` `.xlsx` |
| — | `events_detail_scraper.py` | ⚠️ Older/leftover version, **not used** — superseded by `events_result_detail_scraper.py` above. Safe to ignore or delete. | — |
| 3 | `german_shepherd_eventsdog_scraper.py` | Opens every unique dog found in Phase 2's results, keeps only German Shepherds, scrapes their full profile (pedigree, health, owner/breeder, image) plus their participated-events history matched back to Phase 2 data. | `German Shepherd (Events Dogs)/german_shepherd_events_dog.csv` `.json` `.xlsx` |
| — | `wr_dog_scraper.py` + `api_server.py` + `start_scraper.py` + `scrape_form.html` | Standalone web control panel — search and scrape **any** single dog profile directly by form input, independent of the events pipeline. | `working_dog_data/` |
| — | `kennel_scraper.py` *(needs confirmation)* | Scrapes kennel / breeder listings. | `kennel_data/` |
| — | `wr_events.py` *(needs confirmation — legacy?)* | Earlier/alternate events scraper — outputs appear to be `working_dog_events/` and `working_dog_events_detail/`. Confirm whether this is still used or has been superseded by Phases 1–2 above. | `working_dog_events/`, `working_dog_events_detail/` |

Both the events pipeline (Phases 1–3) and the standalone dog-search tool are **resumable**
— re-running a script skips work already saved, so an interrupted run can always continue
from where it left off. Failed pages are logged to `failed.txt` inside each output folder
for easy retrying.

## 🚀 Features

- Scrape dog profiles, event listings, and event results with Selenium + Python
- Automatic Cloudflare Turnstile CAPTCHA handling (auto-solve via 2captcha, with manual
  fallback and a "pause, don't skip" strategy so no data is lost)
- Crash-safe: browser session crashes are caught, data already scraped is saved, and a
  fresh browser session picks up automatically
- Export results to CSV, Excel, and JSON at every phase
- Cross-references dogs back to the events they competed in, and events back to their full
  detail record — so the final German Shepherd dataset is fully linked, not just flat rows
- Web-based control panel (Flask + HTML form) for one-off dog lookups
- Start scraper with one click (`start_scraper.py`)

## 🛠️ Installation & Setup

### 1. Clone the repository

```bash
git clone https://github.com/khaliqrehman952-source/working-dog-scraper.git
cd working-dog-scraper
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv venv
```

### 3. Activate the virtual environment

**Windows**
```bash
venv\Scripts\activate
```

**Linux/Mac**
```bash
source venv/bin/activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Set your Chrome version

Each scraper script has a `CHROME_MAIN_VERSION` variable near the top — set it to your
installed Chrome's major version (check at `chrome://settings/help`).

## ▶️ Running the pipeline

Run the phases in order — each one reads the previous phase's output:

```bash
python events_result_scraper.py          # Phase 1 — event listings
python events_result_detail_scraper.py   # Phase 2 — event details + dog lists
python german_shepherd_eventsdog_scraper.py   # Phase 3 — German Shepherd profiles
```

Phases 2 and 3 will ask you interactively for a **starting row / ending row** so you can
process the dataset in batches instead of all at once.

## 🌐 Running the standalone web control panel

For one-off dog-profile lookups (outside the events pipeline):

```bash
python start_scraper.py
```

- Open your browser → http://127.0.0.1:5000/
- Everything (scraper + control panel) runs from this one file.
- No need to run `api_server.py` separately.

## 👨‍💻 Author

Developed by Wajid Rehman Khanzada

## 📜 License

MIT License – Free to use and modify
