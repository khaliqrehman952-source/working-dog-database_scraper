

# 🐶 Working Dog Scraper

This is a **Python-based web scraper** for [Working-Dog](https://www.working-dog.com/) profiles.
It allows users to scrape dog profile data and export it into multiple formats with an easy-to-use control panel.

---

## 🚀 Features

* Scrape dog profiles with Selenium + Python
* Export results to:

  * CSV
  * Excel
  * JSON
* Web-based control panel (Flask + HTML form)
* Start scraper with one click (`start_scraper.py`)

---

## 🛠️ Installation & Setup

### 1. Clone the repository

```bash
git clone https://github.com/khaliqrehman952-source/working-dog-scraper.git
cd working-dog-scraper
```

### 2. Create virtual environment (recommended)

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

### 5. Run the scraper & control panel

```bash
python start_scraper.py
```

* Open your browser → [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
* Everything (scraper + control panel) runs from this one file.
* No need to run `api_server.py` separately.

---

## 👨‍💻 Author

Developed by **Wajid Rehman Khanzada**

---

## 📜 License

MIT License – Free to use and modify

---


