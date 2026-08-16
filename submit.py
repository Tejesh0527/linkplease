import os
import sys
import json
import urllib.request

SUBMIT_URL = "https://pseudogram-api.onrender.com/v1/submit"


def submit():
    print("=== LINKPLEASE ASSIGNMENT SUBMISSION TOOL ===")
    
    email = os.getenv("SUBMIT_EMAIL", "")
    api_key = os.getenv("API_KEY", "")
    github_repo = os.getenv("GITHUB_REPO", "")
    working_url = os.getenv("WORKING_URL", "")
    loom_url = os.getenv("LOOM_URL", "")
    parts_completed = os.getenv("PARTS_COMPLETED", "A+B+C")
    start_date = os.getenv("START_DATE", "2026-08-16")

    if not email and not api_key:
        print("Error: Please set SUBMIT_EMAIL or API_KEY in environment or script.")
        print("Example: SUBMIT_EMAIL=your@email.com GITHUB_REPO=https://github.com/user/repo WORKING_URL=https://app.render.com LOOM_URL=https://loom.com/share/... python submit.py")
        sys.exit(1)

    payload = {
        "github_repo": github_repo,
        "working_url": working_url,
        "loom_url": loom_url,
        "parts_completed": parts_completed,
        "start_date": start_date
    }

    if email:
        payload["email"] = email
    else:
        payload["api_key"] = api_key

    print(f"Submitting payload:\n{json.dumps(payload, indent=2)}")

    confirm = input("\nSend submission to mock API? (y/N): ").strip().lower()
    if confirm != 'y':
        print("Submission canceled.")
        return

    try:
        req = urllib.request.Request(
            SUBMIT_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )
        res = urllib.request.urlopen(req)
        print("\n[SUCCESS] Submission accepted!")
        print(f"Response ({res.status}): {res.read().decode()}")
    except Exception as e:
        print(f"\n[ERROR] Submission failed: {e}")


if __name__ == "__main__":
    submit()
