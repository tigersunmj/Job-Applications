"""
Quant keyword filters + monitoring of target companies' own career sites.

Target companies live in data/target_companies.csv (group, type, company, url).

How official-site monitoring works:
  1. DISCOVERY (at most once every DISCOVERY_MAX_AGE_DAYS): for each target
     company, fetch its listed URL (plus a few likely careers pages) and look
     for a known applicant-tracking system (Greenhouse, Lever, Ashby, Workday,
     SmartRecruiters). Results are cached in data/ats_sources.json and a
     human-readable report is written to data/ats_coverage.md.
     Manual additions go in data/ats_overrides.json, e.g.
       {"Some Company": [{"ats": "greenhouse", "token": "somecompany"}]}
  2. POLLING (every run): read each discovered job board's public JSON feed.
     The FIRST time a board is seen, everything currently on it is recorded
     as "already seen" and nothing is reported (existing postings don't
     count). After that, only postings that are new AND look like a quant
     internship in the US are reported.

State lives in data/site_state.json.
"""
import concurrent.futures
import csv
import datetime
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

HERE = os.path.dirname(__file__)
DATA_DIR = os.path.join(HERE, "..", "data")
TARGETS_PATH = os.path.join(DATA_DIR, "target_companies.csv")
SOURCES_PATH = os.path.join(DATA_DIR, "ats_sources.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "ats_overrides.json")
COVERAGE_PATH = os.path.join(DATA_DIR, "ats_coverage.md")
STATE_PATH = os.path.join(DATA_DIR, "site_state.json")

DISCOVERY_MAX_AGE_DAYS = 7
HTTP_TIMEOUT = 10
MAX_WORKERS = 24
HEADERS = {"User-Agent": "Mozilla/5.0 (internship-tracker; personal job alert bot)"}

# ---------------------------------------------------------------------------
# Keyword rules (case-insensitive, matched against the job TITLE and, for
# Simplify postings, its category). Edit these to tune what gets reported.
# ---------------------------------------------------------------------------
# Counts as quant at ANY company.
STRICT_KEYWORDS = [
    r"quant",               # quant, quantitative, quants
    r"trading", r"trader",
    r"algorithmic", r"systematic",
    r"derivatives", r"\bstrats?\b", r"quantitative strategist",
    r"financial engineer",
]
# Also counts at companies on YOUR list (data / modeling / risk / strategy).
BROAD_KEYWORDS = [
    r"data scien", r"data analy", r"data engineer", r"analytics",
    r"machine learning", r"\bml\b", r"\bai\b", r"artificial intelligence",
    r"statistic", r"econometric", r"model(l)?ing", r"\bmodel",
    r"risk analy", r"risk model", r"market risk", r"credit risk",
    r"model risk", r"quantitative risk", r"risk management",
    r"investment strateg", r"trading strateg", r"strategist", r"portfolio analy",
    r"portfolio construct", r"research scientist", r"\bdata\b",
]
# Also counts at QR-core firms (a plain "Research Intern" there is quant).
QR_CORE_KEYWORDS = [r"research"]
# Never counts, even if something above matched.
EXCLUDE_KEYWORDS = [
    r"quantum", r"pharmacolog", r"data cent(er|re)", r"datacenter",
    r"data entry", r"master data", r"assurance", r"audit", r"\btax\b",
    r"\bsales\b", r"marketing", r"recruit", r"human resources",
]
INTERN_PATTERN = re.compile(
    r"intern\b|internship|co-?op\b|summer (analyst|associate|20\d\d)|"
    r"\b20\d\d summer|placement|student",
    re.I,
)

_STRICT = [re.compile(p, re.I) for p in STRICT_KEYWORDS]
_BROAD = [re.compile(p, re.I) for p in BROAD_KEYWORDS]
_QR = [re.compile(p, re.I) for p in QR_CORE_KEYWORDS]
_EXCL = [re.compile(p, re.I) for p in EXCLUDE_KEYWORDS]


def _any(patterns, text):
    return any(p.search(text) for p in patterns)


def quant_match(title: str, category: str = "", group: str = None) -> bool:
    """group: None (not on the list), "qr_core", or "non_qr"."""
    title = title or ""
    category = category or ""
    if _any(_EXCL, title):
        return False
    if "quant" in category.lower() or _any(_STRICT, title):
        return True
    if group is None:
        return False
    if _any(_BROAD, title):
        return True
    return group == "qr_core" and _any(_QR, title)


def is_internship(title: str) -> bool:
    return bool(INTERN_PATTERN.search(title or ""))


# ---------------------------------------------------------------------------
# Target company list + name matching
# ---------------------------------------------------------------------------
_SUFFIXES = re.compile(
    r"\b(llc|l\.?l\.?c|inc|incorporated|lp|l\.p|llp|ltd|limited|corp|"
    r"corporation|co|company|group|holdings|plc|sa|se|ag|n\.?a)\b\.?",
    re.I,
)
ALIASES = {
    "hrt": "hudson river trading",
    "sig": "susquehanna international",
    "susquehanna": "susquehanna international",
    "ernst young": "ey",
    "boston consulting group bcg": "bcg",
    "boston consulting group": "bcg",
    "jp morgan chase": "jpmorgan chase",
    "jpmorgan": "jpmorgan chase",
    "j p morgan": "jpmorgan chase",
    "the voleon": "voleon",
    "standard poor s": "s p global",
    "citadel llc": "citadel",
    "peak 6": "peak6",
    "tgs": "tgs management",
    "renaissance": "renaissance technologies",
    "ihs markit": "s p global",
}


def normalize_company(name: str) -> str:
    s = (name or "").lower().replace("&", " ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = _SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = ALIASES.get(s, s)
    return s.replace(" ", "")


_GENERIC = re.compile(
    r"(assetmanagement|capitalmanagement|investmentmanagement|management|"
    r"investments?|investors|capital|partners|technologies|financial|global|"
    r"advisors|associates|trading|securities)$")


def _loose(key: str) -> str:
    prev = None
    while key and key != prev:
        prev = key
        stripped = _GENERIC.sub("", key)
        if len(stripped) >= 3:
            key = stripped
    return key


def load_targets() -> list:
    if not os.path.exists(TARGETS_PATH):
        return []
    with open(TARGETS_PATH, newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("company")]


def target_lookup(targets: list) -> dict:
    """normalized name -> group ("qr_core" wins over "non_qr")."""
    out = {}
    for t in targets:
        key = normalize_company(t["company"])
        for k in (key, "~" + _loose(key)):
            if key and out.get(k) != "qr_core":
                out[k] = t["group"]
    return out


def company_group(company: str, lookup: dict):
    key = normalize_company(company)
    return lookup.get(key) or lookup.get("~" + _loose(key))


# ---------------------------------------------------------------------------
# Location handling
# ---------------------------------------------------------------------------
US_HINTS = [
    "united states", "usa", "u.s.", "new york", "nyc", "chicago", "boston",
    "san francisco", "austin", "houston", "dallas", "miami", "greenwich",
    "stamford", "philadelphia", "seattle", "los angeles", "princeton",
    "jersey city", "charlotte", "atlanta", "denver", "washington",
    "bala cynwyd", "westport", "evanston", "jupiter", "berkeley",
    "irvine", "minneapolis", "radnor", "rowayton", "pittsburgh", "tampa",
    "salt lake", "palo alto", "menlo park", "mountain view", "cambridge, ma",
    "remote - us", "remote, us", "remote (us",
]
NON_US_HINTS = [
    "london", "amsterdam", "paris", "hong kong", "singapore", "sydney",
    "dublin", "zurich", "zürich", "geneva", "tokyo", "shanghai", "beijing",
    "mumbai", "bangalore", "bengaluru", "gurgaon", "gurugram", "hyderabad",
    "toronto", "montreal", "vancouver", "frankfurt", "tel aviv", "warsaw",
    "madrid", "milan", "luxembourg", "cayman", "rotterdam", "munich",
    "united kingdom", "uk", "canada", "india", "germany", "france",
    "netherlands", "switzerland", "australia", "china", "japan", "ireland",
    "poland", "israel", "cyprus", "limassol", "dubai", "abu dhabi",
    "st. helier", "jersey, channel", "sao paulo", "mexico", "manila",
]


def classify_location(text: str) -> str:
    """"us", "non_us", or "unknown"."""
    t = (text or "").lower()
    if not t.strip():
        return "unknown"
    if any(h in t for h in US_HINTS) or re.search(
        r",\s*(ny|il|ma|ca|tx|fl|ct|nj|pa|wa|co|ga|nc|dc|mn|ut)\b", t
    ):
        return "us"
    if any(re.search(r"\b" + re.escape(h) + r"\b", t) for h in NON_US_HINTS):
        return "non_us"
    return "unknown"


# ---------------------------------------------------------------------------
# ATS discovery
# ---------------------------------------------------------------------------
ATS_PATTERNS = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._%-]+)")),
    ("workday", re.compile(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
]
BAD_TOKENS = {"embed", "js", "v1", "jobs", "job", "api", "wday", "en-us", "search", "login"}
CAREER_LINK = re.compile(r'href=["\']([^"\']+)["\']', re.I)
CAREER_WORDS = re.compile(r"career|jobs|join|opportunit|students|campus|work-with-us|work-at", re.I)


def _get(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
    except requests.RequestException:
        pass
    return None, None


def find_ats(html: str) -> list:
    found = []
    for ats, pat in ATS_PATTERNS:
        for m in pat.finditer(html):
            if ats == "workday":
                tenant, wd, site = m.group(1), m.group(2), m.group(3)
                if site.lower() in BAD_TOKENS or tenant in ("www", "wd1", "wd3", "wd5"):
                    continue
                src = {"ats": "workday", "tenant": tenant, "wd": wd, "site": site}
            else:
                token = m.group(1).strip("./")
                if not token or token.lower() in BAD_TOKENS:
                    continue
                src = {"ats": ats, "token": token}
            if src not in found:
                found.append(src)
    return found


def discover_one(company: dict) -> list:
    url = (company.get("url") or "").strip()
    if not url.startswith("http"):
        return []
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    pages = [url, base + "/careers", base + "/careers/", base + "/jobs"]
    seen_pages, found = set(), []
    first_html = None
    for p in pages:
        if p in seen_pages:
            continue
        seen_pages.add(p)
        html, final = _get(p)
        if not html:
            continue
        if first_html is None:
            first_html = (html, final or p)
        for src in find_ats(html) + find_ats(final or ""):
            if src not in found:
                found.append(src)
        if found:
            return found
    # One more hop: follow a few career-looking links from the first page.
    if first_html:
        html, page_url = first_html
        links = []
        for href in CAREER_LINK.findall(html):
            if CAREER_WORDS.search(href):
                full = urljoin(page_url, href)
                if full not in seen_pages and full not in links:
                    links.append(full)
        for link in links[:4]:
            for src in find_ats(link):
                if src not in found:
                    found.append(src)
            h, final = _get(link)
            if h:
                for src in find_ats(h) + find_ats(final or ""):
                    if src not in found:
                        found.append(src)
            if found:
                break
    return found


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return default


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def source_key(company: str, src: dict) -> str:
    if src["ats"] == "workday":
        return f"workday:{src['tenant']}:{src['site']}"
    return f"{src['ats']}:{src['token']}"


def load_sources(targets: list, now: datetime.datetime) -> dict:
    """company -> [sources]; re-runs discovery when the cache is stale."""
    cache = _read_json(SOURCES_PATH, {})
    last = cache.get("_discovered_at")
    stale = True
    if last:
        try:
            age = now - datetime.datetime.fromisoformat(last.rstrip("Z"))
            stale = age > datetime.timedelta(days=DISCOVERY_MAX_AGE_DAYS)
        except ValueError:
            pass
    listed = {t["company"] for t in targets}
    if not stale and listed <= set(cache.get("companies", {})):
        sources = cache.get("companies", {})
    else:
        sources = {}
        with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as pool:
            results = pool.map(discover_one, targets)
            for t, found in zip(targets, results):
                sources[t["company"]] = found
        _write_json(SOURCES_PATH, {"_discovered_at": now.isoformat() + "Z", "companies": sources})
        write_coverage(targets, sources)
    overrides = _read_json(OVERRIDES_PATH, {})
    merged = {k: list(v) for k, v in sources.items()}
    for company, extra in overrides.items():
        if company.startswith("_"):
            continue
        merged.setdefault(company, [])
        for src in extra:
            if src not in merged[company]:
                merged[company].append(src)
    return merged


def write_coverage(targets: list, sources: dict) -> None:
    found = [t for t in targets if sources.get(t["company"])]
    missing = [t for t in targets if not sources.get(t["company"])]
    lines = [
        "# Official-site coverage",
        "",
        f"Auto-generated. **{len(found)} / {len(targets)}** companies have a job "
        "board the bot can read automatically. The rest use a system it can't "
        "read (or it couldn't find one) — those are still covered whenever they "
        "show up on the SimplifyJobs feed, but check their sites by hand now and "
        "then. To add one manually, put it in `data/ats_overrides.json`.",
        "",
        "## ✅ Monitored",
        "",
    ]
    for t in found:
        srcs = ", ".join(source_key(t["company"], s) for s in sources[t["company"]])
        lines.append(f"- {t['company']} — `{srcs}`")
    lines += ["", "## ⚠️ Not monitored (check manually)", ""]
    for t in missing:
        lines.append(f"- {t['company']} — {t.get('url') or 'no link'}")
    with open(COVERAGE_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Job board readers -> list of {id, title, url, location, date_posted}
# ---------------------------------------------------------------------------
def _iso_from_ms(ms):
    try:
        return datetime.datetime.utcfromtimestamp(int(ms) / 1000).isoformat() + "Z"
    except (TypeError, ValueError):
        return None


def read_greenhouse(src):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{src['token']}/jobs",
                     headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]), "title": j.get("title", ""),
        "url": j.get("absolute_url"),
        "location": (j.get("location") or {}).get("name", ""),
        "date_posted": j.get("first_published") or j.get("updated_at"),
    } for j in r.json().get("jobs", [])]


def read_lever(src):
    r = requests.get(f"https://api.lever.co/v0/postings/{src['token']}?mode=json",
                     headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        out.append({"id": j["id"], "title": j.get("text", ""), "url": j.get("hostedUrl"),
                    "location": loc, "date_posted": _iso_from_ms(j.get("createdAt"))})
    return out


def read_ashby(src):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{src['token']}",
                     headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location") or ""] + [
            (s.get("location") or "") for s in (j.get("secondaryLocations") or [])]
        out.append({"id": j.get("id"), "title": j.get("title", ""), "url": j.get("jobUrl"),
                    "location": "; ".join(l for l in locs if l),
                    "date_posted": j.get("publishedAt")})
    return out


def read_workday(src):
    host = f"https://{src['tenant']}.{src['wd']}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{src['tenant']}/{src['site']}/jobs"
    out = []
    for offset in range(0, 100, 20):
        r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                     "searchText": "intern"},
                          headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            out.append({"id": path, "title": j.get("title", ""),
                        "url": f"{host}/{src['site']}{path}",
                        "location": j.get("locationsText", ""), "date_posted": None})
        if len(posts) < 20:
            break
    return out


def read_smartrecruiters(src):
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{src['token']}/postings",
                     params={"q": "intern", "limit": 100}, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append({"id": j["id"], "title": j.get("name", ""),
                    "url": f"https://jobs.smartrecruiters.com/{src['token']}/{j['id']}",
                    "location": ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                                      loc.get("country")] if x),
                    "date_posted": j.get("releasedDate")})
    return out


READERS = {
    "greenhouse": read_greenhouse, "lever": read_lever, "ashby": read_ashby,
    "workday": read_workday, "smartrecruiters": read_smartrecruiters,
}


def _read_source(item):
    company, src = item
    try:
        return company, src, READERS[src["ats"]](src)
    except Exception as e:  # one broken board must never break the run
        return company, src, e


# ---------------------------------------------------------------------------
# Main entry used by fetch_internships.py
# ---------------------------------------------------------------------------
def load_state():
    return _read_json(STATE_PATH, {})


def save_state(state):
    _write_json(STATE_PATH, state)


def poll_company_sites(targets, lookup, now, known_urls, state) -> list:
    """Returns new matching job records (same shape as Simplify records)."""
    sources = load_sources(targets, now)
    items = [(c, s) for c, srcs in sources.items() for s in srcs]
    seen = state.setdefault("sites", {})
    now_iso = now.isoformat() + "Z"
    records, ok, failed = [], 0, 0

    with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as pool:
        for company, src, jobs in pool.map(_read_source, items):
            if isinstance(jobs, Exception):
                failed += 1
                continue
            ok += 1
            key = source_key(company, src)
            first_time = key not in seen
            already = set(seen.get(key, []))
            group = lookup.get(normalize_company(company)) or "non_qr"
            for j in jobs:
                jid = str(j.get("id") or j.get("url"))
                if jid in already:
                    continue
                already.add(jid)
                if first_time:
                    continue  # baseline: postings already up don't count
                title = j.get("title", "")
                if not is_internship(title) or not quant_match(title, "", group):
                    continue
                where = classify_location(j.get("location", ""))
                if where == "non_us":
                    continue
                url = j.get("url") or ""
                if url and url in known_urls:
                    continue
                records.append({
                    "id": f"site:{key}:{jid}",
                    "source": "site",
                    "company": company,
                    "title": title,
                    "terms": [],
                    "locations": [j.get("location")] if j.get("location") else [],
                    "countries": ["USA"] if where == "us" else [],
                    "degrees": [],
                    "url": url,
                    "date_posted": j.get("date_posted"),
                    "big_tech": False,
                    "status": "new",
                    "found_at": now_iso,
                    "applied_at": None,
                    "issue_number": None,
                    "issue_url": None,
                    "expired_issue_closed": False,
                    "backfilled": False,
                })
            seen[key] = sorted(already)

    print(f"Company sites: {len(items)} boards, {ok} read, {failed} failed, "
          f"{len(records)} new quant internship(s).")
    return records
