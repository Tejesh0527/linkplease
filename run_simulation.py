import os
import sys
import time
import json
import urllib.request
from dotenv import load_dotenv

load_dotenv()

MOCK_API_BASE = "https://pseudogram-api.onrender.com"
API_KEY = os.getenv("API_KEY", "")
APP_BASE = os.getenv("APP_BASE", "http://localhost:8000")


def run():
    print(f"=== LINKPLEASE SIMULATION LOAD TEST ===")
    print(f"Target Webhook: {APP_BASE}/webhook")
    print(f"API Key: {API_KEY[:10]}...")

    # 1. Ensure a rule exists
    try:
        rule_req = urllib.request.Request(
            f"{APP_BASE}/rules",
            data=json.dumps({"keyword": "PRICE", "dm_message": "Price list is $99"}).encode(),
            headers={"Content-Type": "application/json"}
        )
        res = urllib.request.urlopen(rule_req)
        print(f"[RULES] Rule created/verified: {res.read().decode()}")
    except Exception as e:
        print(f"[RULES] Could not create rule on local app: {e}")

    # 2. Trigger Simulation
    sim_payload = {
        "webhook_url": f"{APP_BASE}/webhook",
        "count": 500,
        "duration_seconds": 10
    }
    req = urllib.request.Request(
        f"{MOCK_API_BASE}/v1/simulate/start",
        data=json.dumps(sim_payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY}
    )

    try:
        res = urllib.request.urlopen(req)
        data = json.loads(res.read().decode())
        run_id = data.get("run_id")
        print(f"[SIMULATE] Simulation launched! Run ID: {run_id}")
    except Exception as e:
        print(f"[SIMULATE] Error launching simulation: {e}")
        return

    # 3. Monitor local /stats and poll truth log
    print("\n[MONITORING] Waiting for events to process...")
    for i in range(60):
        time.sleep(2)
        try:
            stats_res = urllib.request.urlopen(f"{APP_BASE}/stats")
            stats = json.loads(stats_res.read().decode())
            print(f"  T+{ (i+1)*2 }s -> /stats: sent={stats['sent']}, queued={stats['queued']}, failed={stats['failed']}, duplicates_blocked={stats['duplicates_blocked']}")
        except Exception as err:
            print(f"  Error fetching /stats: {err}")

    # 4. Fetch Server Truth
    try:
        truth_req = urllib.request.Request(
            f"{MOCK_API_BASE}/v1/simulate/{run_id}/truth",
            headers={"X-API-Key": API_KEY}
        )
        res = urllib.request.urlopen(truth_req)
        truth = json.loads(res.read().decode())
        print("\n=== MOCK API SERVER-SIDE TRUTH ===")
        print(json.dumps(truth, indent=2))
    except Exception as e:
        print(f"Error fetching truth: {e}")


if __name__ == "__main__":
    run()
