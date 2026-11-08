"""Start the study site:  python serve.py   then open http://127.0.0.1:8000

Only listens on this computer (127.0.0.1) -- the exams aren't ours to share.
"""
import argparse
import webbrowser

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    print(f"Exam bank running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    uvicorn.run("webapp.app:app", host="127.0.0.1", port=a.port, log_level="warning")
