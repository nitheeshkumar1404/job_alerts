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
import time
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests

from h1b_lookup import H1BIndex

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen_jobs.json")
TIMEOUT = 15
# Only used when TITLE_MATCH_ONLY is False. In title-match mode every match
# returns 10, so this gate never blocks anything.
MIN_SCORE = 8

# --- MATCHING MODE ---------------------------------------------------------
# True  = pure title matching. Any title containing an INCLUDE term and no
#         EXCLUDE term is emailed, regardless of how the description scores.
#         More email, nothing missed.
# False = the original 0-10 resume scoring with MIN_SCORE as the cut-off.
#         Less email, but a badly-worded description can hide a good job.
TITLE_MATCH_ONLY = True

# ("Analyst" OR "BI Developer" OR "SQL Developer" OR "Business Intelligence
#  Developer" OR "Data Engineer" OR "Analytics Engineer" OR "Data Analytics")
INCLUDE_TERMS = [
    "analyst", "bi developer", "sql developer",
    "business intelligence developer", "data engineer",
    "analytics engineer", "data analytics",
]
# --- LOCATION FILTER -------------------------------------------------------
# Without this you get Hyderabad, Toronto and Warsaw postings - these boards
# are global. Logic: reject anything carrying a known non-US marker, accept
# everything else (a bare "Remote" or an unfamiliar US city still gets
# through, which is the safer direction to err).
US_ONLY = True

NON_US_MARKERS = [
    # countries
    "india", "canada", "united kingdom", "england", "scotland", "ireland",
    "germany", "france", "spain", "italy", "netherlands", "belgium",
    "poland", "romania", "portugal", "sweden", "norway", "denmark",
    "finland", "switzerland", "austria", "czech", "hungary", "greece",
    "australia", "new zealand", "singapore", "japan", "china", "korea",
    "philippines", "malaysia", "indonesia", "vietnam", "thailand",
    "brazil", "mexico", "argentina", "chile", "colombia", "peru",
    "israel", "turkey", "egypt", "nigeria", "kenya", "south africa",
    "uae", "dubai", "saudi", "qatar", "morocco", "algeria", "ukraine",
    "russia", "costa rica", "panama", "luxembourg", "iceland", "malta",
    "taiwan", "hong kong", "pakistan", "bangladesh", "sri lanka",
    # cities that appear without a country
    "hyderabad", "bangalore", "bengaluru", "chennai", "mumbai", "pune",
    "delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
    "jaipur", "indore", "kochi", "coimbatore", "trivandrum",
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "halifax",
    "mississauga", "waterloo", "edmonton", "winnipeg", "quebec",
    "london", "manchester", "birmingham", "edinburgh", "glasgow",
    "dublin", "belfast", "cork", "amsterdam", "berlin", "munich",
    "hamburg", "frankfurt", "paris", "lyon", "madrid", "barcelona",
    "milan", "rome", "warsaw", "krakow", "prague", "budapest",
    "bucharest", "lisbon", "stockholm", "oslo", "copenhagen",
    "helsinki", "zurich", "geneva", "vienna", "brussels", "athens",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "wellington",
    "tokyo", "osaka", "seoul", "beijing", "shanghai", "shenzhen",
    "taguig", "manila", "cebu", "jakarta", "bangkok", "kuala lumpur",
    "sao paulo", "mexico city", "bogota", "santiago", "buenos aires",
    "tel aviv", "cairo", "lagos", "nairobi", "johannesburg",
    "putrajaya", "selangor", "clonmel", "witney", "abingdon", "olst",
    "weesp", "heerlen", "breda", "basel", "porto", "algiers",
]


def is_us(location):
    """False when the location clearly isn't in the US."""
    if not US_ONLY:
        return True
    loc = (location or "").lower()
    if not loc:
        return True          # unknown - let it through rather than lose a job
    for m in NON_US_MARKERS:
        if re.search(rf"\b{re.escape(m)}\b", loc):
            return False
    return True


# NOT ("Lead" OR "Principal" OR "Staff" OR "Director" OR "Manager" OR "Head"
#  OR "VP" OR "President" OR "Chief" OR "Security" OR "Cyber" OR "SOC" OR "QA"
#  OR "Quality" OR "Clinical" OR "Nurse" OR "Policy" OR "Crime" OR
#  "Intelligence Analyst" OR "GIS" OR "Lab" OR "Laboratory")
EXCLUDE_TERMS = [
    "lead", "principal", "staff", "director", "manager", "head",
    "vp", "vice president", "president", "chief",
    "security", "cyber", "soc", "qa", "quality", "clinical", "nurse",
    "policy", "crime", "intelligence analyst", "gis", "lab", "laboratory",
]
MAX_SMS_JOBS = 4          # only used for SMS modes; email shows all

# --- Alert delivery ---------------------------------------------------------
# "email"  -> sends to ALERT_TO (recommended; turn on Gmail push notifications
#             on your phone and you get a buzz within seconds, with clickable
#             links and a searchable history)
# "sms"    -> carrier email-to-SMS gateway. Unreliable on MVNOs like Mint,
#             which silently drop gateway messages.
# "twilio" -> paid but reliable SMS
# "off"    -> print to console only
ALERT_MODE = "email"
ALERT_TO = "nitheeshkumar87@gmail.com"   # where alerts land
PHONE = "4058771675"
SMS_GATEWAY = "tmomail.net"           # only used when ALERT_MODE = "sms"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")

# --- Apify (LinkedIn + Indeed coverage) ------------------------------------
# Optional. If APIFY_TOKEN is not set, the script silently skips this source
# and just polls the company boards as before.
#
# FREE PLAN FITS: Apify's free tier gives $5 of credit per month, no card
# required, and BLOCKS runs when it's exhausted - there is no overage and no
# surprise bill. At roughly $0.06/run that's ~80 runs/month, so the schedule
# below (3x/day on weekdays, ~65 runs) stays inside the free credit.
# If credit does run out, this source just goes quiet; the free company
# boards keep polling hourly regardless.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR = "fantastic-jobs~advanced-linkedin-job-search-api"
APIFY_LIMIT = 20        # fewer results = less credit per run
# Local hours (24h) when Apify is allowed to run. Postings cluster in the
# morning; overnight runs mostly pay for empty results.
# Set to None to run every hour.
APIFY_HOURS = (8, 12, 16)           # 3x/day local time
APIFY_WEEKDAYS_ONLY = True          # skip Sat/Sun - almost nothing posts

# --- Adzuna (FREE aggregator: LinkedIn/Indeed/Monster/CareerBuilder etc) ----
# Free tier is 1,000 API calls per month. Running hourly, 24/7, uses ~744.
# To stay inside that we make exactly ONE call per run and rotate which
# search term is used each hour, so all terms get covered across a day.
# Register free at https://developer.adzuna.com/ - you get an app_id and an
# app_key (two values, not one token).
ADZUNA_ID = os.environ.get("ADZUNA_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_KEY", "")
ADZUNA_TERMS = [
    "data analyst", "business analyst", "business intelligence analyst",
    "bi developer", "sql developer", "data engineer",
    "reporting analyst", "operations analyst", "financial analyst",
    "analytics engineer", "systems analyst", "data analytics",
]

# ---------------------------------------------------------------------------
# TARGET ROLES - the wide analyst net
# ---------------------------------------------------------------------------
TITLE_STRONG = [
    "data analyst", "business analyst", "business data analyst",
    "analytics engineer", "business intelligence analyst", "bi analyst",
    "bi developer", "business intelligence developer", "reporting analyst",
    "data engineer", "sql developer", "operations analyst",
    "operational analyst", "systems analyst", "fp&a analyst", "fpa analyst",
    "financial analyst", "finance analyst", "data analytics",
    "financial data analyst", "revenue analyst", "pricing analyst",
    "insights analyst", "decision support analyst", "quantitative analyst",
    "business systems analyst", "technology analyst", "product analyst",
]

# Hard title exclusions - seniority levels above his experience.
# Word-boundary matched so "Leadership" or "Headcount" don't false-trigger.
# NOTE: "Senior"/"Sr" are deliberately NOT here. Senior-titled roles asking
# for 3 years are a good fit for him and sit at higher H-1B wage levels;
# the TOO_SENIOR check below penalises actual year requirements instead.
TITLE_EXCLUDE = [
    "lead", "principal", "staff", "director", "manager", "head",
    "vp", "vice president", "president", "chief", "avp", "svp",
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
def title_matches(title):
    """Pure boolean title filter. Word-boundary matched so 'Leadership'
    doesn't trip 'lead' and 'Equality' doesn't trip 'quality'."""
    t = (title or "").lower()
    for x in EXCLUDE_TERMS:
        if re.search(rf"\b{re.escape(x.strip())}\b", t):
            return False, x
    for inc in INCLUDE_TERMS:
        if re.search(rf"\b{re.escape(inc.strip())}", t):
            return True, inc
    return False, None


def score_job(title, description, company, h1b):
    t = (title or "").lower()
    d = (description or "").lower()
    blob = f"{t} {d}"
    notes = []

    if TITLE_MATCH_ONLY:
        ok, term = title_matches(title)
        if not ok:
            return 0, [f"title filter: {term or 'no include term'}"], ""
        # Still attach H-1B context - it is the most useful thing in the alert.
        info = h1b.lookup(company) if h1b else None
        label = info["status"] if info else "UNKNOWN"
        notes = [f"matched '{term}'"]
        if info and info.get("cap_exempt"):
            label = "CAP-EXEMPT"
            notes.append(info["summary"])
        elif label == "SPONSOR":
            notes.append(info["summary"])
        elif label == "NO RECORD":
            notes.append("no H-1B filings FY24-26")
        if any(x in blob for x in SPONSOR_YES):
            notes.append("*** POSTING MENTIONS SPONSORSHIP ***")
        return 10, notes, label

    for r in TITLE_REJECT:
        if r in t:
            return 0, [f"off-target title ({r})"], ""
    for x in TITLE_EXCLUDE:
        if re.search(rf"\b{re.escape(x)}\b", t):
            return 0, [f"excluded seniority ({x})"], ""
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
# Greenhouse boards. Many large tech sponsors use Greenhouse for at least
# part of their hiring. Run --verify: slugs change and some will 404.
# Verified 2026-08-11. Snowflake/DoorDash/Uber/Plaid/Rippling 404'd and were
# removed - they've moved off Greenhouse or use a different slug.
GREENHOUSE = [
    ("itD Tech", "itd"), ("Juvare", "juvare"), ("Upstart", "upstart"),
    ("Stripe", "stripe"), ("Databricks", "databricks"),
    ("Airbnb", "airbnb"), ("Robinhood", "robinhood"),
    ("Cloudflare", "cloudflare"), ("Coinbase", "coinbase"),
    ("Instacart", "instacart"), ("Reddit", "reddit"), ("Discord", "discord"),
    ("Twilio", "twilio"), ("Datadog", "datadog"), ("Affirm", "affirm"),
    ("Chime", "chime"), ("Gusto", "gusto"), ("Samsara", "samsara"),
    ("Lyft", "lyft"), ("Figma", "figma"), ("Anthropic", "anthropic"),
    ("Scale AI", "scaleai"),
    ("Brex", "brex"),      # timed out once on verify; usually fine
]

SMARTREC = [("National Vision", "NationalVision1"), ("Wise", "Wise"),
            ("Visa", "Visa"), ("Bosch", "BoschGroup")]

# --- Big-sponsor boards with their own APIs (all free, no token) ----------
# Ranked from the user's own H-1B workbook. Amazon, Google, Microsoft, Meta,
# Apple and NVIDIA together file 60,000+ LCAs a year.
LEVER = [("Palantir", "palantir"), ("Shield AI", "shieldai")]
ASHBY = [("Ramp", "ramp"), ("Notion", "notion"), ("OpenAI", "openai")]
# VERIFIED = seen working. GUESS = plausible slug, run --verify and delete
# any that fail. Workday tenants move (Capital One went wd1 -> wd12), so
# re-run --verify every few months.
WORKDAY = [
    # All verified working 2026-08-11/12. Anything not here failed --verify:
    # the slug was wrong. Use find_boards.py or read the slug off the
    # company's careers URL, then add it back and re-verify.
    ("Abbott",           "abbott",     "wd5",  "abbottcareers"),
    ("U.S. Bank",        "usbank",     "wd1",  "US_Bank_Careers"),
    ("RBC",              "rbc",        "wd3",  "RBCGLOBAL1"),
    ("PwC",              "pwc",        "wd3",  "US_Experienced_Careers"),
    ("Huron",            "huron",      "wd1",  "huroncareers"),
    ("CareSource",       "caresource", "wd1",  "CareSource"),
    ("Velera",           "velera",     "wd5",  "VeleraCareers"),
    ("Capital One",      "capitalone", "wd12", "Capital_One"),
    ("Salesforce",       "salesforce", "wd12", "External_Career_Site"),
    ("Intel",            "intel",      "wd1",  "External"),
    ("Adobe",            "adobe",      "wd5",  "external_experienced"),
    ("PayPal",           "paypal",     "wd1",  "jobs"),
    ("eBay",             "ebay",       "wd5",  "apply"),
    # cap-exempt (no H-1B lottery)
    ("UT Austin",        "utaustin",   "wd1",  "UTstaff"),
    ("Texas A&M",        "tamus",      "wd1",  "TAMU_External"),
    ("Univ of Chicago",  "uchicago",   "wd5",  "External"),
    ("Ohio State Univ",  "osu",        "wd1",  "OSUCareers"),
    ("St. Jude",         "stjude",     "wd1",  "StJude"),

    # --- Found by searching live myworkdayjobs.com URLs (2026-08-12).
    # These are real tenant/host/site values, not guesses - but re-run
    # --verify after adding: tenants do migrate (Capital One wd1 -> wd12).
    ("Wells Fargo",      "wf",         "wd1",  "WellsFargoJobs"),
    ("Morgan Stanley",   "ms",         "wd5",  "External"),
    ("NVIDIA",           "nvidia",     "wd5",  "NVIDIAExternalCareerSite"),
    ("Mizuho Americas",  "mizuho",     "wd1",  "mizuhoamericas"),
    ("MUFG",             "mufgub",     "wd3",  "MUFG-Careers"),
    ("Workday",          "workday",    "wd5",  "Workday"),
    ("Cushman&Wakefield","cw",         "wd1",  "External"),
    ("Accenture",        "accenture",  "wd103","AccentureCareers"),
    ("Cisco",            "cisco",      "wd5",  "Cisco_Careers"),
    ("Citi",             "citi",       "wd5",  "2"),
    ("Amgen",            "amgen",      "wd1",  "Careers"),
    ("Ciena",            "ciena",      "wd5",  "Careers"),
    ("PATH",             "path",       "wd1",  "External"),
]

# Workday rate-limits bursts. Two broad terms cover the same ground as six
# narrow ones ("analyst" matches data/business/financial/systems analyst) and
# cuts request volume by 3x.
WD_TERMS = ["analyst", "data"]
THROTTLE = 0.4          # seconds between HTTP calls, everywhere

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


def fetch_lever(name, slug):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                     timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return [{"company": name, "title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j.get("hostedUrl", ""),
             "description": re.sub(r"<[^>]+>", " ",
                                   j.get("descriptionPlain") or
                                   j.get("description", "") or ""),
             "id": f"lv:{slug}:{j.get('id')}"} for j in r.json()]


def fetch_ashby(name, slug):
    r = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return [{"company": name, "title": j.get("title", ""),
             "location": j.get("location", ""),
             "url": j.get("jobUrl", ""),
             "description": re.sub(r"<[^>]+>", " ", j.get("descriptionHtml", "") or ""),
             "id": f"ab:{slug}:{j.get('id')}"} for j in r.json().get("jobs", [])]


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
        time.sleep(THROTTLE)
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


SEARCH_TERMS = ["data analyst", "business analyst", "business intelligence",
                "data engineer", "financial analyst", "operations analyst"]


def fetch_amazon(name="Amazon"):
    """amazon.jobs has a free public JSON search. Amazon is the largest H-1B
    sponsor in the US, so this is a high-value board to watch."""
    seen, out = set(), []
    for term in SEARCH_TERMS:
        try:
            r = requests.get("https://www.amazon.jobs/en/search.json",
                             params={"base_query": term, "country": "USA",
                                     "result_limit": 50, "sort": "recent",
                                     "offset": 0},
                             timeout=TIMEOUT, headers=UA)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                path = j.get("job_path", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "company": "Amazon",
                    "title": j.get("title", ""),
                    "location": j.get("location", "") or j.get("normalized_location", ""),
                    "url": f"https://www.amazon.jobs{path}",
                    "description": " ".join(filter(None, [
                        j.get("description", ""),
                        j.get("basic_qualifications", ""),
                        j.get("preferred_qualifications", "")]))[:6000],
                    "id": f"az_amzn:{path}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results for any search term")
    return out


def fetch_microsoft(name="Microsoft"):
    """Microsoft's careers search API is public and unauthenticated."""
    seen, out = set(), []
    for term in SEARCH_TERMS:
        try:
            r = requests.get(
                "https://gcsservices.careers.microsoft.com/search/api/v1/search",
                params={"q": term, "lc": "United States", "l": "en_us",
                        "pg": 1, "pgSz": 20, "o": "Recent", "flt": "true"},
                timeout=TIMEOUT, headers=UA)
            r.raise_for_status()
            res = (r.json().get("operationResult") or {}).get("result") or {}
            for j in res.get("jobs", []):
                jid = str(j.get("jobId", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = j.get("properties", {}).get("locations") or []
                out.append({
                    "company": "Microsoft",
                    "title": j.get("title", ""),
                    "location": locs[0] if locs else "",
                    "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                    "description": " ".join(filter(None, [
                        j.get("properties", {}).get("description", ""),
                        j.get("properties", {}).get("responsibilities", ""),
                        j.get("properties", {}).get("qualifications", "")]))[:6000],
                    "id": f"ms:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results for any search term")
    return out


def fetch_google(name="Google"):
    """Google's careers app: JSON API under /about/careers/applications/."""
    seen, out = set(), []
    for term in SEARCH_TERMS:
        try:
            r = requests.get(
                "https://www.google.com/about/careers/applications/api/v3/search/",
                params={"q": term, "location": "United States",
                        "page_size": 20, "sort_by": "date"},
                timeout=TIMEOUT, headers=UA)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                jid = j.get("id", "") or j.get("job_id", "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = j.get("locations") or []
                slug = (j.get("apply_url") or "").strip()
                out.append({
                    "company": "Google",
                    "title": j.get("title", ""),
                    "location": (locs[0].get("display") if locs and
                                 isinstance(locs[0], dict) else ""),
                    "url": slug or f"https://www.google.com/about/careers/applications/jobs/results/{jid}",
                    "description": " ".join(filter(None, [
                        j.get("description", ""),
                        " ".join(j.get("qualifications", []) if
                                 isinstance(j.get("qualifications"), list) else
                                 [j.get("qualifications", "")])]))[:6000],
                    "id": f"goog:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - endpoint may have changed")
    return out


def fetch_meta(name="Meta"):
    """Meta careers GraphQL-backed search."""
    out = []
    try:
        r = requests.post(
            "https://www.metacareers.com/graphql",
            data={"doc_id": "9114524511922157",
                  "variables": json.dumps({"search_input": {
                      "q": "analyst", "divisions": [], "offices": [],
                      "roles": [], "leadership_levels": [],
                      "sub_teams": [], "teams": []}})},
            timeout=TIMEOUT, headers=UA)
        r.raise_for_status()
        res = (((r.json().get("data") or {}).get("job_search_with_featured_jobs")
                or {}).get("all_jobs") or [])
        for j in res:
            jid = j.get("id", "")
            if not jid:
                continue
            out.append({
                "company": "Meta",
                "title": j.get("title", ""),
                "location": ", ".join(j.get("locations") or []),
                "url": f"https://www.metacareers.com/jobs/{jid}/",
                "description": " ".join(filter(None, [
                    j.get("description", ""),
                    " ".join(j.get("qualifications") or []),
                    " ".join(j.get("teams") or [])]))[:6000],
                "id": f"meta:{jid}"})
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")
    if not out:
        raise RuntimeError("no results - endpoint may have changed")
    return out


def fetch_nvidia(name="NVIDIA"):
    """NVIDIA is on Workday - reuse that fetcher with their tenant."""
    return fetch_workday("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite")


def fetch_apple(name="Apple"):
    """Apple's jobs search API is public."""
    seen, out = set(), []
    for term in SEARCH_TERMS[:3]:
        try:
            r = requests.post(
                "https://jobs.apple.com/api/v1/search",
                json={"query": term, "page": 0, "locale": "en-us",
                      "sort": "newest",
                      "filters": {"locations": ["postLocation-USA"]}},
                timeout=TIMEOUT,
                headers={**UA, "Content-Type": "application/json"})
            r.raise_for_status()
            for j in r.json().get("searchResults", []):
                jid = j.get("positionId", "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "company": "Apple",
                    "title": j.get("postingTitle", ""),
                    "location": (j.get("locations") or [{}])[0].get("name", ""),
                    "url": f"https://jobs.apple.com/en-us/details/{jid}",
                    "description": " ".join(filter(None, [
                        j.get("jobSummary", ""),
                        j.get("minimumQualifications", ""),
                        j.get("keyQualifications", "")]))[:6000],
                    "id": f"appl:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - endpoint may have changed")
    return out


# --- Oracle Recruiting Cloud (ORC) -----------------------------------------
# Used by a lot of large employers that aren't on Workday. Public REST API.
# (site_number, site_name) come from the careers URL:
#   https://fa-XXXX.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/
# (Display Name, full hostname, site code) - read both off the careers URL:
#   https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001
#            └──── hostname ────┘                                    └ site ┘
ORACLE_CLOUD = [
    ("GuideWell/Florida Blue", "fa-etum-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1"),
    ("JPMorgan Chase",         "jpmc.fa.oraclecloud.com",                    "CX_1001"),
]


# --- Radancy / TalentBrew career sites --------------------------------------
# Used by Mayo Clinic and many large health systems, retailers and banks.
# The site is jobs.<company>.org or .com and it exposes a JSON search at
# /search-jobs/results. Add entries as (Display Name, hostname).
RADANCY = [
    ("Mayo Clinic", "jobs.mayoclinic.org"),
    ("Citi",        "jobs.citi.com"),
]


def fetch_radancy(name, host):
    """Radancy/TalentBrew JSON search endpoint."""
    seen, out = set(), []
    for term in SEARCH_TERMS[:4]:
        try:
            r = requests.get(f"https://{host}/search-jobs/results",
                             timeout=TIMEOUT,
                             headers={**UA, "Accept": "application/json",
                                      "X-Requested-With": "XMLHttpRequest"},
                             params={"ActiveFacetID": 0, "CurrentPage": 1,
                                     "RecordsPerPage": 50, "Distance": 50,
                                     "RadiusUnitType": 0, "Keywords": term,
                                     "Location": "", "ShowRadius": "False",
                                     "IsPagination": "False",
                                     "SearchResultsModuleName": "Search Results",
                                     "SearchFiltersModuleName": "Search Filters",
                                     "SortCriteria": 0, "SortDirection": 0,
                                     "SearchType": 5})
            r.raise_for_status()
            data = r.json()
            # Radancy returns either a results list or an HTML blob.
            items = data.get("results") or []
            if not items and isinstance(data.get("results_html"), str):
                html = data["results_html"]
                for m in re.finditer(
                        r'href="(/job/[^"]+)"[^>]*>.*?<h2[^>]*>(.*?)</h2>.*?'
                        r'<span class="job-location">(.*?)</span>', html, re.S):
                    path, title, loc = m.groups()
                    if path in seen:
                        continue
                    seen.add(path)
                    out.append({"company": name,
                                "title": re.sub(r"<[^>]+>", "", title).strip(),
                                "location": re.sub(r"<[^>]+>", "", loc).strip(),
                                "url": f"https://{host}{path}",
                                "description": title,
                                "id": f"rad:{host}:{path}"})
                continue
            for j in items:
                jid = str(j.get("jobId") or j.get("id") or j.get("url", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                url = j.get("url", "")
                if url and not url.startswith("http"):
                    url = f"https://{host}{url}"
                out.append({"company": name,
                            "title": j.get("title", ""),
                            "location": j.get("location", "") or
                                        j.get("formattedLocation", ""),
                            "url": url,
                            "description": " ".join(filter(None, [
                                j.get("title", ""),
                                j.get("descriptionTeaser", ""),
                                j.get("category", "")])),
                            "id": f"rad:{host}:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - endpoint or host may be wrong")
    return out


# --- Phenom People career sites ---------------------------------------------
# Used by Cognizant, Microsoft (apply.careers.microsoft.com) and many large
# employers. Exposes a JSON widget endpoint at /widgets.
PHENOM = [
    ("Cognizant", "careers.cognizant.com"),
    ("Microsoft", "apply.careers.microsoft.com"),
    ("Qualcomm",  "careers.qualcomm.com"),
    ("Walmart",   "careers.walmart.com"),
]


# --- SAP SuccessFactors career sites ----------------------------------------
# URL shape: https://careers.<company>.com/search/?q=...  or  /go/<name>/<id>/
# The JSON is at /search/?q=<term>&sortColumn=referencedate&sortDirection=desc
# with an AJAX header. Used by EY, HCL and many large employers.
SUCCESSFACTORS = [
    ("Ernst & Young", "careers.ey.com"),
    ("HCL Tech",      "careers.hcltech.com"),
]


def fetch_successfactors(name, host):
    seen, out = set(), []
    for term in SEARCH_TERMS[:4]:
        try:
            r = requests.get(f"https://{host}/search/", timeout=TIMEOUT,
                             headers={**UA, "X-Requested-With": "XMLHttpRequest",
                                      "Accept": "application/json, text/html"},
                             params={"q": term, "sortColumn": "referencedate",
                                     "sortDirection": "desc",
                                     "optionsFacetsDD_country": "US"})
            r.raise_for_status()
            # SuccessFactors returns HTML; job rows carry a jobtitle link.
            for m in re.finditer(
                    r'<a[^>]+href="(/job/[^"]+)"[^>]*class="jobTitle-link"[^>]*>'
                    r'(.*?)</a>.*?<span class="jobLocation">(.*?)</span>',
                    r.text, re.S):
                path, title, loc = m.groups()
                if path in seen:
                    continue
                seen.add(path)
                out.append({
                    "company": name,
                    "title": re.sub(r"<[^>]+>", "", title).strip(),
                    "location": re.sub(r"\s+", " ",
                                       re.sub(r"<[^>]+>", "", loc)).strip(),
                    "url": f"https://{host}{path}",
                    "description": re.sub(r"<[^>]+>", "", title),
                    "id": f"sf:{host}:{path}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - host or markup may have changed")
    return out


def fetch_phenom(name, host):
    seen, out = set(), []
    for term in SEARCH_TERMS[:4]:
        try:
            r = requests.get(f"https://{host}/widgets", timeout=TIMEOUT,
                             headers={**UA, "Accept": "application/json"},
                             params={"keyword": term, "location": "United States",
                                     "limit": 50, "page": 1, "sortBy": "Most recent",
                                     "ddoKey": "refineSearch"})
            r.raise_for_status()
            jobs = (((r.json().get("refineSearch") or {}).get("data") or {})
                    .get("jobs") or [])
            for j in jobs:
                jid = str(j.get("jobId") or j.get("jobSeqNo") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "company": name,
                    "title": j.get("title", ""),
                    "location": j.get("location", "") or
                                f"{j.get('city','')}, {j.get('state','')}".strip(", "),
                    "url": j.get("applyUrl", "") or j.get("jobUrl", "") or
                           f"https://{host}/job/{jid}",
                    "description": " ".join(filter(None, [
                        j.get("title", ""), j.get("descriptionTeaser", ""),
                        j.get("category", ""), j.get("jobFamily", "")]))[:4000],
                    "id": f"ph:{host}:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - host or endpoint may be wrong")
    return out


def fetch_goldman(name="Goldman Sachs"):
    """higher.gs.com exposes a public job search API."""
    seen, out = set(), []
    for term in SEARCH_TERMS[:4]:
        try:
            r = requests.get("https://higher.gs.com/services/careers/search",
                             timeout=TIMEOUT,
                             headers={**UA, "Accept": "application/json"},
                             params={"query": term, "page": 1, "pageSize": 50,
                                     "sort": "RELEVANCE"})
            r.raise_for_status()
            for j in (r.json().get("results") or r.json().get("jobs") or []):
                jid = str(j.get("jobId") or j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "company": "Goldman Sachs",
                    "title": j.get("jobTitle") or j.get("title", ""),
                    "location": ", ".join(filter(None, [
                        j.get("city", ""), j.get("state", "")])) or
                        j.get("location", ""),
                    "url": f"https://higher.gs.com/roles/{jid}",
                    "description": " ".join(filter(None, [
                        j.get("jobTitle", ""), j.get("division", ""),
                        j.get("jobSummary", "")]))[:4000],
                    "id": f"gs:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - endpoint may have changed")
    return out


def fetch_oracle_cloud(name, host, site):
    """Oracle Recruiting Cloud public candidate-experience API."""
    url = (f"https://{host}/hcmRestApi/resources/latest/"
           "recruitingCEJobRequisitions")
    out = []
    for term in SEARCH_TERMS[:3]:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=UA, params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": (f"findReqs;siteNumber={site},limit=50,"
                           f"keyword={term},sortBy=POSTING_DATES_DESC")})
            r.raise_for_status()
            items = (r.json().get("items") or [{}])[0].get("requisitionList", [])
            for j in items:
                jid = j.get("Id", "")
                if not jid:
                    continue
                out.append({
                    "company": name, "title": j.get("Title", ""),
                    "location": j.get("PrimaryLocation", ""),
                    "url": (f"https://{host}/hcmUI/CandidateExperience/"
                            f"en/sites/{site}/job/{jid}"),
                    "description": " ".join(filter(None, [
                        j.get("ShortDescriptionStr", ""),
                        j.get("JobFamily", ""),
                        j.get("Title", "")]))[:4000],
                    "id": f"orc:{host}:{jid}"})
        except Exception:
            continue
    if not out:
        raise RuntimeError("no results - check host/site slug")
    return out


def fetch_tesla(name="Tesla"):
    """Tesla runs its own careers API."""
    r = requests.get("https://www.tesla.com/cua-api/apps/careers/state",
                     params={"site": "US"}, timeout=TIMEOUT,
                     headers={**UA, "Accept": "application/json",
                              "Referer": "https://www.tesla.com/careers/search/"})
    r.raise_for_status()
    d = r.json()
    lookup = d.get("lookup", {})
    out = []
    for j in (d.get("listings") or []):
        jid = j.get("id", "")
        if not jid:
            continue
        out.append({
            "company": "Tesla", "title": j.get("t", ""),
            "location": lookup.get("location", {}).get(str(j.get("l", "")), ""),
            "url": f"https://www.tesla.com/careers/search/job/{jid}",
            "description": " ".join(filter(None, [
                j.get("t", ""),
                lookup.get("department", {}).get(str(j.get("dp", "")), "")])),
            "id": f"tsla:{jid}"})
    if not out:
        raise RuntimeError("no results - endpoint may have changed")
    return out


def fetch_adzuna(name="Adzuna (LinkedIn/Indeed/etc)"):
    """One call per run, rotating search terms by hour to stay in the free
    1,000-calls/month tier. Adzuna aggregates LinkedIn, Indeed, Monster and
    others, so this fills the gap the company boards leave."""
    if not (ADZUNA_ID and ADZUNA_KEY):
        raise RuntimeError("ADZUNA_ID/ADZUNA_KEY not set - skipping")

    # Rotate the term so all of them get covered over the course of a day.
    term = ADZUNA_TERMS[datetime.now().hour % len(ADZUNA_TERMS)]

    r = requests.get(
        "https://api.adzuna.com/v1/api/jobs/us/search/1",
        params={"app_id": ADZUNA_ID, "app_key": ADZUNA_KEY,
                "results_per_page": 50, "what_phrase": term,
                "max_days_old": 1, "sort_by": "date",
                "content-type": "application/json"},
        timeout=TIMEOUT, headers=UA)
    r.raise_for_status()

    out = []
    for j in r.json().get("results", []):
        url = j.get("redirect_url", "")
        if not url:
            continue
        out.append({
            "company": (j.get("company") or {}).get("display_name", "") or "Unknown",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "url": url,
            "description": re.sub(r"<[^>]+>", " ", j.get("description", "") or ""),
            "id": f"az:{j.get('id')}",
        })
    return out


def fetch_apify(name="LinkedIn/Indeed (Apify)"):
    """Pull the last hour of postings via Apify. Raises on any failure so the
    caller reports it like any other board - a silent failure here would be
    money spent for nothing."""
    if not APIFY_TOKEN:
        return []          # not configured; not an error
    now = datetime.now()
    if APIFY_WEEKDAYS_ONLY and now.weekday() >= 5:
        raise RuntimeError("weekend - skipping to save credit")
    if APIFY_HOURS is not None and now.hour not in APIFY_HOURS:
        raise RuntimeError("outside APIFY_HOURS - skipping to save credit")

    r = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {APIFY_TOKEN}",
                 "Content-Type": "application/json"},
        json={
            "timeRange": "1h",
            "limit": APIFY_LIMIT,
            "removeAgency": True,
            "descriptionType": "text",
            "locationSearch": ["United States"],
            "titleSearch": ["Data Analyst", "Business Analyst", "Data Engineer",
                            "Analytics Engineer", "Business Intelligence Analyst",
                            "BI Analyst", "BI Developer", "SQL Developer",
                            "Reporting Analyst", "Operations Analyst",
                            "Systems Analyst", "Financial Analyst"],
            "aiWorkArrangementFilter": ["On-site", "Hybrid",
                                        "Remote OK", "Remote Solely"],
        },
        timeout=180)
    r.raise_for_status()

    out = []
    for j in r.json():
        locs = j.get("locations_derived") or []
        # Fold the AI-extracted fields into one blob so scoring sees them.
        desc = " ".join(filter(None, [
            j.get("ai_requirements_summary", ""),
            " ".join(j.get("ai_key_skills") or []),
            j.get("ai_experience_level", ""),
            "visa sponsorship" if j.get("ai_visa_sponsorship") else "",
            (j.get("description") or "")[:4000],
        ]))
        url = j.get("url", "")
        if not url:
            continue
        out.append({"company": j.get("organization", "") or "Unknown",
                    "title": j.get("title", ""),
                    "location": (locs[0] if locs else ""),
                    "url": url,
                    "description": desc,
                    "id": f"ap:{url}"})
    return out


# Google/Meta/Apple/Microsoft/Tesla use internal APIs that rejected our
# requests (403/400/no results). Their fetchers are left in the file above so
# you can revive them if you find working endpoints, but they are NOT
# registered here - a failing source costs ~15s of every hourly run.
# NOT REGISTERED: radancy (Mayo, Citi), phenom (Cognizant, Microsoft,
# Qualcomm, Walmart), successfactors (EY, HCL), goldman, apple, tesla, google.
# Their endpoints were inferred from careers-page URLs and all returned nothing
# on two separate verify runs. The fetchers remain above so they can be revived
# if the real API shape is found (browser DevTools -> Network tab -> XHR while
# searching on the site gives the exact request), but leaving them registered
# costs ~15s per source per run for no results.
SOURCES = ([("oracle_cloud", fetch_oracle_cloud, a) for a in ORACLE_CLOUD]
           + [("lever", fetch_lever, a) for a in LEVER]
           + [("ashby", fetch_ashby, a) for a in ASHBY]
           + [("amazon", fetch_amazon, ()), ("apify", fetch_apify, ())]
           + [("greenhouse", fetch_greenhouse, a) for a in GREENHOUSE]
           + [("smartrecruiters", fetch_smartrec, a) for a in SMARTREC]
           + [("workday", fetch_workday, a) for a in WORKDAY])


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------
def send_alert(body, subject="Job alerts", html=None):
    if ALERT_MODE == "off":
        print("[alerts off]\n" + body)
        return
    try:
        if ALERT_MODE == "twilio":
            requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={"From": TWILIO_FROM, "To": f"+1{PHONE}", "Body": body[:1500]},
                timeout=TIMEOUT).raise_for_status()
            print("  sms sent (twilio)")
        elif ALERT_MODE == "sms":
            msg = MIMEText(body[:1500])
            msg["From"], msg["To"], msg["Subject"] = SMTP_USER, f"{PHONE}@{SMS_GATEWAY}", ""
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
            srv.starttls(); srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg); srv.quit()
            print("  sms sent (gateway)")
        else:
            if html:
                msg = MIMEMultipart("alternative")
                msg.attach(MIMEText(body, "plain"))
                msg.attach(MIMEText(html, "html"))
            else:
                msg = MIMEText(body)
            msg["From"], msg["To"], msg["Subject"] = SMTP_USER, ALERT_TO, subject
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
            srv.starttls(); srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg); srv.quit()
            print(f"  email sent to {ALERT_TO}")
    except Exception as e:
        print(f"  ! alert failed: {type(e).__name__} {e}")


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
        time.sleep(THROTTLE)
        label = f"{args[0] if args else kind.title()} ({kind})"
        try:
            try:
                jobs = fn(*args)
            except Exception:
                time.sleep(3)          # backoff, then one retry
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
                if not is_us(j.get("location", "")):
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
        exempt = sum(1 for m in matches if m["sponsor"] == "CAP-EXEMPT")
        subject = f"{len(matches)} job match{'es' if len(matches) > 1 else ''}"
        if exempt:
            subject += f" ({exempt} cap-exempt)"

        text, html = [f"{len(matches)} new match(es) at {stamp}\n"], [
            "<div style=\"font-family:-apple-system,Segoe UI,sans-serif;"
            "max-width:600px\">",
            f"<p style=\"color:#666\">{len(matches)} new match(es) &middot; {stamp}</p>"]

        for m in matches:
            tag = ("#0a7d3c" if m["sponsor"] == "CAP-EXEMPT"
                   else "#1a56c4" if m["sponsor"] == "SPONSOR" else "#999")
            text.append(f"\n[{m['score']}/10] {m['title']} - {m['company']}"
                        f"\n  {m['location']}  |  {m['sponsor']}"
                        f"\n  {m['url']}"
                        + ("\n  " + "; ".join(m["notes"]) if m["notes"] else ""))
            html.append(
                f"<div style=\"border-left:3px solid {tag};padding:4px 0 4px 12px;"
                f"margin:16px 0\">"
                f"<div><b style=\"font-size:15px\">{m['title']}</b> &mdash; {m['company']}</div>"
                f"<div style=\"color:#666;font-size:13px;margin:3px 0\">"
                f"{m['score']}/10 &middot; {m['location'] or 'location n/a'} &middot; "
                f"<b style=\"color:{tag}\">{m['sponsor']}</b></div>"
                + (f"<div style=\"color:#888;font-size:12px\">"
                   f"{'; '.join(m['notes'])}</div>" if m["notes"] else "")
                + f"<div style=\"margin-top:6px\"><a href=\"{m['url']}\">Apply</a></div>"
                f"</div>")

        html.append("</div>")
        send_alert("\n".join(text), subject, "".join(html))
        for m in matches:
            print(f"[{m['score']}/10] {m['title']} - {m['company']} [{m['sponsor']}]"
                  f"\n   {m['location']}\n   {m['url']}\n   {'; '.join(m['notes'])}\n")
    else:
        mode = ("no new matching titles" if TITLE_MATCH_ONLY
                else f"nothing new at >= {MIN_SCORE}")
        print(f"[{stamp}] {mode}")

    if errors:
        print("\n".join(errors))


if __name__ == "__main__":
    main()
