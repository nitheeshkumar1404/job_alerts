#!/usr/bin/env python3
"""
h1b_lookup.py — sponsor screening against the FY2024-26 index.

Import from job_alerts.py:
    from h1b_lookup import H1BIndex
    h1b = H1BIndex()
    info = h1b.lookup("Capital One")

Matching is TOKEN-PREFIX, not substring. Plain substring matching produces
false hits that look plausible and are badly wrong — "TridentCare" matches an
unrelated employer called "CARE", "Security Mutual" matches "Curi Holdings".
Requiring one name's tokens to be a leading prefix of the other's keeps
"Abbott" -> "Abbott Laboratories" while rejecting those.

Where several entities match, the highest-volume one wins: "Amazon" should
resolve to Amazon.com Services (5,055 LCAs), not a 2-LCA shell entity.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(HERE, "h1b_index.json")

SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|"
    r"lp|llp|plc|holdings|holding|group|services|service|solutions|technologies|"
    r"technology|systems|international|worldwide|global|usa|na|"
    r"national association|the)\b\.?", re.I)


def norm_tokens(name):
    n = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    n = SUFFIXES.sub(" ", n)
    return [t for t in n.split() if t]


# Posting name -> legal filing name. Add to this as you hit misses.
ALIASES = {
    "PWC": "PricewaterhouseCoopers",
    "EY": "Ernst & Young",
    "KPMG": "KPMG LLP",
    "DELOITTE": "Deloitte Consulting",
    "JPMORGAN": "JPMorgan Chase",
    "JP MORGAN": "JPMorgan Chase",
    "GOOGLE": "Google LLC",
    "META": "Meta Platforms",
    "FACEBOOK": "Meta Platforms",
    "UHG": "UnitedHealth",
    "UNITEDHEALTH GROUP": "UnitedHealth",
    "RBC": "Royal Bank of Canada",
    "USAA": "United Services Automobile Association",
    "IBM": "IBM Corporation",
    "AWS": "Amazon Web Services",
    "TCS": "Tata Consultancy",
    "EPAM": "EPAM Systems",
    "UBC": "United BioSource",
}


class H1BIndex:
    def __init__(self, path=INDEX_FILE):
        self.idx = {}
        self.tokens = {}
        if os.path.exists(path):
            with open(path) as f:
                self.idx = json.load(f)
            self.tokens = {k: k.split() for k in self.idx}

    def __len__(self):
        return len(self.idx)

    def lookup(self, company):
        """
        Returns None if no index loaded, else a dict:
          status        SPONSOR | NO RECORD
          cap_exempt    bool
          summary       one-line human string
          plus the raw record fields
        """
        if not self.idx:
            return None
        raw = re.sub(r"[^A-Z0-9 &]", " ", str(company or "").upper()).strip()
        raw = re.sub(r"\s+", " ", raw)
        if raw in ALIASES:
            company = ALIASES[raw]
        toks = norm_tokens(company)
        if not toks or len(" ".join(toks)) < 3:
            return {"status": "NO RECORD", "cap_exempt": False,
                    "summary": "name too short to match"}

        key = " ".join(toks)
        cands = []
        if key in self.idx:
            cands.append((key, self.idx[key]))

        for k, ktoks in self.tokens.items():
            if k == key:
                continue
            short, long_ = (toks, ktoks) if len(toks) <= len(ktoks) else (ktoks, toks)
            # One name's tokens must lead the other's, and a single-token
            # match has to be a real word, not an abbreviation fragment.
            if long_[:len(short)] == short and (len(short) > 1 or len(short[0]) >= 4):
                cands.append((k, self.idx[k]))

        if not cands:
            return {"status": "NO RECORD", "cap_exempt": False,
                    "summary": "no H-1B filings FY2024-26"}

        # Prefer the largest filer among the plausible matches.
        _, rec = max(cands, key=lambda c: c[1]["lcas"] + c[1]["approvals"])

        bits = []
        if rec["lcas"]:
            bits.append(f"{rec['lcas']:,} LCAs")
        if rec["approvals"]:
            bits.append(f"{rec['approvals']:,} approvals")
        if rec["years"]:
            bits.append("/".join(y.replace("FY", "") for y in rec["years"]))
        if rec["median_salary"]:
            bits.append(f"med ${rec['median_salary']:,}")
        if rec["pct_high_wage"]:
            bits.append(f"{rec['pct_high_wage']}% L3-4")

        out = dict(rec)
        out["status"] = "SPONSOR"
        out["summary"] = (("CAP-EXEMPT — no lottery. " if rec["cap_exempt"] else "")
                          + f"{rec['name']}: " + ", ".join(bits))
        return out


if __name__ == "__main__":
    import sys
    h = H1BIndex()
    print(f"{len(h):,} employers indexed\n")
    for name in (sys.argv[1:] or ["Capital One", "Amazon", "TridentCare"]):
        r = h.lookup(name)
        print(f"{name:34s} {r['status']:10s} {r['summary']}" if r else "no index")
