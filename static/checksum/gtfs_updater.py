# =============================================================================
# gtfs_updater.py
# =============================================================================
# Project:      2026 FIFA Digital Display Dashboard - Kitsap Transit
# Intention:    Run a check for any changes to GTFS static feed from Kitsap Transit.
#               Downloads and extracts new TXT files when a feed
#               update is detected to keep static feed current.
#
# Order of Operations:
#   1. Reads feed_end_date from local feed_info.txt
#   2. Does nothing until 7 days before listed expiry date
#   3. Once in the check window, sends a HEAD request to the Kitsap GTFS URL
#      to check Last-Modified/ETag headers
#   4. If headers suggest change, downloads the ZIP and compares SHA256
#      checksum against the stored hash
#   5. If content is new, peeks inside ZIP to read new feed_start_date
#   6. If today >= feed_start_date, extracts TXT files into the static
#      folder & overwrites the old ones
#   7. If today < feed_start_date, holds the ZIP and checks again next night
#   8. Saves updated headers, hash, and timestamp to gtfs_update_state.json
#   9. On next run, reads the new feed_end_date and cycle begins anew
#
# Files:
#   Reads from : static\feed_info.txt          (feed_end_date)
#   Writes to  : static\*.txt                  (extracted GTFS files)
#                static\checksum\google_transit.zip
#                static\checksum\gtfs_update_state.json
#
# GTFS Feed URL:
#   https://pride.kitsaptransit.com/gtfs/google_transit.zip
#
# Scheduling (Windows Task Scheduler):
#   Trigger  : Daily at 12:00 AM
#   Program  : python
#   Arguments: "P:\Marketing Shared Folder\Digital-Display\2026 Digital Display Project for FIFA\static\checksum\gtfs_updater.py"
#   Log      : Redirect output to gtfs_updater.log in the checksum folder
# =============================================================================

import csv
import hashlib
import io
import json
import os
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

GTFS_URL = "https://pride.kitsaptransit.com/gtfs/google_transit.zip"
GTFS_DIR = r"P:\Marketing Shared Folder\Digital-Display\2026 Digital Display Project for FIFA\static"
ZIP_PATH = r"P:\Marketing Shared Folder\Digital-Display\2026 Digital Display Project for FIFA\static\checksum\google_transit.zip"
STATE_FILE = r"P:\Marketing Shared Folder\Digital-Display\2026 Digital Display Project for FIFA\static\checksum\gtfs_update_state.json"
FEED_INFO = r"P:\Marketing Shared Folder\Digital-Display\2026 Digital Display Project for FIFA\static\feed_info.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}

DAYS_BEFORE_EXPIRY = 7

def load_state():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def read_feed_dates_from_file(feed_info_path):
    """Read feed_start_date and feed_end_date from a feed_info.txt file path"""
    with open(feed_info_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            start = datetime.strptime(row["feed_start_date"].strip(), "%Y%m%d").date()
            end = datetime.strptime(row["feed_end_date"].strip(), "%Y%m%d").date()
            return start, end
    raise ValueError("Could not read feed dates from feed_info.txt")

def read_feed_dates_from_zip(zip_path):
    """Read feed_start_date and feed_end_date from feed_info.txt inside a ZIP"""
    with zipfile.ZipFile(zip_path, "r") as z:
        with z.open("feed_info.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                start = datetime.strptime(row["feed_start_date"].strip(), "%Y%m%d").date()
                end = datetime.strptime(row["feed_end_date"].strip(), "%Y%m%d").date()
                return start, end
    raise ValueError("Could not read feed dates from ZIP feed_info.txt")

def should_check_today(feed_end_date):
    """Return True if we are within 7 days of expiry, or past it"""
    today = datetime.now().date()
    check_from = feed_end_date - timedelta(days=DAYS_BEFORE_EXPIRY)
    return today >= check_from

def check_and_update():
    state = load_state()
    today = datetime.now().date()
    print(f"[{datetime.now()}] GTFS Updater starting...")

    # Step 1: Check feed_end_date from local feed_info.txt
    _, feed_end_date = read_feed_dates_from_file(FEED_INFO)
    check_from = feed_end_date - timedelta(days=DAYS_BEFORE_EXPIRY)

    print(f"  Current feed expires : {feed_end_date}")
    print(f"  Start checking from  : {check_from}")
    print(f"  Today                : {today}")

    if not should_check_today(feed_end_date):
        days_remaining = (check_from - today).days
        print(f"  Not yet in check window. Sleeping for {days_remaining} more day(s). Done.")
        return

    print(f"  In check window — checking Kitsap server for updates...")

    # Step 2: HEAD request
    req = urllib.request.Request(GTFS_URL, method="HEAD", headers=HEADERS)
    with urllib.request.urlopen(req) as resp:
        last_modified = resp.headers.get("Last-Modified", "")
        etag = resp.headers.get("ETag", "")

    print(f"  Last-Modified : {last_modified}")
    print(f"  ETag          : {etag}")

    if last_modified and last_modified == state.get("last_modified"):
        print("  No change detected via Last-Modified. Skipping.")
        return
    if etag and etag == state.get("etag"):
        print("  No change detected via ETag. Skipping.")
        return

    # Step 3: Download and checksum
    print("  Headers changed or absent — downloading ZIP...")
    req2 = urllib.request.Request(GTFS_URL, headers=HEADERS)
    with urllib.request.urlopen(req2) as resp, open(ZIP_PATH, "wb") as out:
        out.write(resp.read())

    new_hash = sha256(ZIP_PATH)
    print(f"  SHA256: {new_hash}")

    if new_hash == state.get("zip_hash"):
        print("  ZIP hash unchanged despite header difference. Skipping extraction.")
        save_state({**state, "last_modified": last_modified, "etag": etag})
        return

    # Step 4: Peek inside ZIP to check new feed_start_date before extracting
    new_start_date, new_end_date = read_feed_dates_from_zip(ZIP_PATH)
    print(f"  New feed start date  : {new_start_date}")
    print(f"  New feed end date    : {new_end_date}")

    if today < new_start_date:
        days_until_start = (new_start_date - today).days
        print(f"  New feed found but not yet valid (starts in {days_until_start} day(s)).")
        print(f"  ZIP saved to checksum folder. Will extract when start date arrives.")
        save_state({
            "last_modified": last_modified,
            "etag": etag,
            "zip_hash": new_hash,
            "pending_start_date": new_start_date.strftime("%Y%m%d"),
            "last_updated": state.get("last_updated", "")
        })
        return

    # Step 5: Start date has arrived — extract and replace
    print("  New content valid today — extracting...")
    os.makedirs(GTFS_DIR, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "r") as z:
        z.extractall(GTFS_DIR)

    save_state({
        "last_modified": last_modified,
        "etag": etag,
        "zip_hash": new_hash,
        "last_updated": datetime.now().isoformat()
    })

    print(f"  GTFS files updated successfully!")
    print(f"  New feed expires: {new_end_date} — next check window opens {new_end_date - timedelta(days=DAYS_BEFORE_EXPIRY)}")

if __name__ == "__main__":
    check_and_update()