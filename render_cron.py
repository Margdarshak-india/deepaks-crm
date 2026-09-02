import os
import sys
import requests

BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
SECRET = os.getenv("SCHEDULER_SECRET", "").strip()
if not BASE_URL or not SECRET:
    print("APP_BASE_URL and SCHEDULER_SECRET are required")
    sys.exit(1)
try:
    r = requests.post(BASE_URL + "/internal/run-scheduled-campaigns", headers={"X-Scheduler-Secret": SECRET}, timeout=300)
    print(r.text)
    r.raise_for_status()
except Exception as e:
    print(f"Scheduled campaign runner failed: {e}")
    sys.exit(1)
