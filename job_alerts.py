#!/usr/bin/env python3
"""
Hourly analyst-role alerts with H-1B sponsor screening.  v2

Changes from v1:
  - Wide analyst net (data/business/BI/ops/systems/FP&A/finance/SQL dev)
  - Scores the DESCRIPTION against goals, not just the title
  - Offline H-1B sponsor lookup from USCIS Employer Data Hub CSVs
  - Sends results by SMS

SETUP (one time)
----------------
1) pip install requests

2) H-1B data. Download the last 3 fiscal years from:
      https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
   Save the CSVs into ./h1b_data/ . Any filename is fine.
   Without this the script still runs; sponsor status just shows UNKNOWN.

3) SMS. Pick ONE:

   (a) Carrier email gateway - free, no account.
       Set SMS_MODE="email" and SMS_GATEWAY to your carrier:
         AT&T     txt.att.net
         Verizon  vtext.com
         T-Mobile tmomail.net
       Then set SMTP_USER / SMTP_PASS to a Gmail address + App Password.

   (b) Twilio - more reliable, ~$0.008/message.
       Set SMS_MODE="twilio" and fill the three TWILIO_* values.

4) Schedule it. Cron on a laptop only fires while the machine is awake.
   For real 24/7 use GitHub Actions (free) or any small VPS:
      0 * * * * /usr/bin/python3 /full/path/job_alerts.py >> jobs.log 2>&1
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests

from h1b_lookup import H1BIndex

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen_jobs.json")
TIMEOUT = 15
MIN_SCORE = 8
MAX_SMS_JOBS = 4          # keep the text readable

# --- SMS config -------------------------------------------------------------
PHONE = "nitheeshkumar87"
SMS_MODE = "email"
SMS_GATEWAY = "gmail.com"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")

# ---------------------------------------------------------------------------
# TARGET ROLES - the wide analyst net
# ---------------------------------------------------------------------------
TITLE_STRONG = [
    "data analyst", "business analyst", "business data analyst",
    "analytics engineer", "business intelligence analyst", "bi analyst",
    "bi developer", "business intelligence developer", "reporting analyst",
    "data engineer", "sql developer", "operations analyst",
    "operational analyst", "systems analyst", "fp&a analyst",
    "financial analyst", "finance analyst", "data analytics",
    "financial data analyst", "revenue analyst", "pricing analyst",
    "insights analyst", "decision support analyst", "quantitative analyst",
]
TITLE_WEAK = ["analyst", "analytics", "data", "reporting", "intelligence"]

TITLE_REJECT = [
    "nurse", "clinical", "laboratory", "chemist", "attorney", "paralegal",
    "recruiter", "sales representative", "account executive", "marketing manager",
    "policy analyst", "intelligence analyst", "crime analyst", "gis analyst",
    "security analyst", "soc analyst", "cyber", "network analyst", "help desk",
    "test analyst", "qa analyst",
]

# ---------------------------------------------------------------------------
# GOAL MATCHING - weighted against the resume, not just keyword presence
# ---------------------------------------------------------------------------
CORE_STACK = {                      # things he does daily - heaviest weight
    "sql": 4, "python": 4, "tableau": 3, "power bi": 3, "powerbi": 3,
    "etl": 3, "pandas": 2, "excel": 2, "postgresql": 3, "sql server": 3,
    "stored procedure": 2, "data pipeline": 3, "dashboard": 2,
}
SUPPORTING = {                      # real but secondary on his resume
    "azure": 2, "aws": 2, "data modeling": 2, "data warehouse": 2,
    "data quality": 2, "reconciliation": 3, "data validation": 2,
    "data migration": 3, "requirements": 2, "stakeholder": 2, "agile": 1,
    "jira": 1, "c#": 2, ".net": 1, "vb.net": 2, "rest api": 1,
    "scikit": 2, "xgboost": 2, "machine learning": 2, "forecasting": 2,
    "kpi": 1, "variance analysis": 1, "financial": 2, "regression": 1,
}
DOMAIN_BONUS = {                    # OTC + fraud/CLV projects give him an edge
    "finance": 1, "financial services": 2, "banking": 2, "insurance": 1,
    "fraud": 3, "payments": 2, "revenue": 1, "accounting": 1,
    "customer lifetime": 3, "churn": 2, "credit": 2, "tax": 2,
}

HARD_BLOCK = [
    "u.s. citizen", "us citizen", "citizenship is required", "must be a citizen",
    "security clearance", "ts/sci", "top secret", "secret clearance",
    "public trust", "polygraph", "green card holder only",
    "no sponsorship", "not sponsor", "unable to sponsor", "will not sponsor",
    "cannot sponsor", "no visa sponsorship", "without sponsorship",
    "not provide sponsorship", "not offer sponsorship",
]
SPONSOR_YES = [
    "visa sponsorship", "will sponsor", "h-1b", "h1b", "sponsor a visa",
    "sponsorship available", "cap-exempt", "opt", "stem opt",
]
TOO_SENIOR = [
    "10+ years", "12+ years", "15+ years", "9+ years", "8+ years", "7+ years",
    " director", "vice president", "head of ", "principal ", "staff ",
]

# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------
def score_job(title, description, company, h1b):
    t = (title or "").lower()
    d = (description or "").lower()
    blob = f"{t} {d}"
    notes = []

    for r in TITLE_REJECT:
        if r in t:
            return 0, [f"off-target title ({r})"], ""
    for b in HARD_BLOCK:
        if b in blob:
            return 0, [f"BLOCKED: {b}"], ""

    # --- title relevance (0-3)
    if any(s in t for s in TITLE_STRONG):
        score = 3
    elif any(w in t for w in TITLE_WEAK):
        score = 1
        notes.append("loose title match")
    else:
        return 0, ["title not analyst-adjacent"], ""

    # --- description vs goals (0-5)
    core = sum(w for k, w in CORE_STACK.items() if k in blob)
    supp = sum(w for k, w in SUPPORTING.items() if k in blob)
    dom = sum(w for k, w in DOMAIN_BONUS.items() if k in blob)
    score += min(core // 4, 3) + min(supp // 5, 1) + min(dom // 3, 1)

    hits = [k for k in list(CORE_STACK) + list(SUPPORTING) if k in blob]
    if hits:
        notes.append("matches: " + ", ".join(sorted(hits)[:7]))

    # --- fit adjustments
    if any(s in blob for s in TOO_SENIOR):
        score -= 3
        notes.append("over-senior (-3)")
    if "master" in blob:
        score += 1
        notes.append("master's valued (+1)")
    if re.search(r"\b(0-2|1\+|2\+|3\+)\s*years", blob):
        score += 1
        notes.append("experience bar fits (+1)")

    # --- sponsorship (the whole point of the exercise)
    info = h1b.lookup(company) if h1b else None
    label = info["status"] if info else "UNKNOWN"

    if info and info.get("cap_exempt"):
        # No lottery at all. Biggest single advantage available.
        score += 3
        label = "CAP-EXEMPT"
        notes.append(info["summary"])
    elif label == "SPONSOR":
        score += 2
        notes.append(info["summary"])
        # Their own median tells you what wage level they file at, which now
        # drives lottery odds directly.
        if info.get("pct_high_wage", 0) >= 50:
            score += 1
            notes.append("files mostly Level III-IV (+1)")
    elif label == "NO RECORD":
        score -= 2
        notes.append("NO H-1B filings FY24-26 (-2)")

    if any(x in blob for x in SPONSOR_YES):
        score += 2
        notes.append("*** POSTING MENTIONS SPONSORSHIP ***")

    return max(0, min(score, 10)), notes, label


# ---------------------------------------------------------------------------
# BOARDS - see --verify before trusting UNVERIFIED entries
# ---------------------------------------------------------------------------
GREENHOUSE = [("itD Tech", "itd"), ("Juvare", "juvare"), ("Upstart", "upstart")]
SMARTREC = [("National Vision", "NationalVision1"), ("Wise", "Wise")]
WORKDAY = [
    ("Abbott", "abbott", "wd5", "abbottcareers"),
    ("U.S. Bank", "usbank", "wd1", "US_Bank_Careers"),
    ("RBC", "rbc", "wd3", "RBCGLOBAL1"),
    ("PwC", "pwc", "wd3", "US_Experienced_Careers"),
    ("Huron", "huron", "wd1", "huroncareers"),
    ("CareSource", "caresource", "wd1", "CareSource"),
    ("Velera", "velera", "wd5", "VeleraCareers"),
]
WD_TERMS = ["data analyst", "business analyst", "financial analyst",
            "business intelligence", "operations analyst", "systems analyst"]

UA = {"User-Agent": "Mozilla/5.0"}


def fetch_greenhouse(name, token):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                     timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return [{"company": name, "title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""),
             "description": re.sub(r"<[^>]+>", " ", j.get("content", "") or ""),
             "id": f"gh:{token}:{j.get('id')}"} for j in r.json().get("jobs", [])]


def fetch_smartrec(name, cid):
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100",
                     timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append({"company": name, "title": j.get("name", ""),
                    "location": f"{loc.get('city','')}, {loc.get('region','')}".strip(", "),
                    "url": f"https://jobs.smartrecruiters.com/{cid}/{j.get('id')}",
                    "description": j.get("name", ""),   # list endpoint has no body
                    "id": f"sr:{cid}:{j.get('id')}"})
    return out


def fetch_workday(name, tenant, host, site):
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    seen, out = set(), []
    for term in WD_TERMS:
        try:
            r = requests.post(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                              json={"appliedFacets": {}, "limit": 20,
                                    "offset": 0, "searchText": term},
                              timeout=TIMEOUT,
                              headers={**UA, "Accept": "application/json"})
            r.raise_for_status()
            for j in r.json().get("jobPostings", []):
                p = j.get("externalPath", "")
                if p in seen:
                    continue
                seen.add(p)
                out.append({"company": name, "title": j.get("title", ""),
                            "location": j.get("locationsText", ""),
                            "url": f"{base}/en-US/{site}{p}",
                            "description": " ".join(
                                [j.get("title", "")] + (j.get("bulletFields") or [])),
                            "id": f"wd:{tenant}:{p}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results for any search term")
    return out


SOURCES = ([("greenhouse", fetch_greenhouse, a) for a in GREENHOUSE]
           + [("smartrecruiters", fetch_smartrec, a) for a in SMARTREC]
           + [("workday", fetch_workday, a) for a in WORKDAY])


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------
def send_sms(body):
    if SMS_MODE == "off":
        print("[sms off]\n" + body)
        return
    try:
        if SMS_MODE == "twilio":
            requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={"From": TWILIO_FROM, "To": f"+1{PHONE}", "Body": body[:1500]},
                timeout=TIMEOUT).raise_for_status()
        else:
            msg = MIMEText(body[:1500])
            msg["From"], msg["To"], msg["Subject"] = SMTP_USER, f"{PHONE}@{SMS_GATEWAY}", ""
            s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
            s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg); s.quit()
        print("  sms sent")
    except Exception as e:
        print(f"  ! sms failed: {type(e).__name__} {e}")


# ---------------------------------------------------------------------------
def main():
    verify = "--verify" in sys.argv
    h1b = H1BIndex()
    if not verify:
        print(f"H-1B index: {len(h1b):,} employers"
              if len(h1b) else "H-1B index: EMPTY - run build_h1b_index.py first")

    seen = set()
    if not verify and os.path.exists(STATE_FILE):
        try:
            seen = set(json.load(open(STATE_FILE)))
        except Exception:
            pass
    first_run = not seen and not verify

    matches, errors = [], []
    for kind, fn, args in SOURCES:
        label = f"{args[0]} ({kind})"
        try:
            jobs = fn(*args)
            if verify:
                print(f"  OK      {label}: {len(jobs)}")
                continue
            for j in jobs:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                if first_run:
                    continue
                sc, notes, spon = score_job(j["title"], j["description"],
                                            j["company"], h1b)
                if sc >= MIN_SCORE:
                    j.update(score=sc, notes=notes, sponsor=spon)
                    matches.append(j)
        except Exception as e:
            errors.append(f"  FAILED  {label}: {type(e).__name__} {e}")

    if verify:
        print("\n".join(errors) if errors else "  all boards reachable")
        return

    json.dump(sorted(seen), open(STATE_FILE, "w"))
    stamp = datetime.now(timezone.utc).astimezone().strftime("%m/%d %H:%M")

    if first_run:
        print(f"Seeded {len(seen)} postings. Alerts start next run.")
    elif matches:
        matches.sort(key=lambda m: -m["score"])
        lines = [f"{len(matches)} new analyst match(es) {stamp}"]
        for m in matches[:MAX_SMS_JOBS]:
            lines.append(f"\n[{m['score']}/10] {m['title']} - {m['company']}"
                         f" ({m['sponsor']})\n{m['url']}")
        if len(matches) > MAX_SMS_JOBS:
            lines.append(f"\n+{len(matches)-MAX_SMS_JOBS} more in the log")
        send_sms("\n".join(lines))
        for m in matches:
            print(f"[{m['score']}/10] {m['title']} - {m['company']} [{m['sponsor']}]"
                  f"\n   {m['location']}\n   {m['url']}\n   {'; '.join(m['notes'])}\n")
    else:
        print(f"[{stamp}] nothing new at >= {MIN_SCORE}")

    if errors:
        print("\n".join(errors))


if __name__ == "__main__":
    main()
