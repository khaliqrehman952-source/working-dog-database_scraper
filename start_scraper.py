import os
import subprocess
import webbrowser
import sys

# Project ka exact path
PROJECT_DIR = r"C:\Users\hp 640\Desktop\working dog scraper comp"

# Flask server run karne ka command (venv ke saath)
command = [
    os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe"),
    os.path.join(PROJECT_DIR, "api_server.py")
]

# first open the browser
webbrowser.open("http://127.0.0.1:5000/")

#and then run bakend 
subprocess.run(command, cwd=PROJECT_DIR)
