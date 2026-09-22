"""
H-1B slot watcher for GitHub Actions.
Reads the public VisaGrader India page (all 5 consulates), finds new H-1B
slot reports in the "Recent slot activity" table, and pushes them to your
phone through ntfy. Runs once per call; GitHub runs it every 15 minutes.
"""
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://visagrader.com/us-visa-time-slots-availability/india-ind"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CUTOFF = date.fromisoformat(os.environ.get("ALERT_IF_BEFORE") or "2027-06-30")
TYPES = {t.strip().lower() for t in
         (os.environ.get("APPOINTMENT_TYPES") or "interview,biometrics,dropbox").split(",")}
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
STATE = Path("slot_state.json")
FAIL_ALERT_AT = 4  # warn after this many failed checks in a row
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-h1b-slot-watcher)"}


def notify(title, body, priority="high"):
    requests.post(f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
                  headers={"Title": title, "Priority": priority, "Click": URL},
                  timeout=15)


def parse_date(text):
    for fmt in ("%m/%d/%Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def slot_rows(html):
    """Rows of the 'Recent slot activity' table as dicts, or None if missing."""
    soup = BeautifulSoup(html, "html.parser")
    heading = next((h for h in soup.find_all(["h2", "h3", "h4"])
                    if "recent slot activity" in h.get_text(" ", strip=True).lower()), None)
    table = heading.find_next("table") if heading else None
    if table is None:
        return None
    headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if cells and len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def main():
    if not TOPIC:
        sys.exit("NTFY_TOPIC secret is missing. Add it under Settings > Secrets > Actions.")

    first_run = not STATE.exists()
    state = {"seen": [], "fails": 0} if first_run else json.loads(STATE.read_text())

    try:
        resp = requests.get(URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        rows = slot_rows(resp.text)
        if rows is None:
            raise RuntimeError("slot table not found; the page layout may have changed")
    except Exception as e:
        state["fails"] += 1
        print(f"Check failed ({state['fails']} in a row): {e}")
        if state["fails"] == FAIL_ALERT_AT or MANUAL:
            notify("Slot watcher problem", f"Couldn't read the tracker: {e}", "default")
        STATE.write_text(json.dumps(state, indent=2))
        return

    state["fails"] = 0
    today = datetime.now(timezone.utc).date()
    seen = set(state["seen"])
    upcoming, fresh = 0, []

    for r in rows:
        visa = r.get("visa", "").lower().replace("-", "").replace(" ", "")
        kind = r.get("type", "").strip()
        d = parse_date(r.get("availability date", ""))
        if visa != "h1b" or kind.lower() not in TYPES or not d or d < today:
            continue
        upcoming += 1
        key = "|".join([r.get("consulate", ""), kind, d.isoformat(), r.get("reported", "")])
        if key not in seen:
            seen.add(key)
            if d <= CUTOFF:
                fresh.append((d, r.get("consulate", "?"), kind, r.get("count", "?")))

    if first_run:
        notify("Slot watcher is live", "Checking all 5 India consulates every ~15 min.", "low")
    if fresh:
        lines = [f"{c} - {k} - {d.strftime('%d %b %Y')} ({n} slots)"
                 for d, c, k, n in sorted(fresh)]
        notify("H-1B slot reported!",
               "\n".join(lines) + "\nTracker data can lag. Log in once and check.")
    elif MANUAL:
        notify("Manual check OK",
               f"{upcoming} upcoming H-1B entries on the page, none new.", "low")

    # Keep only entries whose slot date hasn't passed, so the file stays small.
    state["seen"] = sorted(k for k in seen if k.split("|")[2] >= today.isoformat())
    STATE.write_text(json.dumps(state, indent=2))
    print(f"OK: {upcoming} upcoming H-1B entries, {len(fresh)} new alerts.")


if __name__ == "__main__":
    main()
