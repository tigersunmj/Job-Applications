"""
Fetches the current internship listings from the SimplifyJobs community board,
filters them to postings explicitly open to Bachelor's/Master's students,
based in the US (or Canada, if ONLY_USA is False), that look like QUANT roles,
and posted within the last RECENCY_DAYS days. Then checks the official career
sites of the companies in data/target_companies.csv (see company_sites.py).
Updates the local tracker (data/tracked_jobs.json + APPLICATIONS.md) with any
new matches.

Postings that were tracked as "new" but have disappeared from the active feed
(filled or pulled) are marked "expired" so stale issues can be auto-closed.

Writes new_matches.json (untracked) listing just this run's new matches, for
scripts/create_issues.py to turn into GitHub issues. Also sets the GitHub
Actions output `new_count`.
"""
import datetime
import json
import os
import sys

import requests

from lib import load_tracked, save_tracked, render_markdown, is_big_tech
import company_sites as cs

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "Summer2026-Internships/dev/.github/scripts/listings.json"
)
NEW_MATCHES_PATH = os.path.join(os.path.dirname(__file__), "..", "new_matches.json")
TARGET_DEGREES = {"Master's", "Bachelor's"}
RECENCY_DAYS = 7

# Only keep US postings. Set to False to also include Canada.
ONLY_USA = True

# What counts as "quant" is defined in scripts/company_sites.py
# (STRICT_KEYWORDS for any company, BROAD_KEYWORDS for companies on your
# list in data/target_companies.csv).
TARGET_LOOKUP = cs.target_lookup(cs.load_targets())

US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
}
CA_PROVINCE_ABBR = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
CA_PROVINCE_NAMES = {
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Northwest Territories", "Nova Scotia",
    "Nunavut", "Ontario", "Prince Edward Island", "Quebec", "Saskatchewan",
    "Yukon",
}


def fetch_listings() -> list:
    """Downloads the Simplify feed, retrying a few times so a brief network
    hiccup on GitHub's side doesn't fail the whole run."""
    import time
    last_error = None
    for attempt in range(4):
        try:
            resp = requests.get(LISTINGS_URL, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_error = e
            print(f"Simplify feed attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise last_error


def _location_countries(location: str) -> set:
    """Returns the subset of {"USA", "Canada"} a single location string
    indicates, empty if neither."""
    lower = location.lower()
    countries = set()
    if "canada" in lower:
        countries.add("Canada")
    if "united states" in lower or lower in ("usa", "us"):
        countries.add("USA")
    if "remote in canada" in lower:
        countries.add("Canada")
    if any(f"remote in {kw}" in lower for kw in ("us", "usa", "the us")):
        countries.add("USA")
    if location in CA_PROVINCE_NAMES:
        countries.add("Canada")
    if location in US_STATE_NAMES:
        countries.add("USA")
    if not countries:
        last_part = location.split(",")[-1].strip().upper()
        if last_part in CA_PROVINCE_ABBR:
            countries.add("Canada")
        elif last_part in US_STATE_ABBR:
            countries.add("USA")
    return countries


def get_countries(job: dict) -> list:
    countries = set()
    for loc in job.get("locations") or []:
        countries |= _location_countries(loc)
    return sorted(countries)


def is_us_or_canada(job: dict) -> bool:
    countries = get_countries(job)
    if ONLY_USA:
        return "USA" in countries
    return bool(countries)


def is_quant(job: dict) -> bool:
    group = cs.company_group(job.get("company_name", ""), TARGET_LOOKUP)
    return cs.quant_match(job.get("title", ""), job.get("category", ""), group)


def is_eligible(job: dict) -> bool:
    """Active, open to our target degrees, in the right country, and quant.
    Used for expiry detection too, so a posting isn't wrongly marked "expired"
    just for aging past the recency window below — only for actually
    disappearing upstream."""
    return (
        bool(job.get("active"))
        and bool(TARGET_DEGREES & set(job.get("degrees") or []))
        and is_us_or_canada(job)
        and is_quant(job)
    )


def is_recent(job: dict, now: datetime.datetime) -> bool:
    """Only postings from the last RECENCY_DAYS days are worth a fresh
    application / issue — older still-active postings are left tracked-but-
    ignored rather than queued."""
    date_posted = job.get("date_posted")
    if not date_posted:
        return False
    posted_at = datetime.datetime.utcfromtimestamp(date_posted)
    return (now - posted_at) <= datetime.timedelta(days=RECENCY_DAYS)


def to_record(job: dict, now: str) -> dict:
    date_posted = job.get("date_posted")
    date_posted_iso = (
        datetime.datetime.utcfromtimestamp(date_posted).isoformat() + "Z"
        if date_posted
        else None
    )
    company = job.get("company_name", "Unknown")
    return {
        "id": job["id"],
        "company": company,
        "title": job.get("title", "Unknown role"),
        "terms": job.get("terms", []),
        "locations": job.get("locations", []),
        "countries": get_countries(job),
        "degrees": job.get("degrees", []),
        "url": job.get("url"),
        "date_posted": date_posted_iso,
        "big_tech": is_big_tech(company),
        "source": "simplify",
        "status": "new",
        "found_at": now,
        "applied_at": None,
        "issue_number": None,
        "issue_url": None,
        "expired_issue_closed": False,
        "backfilled": False,
    }


def main() -> None:
    now_dt = datetime.datetime.utcnow()
    now = now_dt.isoformat() + "Z"
    listings = fetch_listings()
    eligible = [j for j in listings if is_eligible(j)]
    current_ids = {j["id"] for j in eligible}
    recent_eligible = [j for j in eligible if is_recent(j, now_dt)]

    tracked = load_tracked()
    state = cs.load_state()
    new_matches = []

    # The first time the wider "your company list" rules run, whatever they
    # match on Simplify right now is treated as already seen (no alerts).
    first_run = not state.get("simplify_initialized")
    seen_simplify = set(state.get("simplify_seen", []))

    for job in recent_eligible:
        if job["id"] in tracked or job["id"] in seen_simplify:
            continue
        if first_run:
            seen_simplify.add(job["id"])
            continue
        record = to_record(job, now)
        tracked[job["id"]] = record
        new_matches.append(record)
    state["simplify_initialized"] = True
    state["simplify_seen"] = sorted(seen_simplify & current_ids)

    for job_id, record in tracked.items():
        if (record.get("source", "simplify") == "simplify"
                and record["status"] == "new" and job_id not in current_ids):
            record["status"] = "expired"
            record["expired_at"] = now

    # Official career sites of the companies on your list.
    known_urls = {r.get("url") for r in tracked.values() if r.get("url")}
    try:
        site_records = cs.poll_company_sites(
            cs.load_targets(), TARGET_LOOKUP, now_dt, known_urls, state)
    except Exception as e:  # never let site polling break the Simplify part
        print(f"Company-site polling failed: {e}", file=sys.stderr)
        site_records = []
    for record in site_records:
        if record["id"] not in tracked:
            tracked[record["id"]] = record
            new_matches.append(record)

    cs.save_state(state)
    save_tracked(tracked)
    render_markdown(tracked)

    with open(NEW_MATCHES_PATH, "w") as f:
        json.dump(new_matches, f, indent=2)

    print(f"Fetched {len(listings)} listings, {len(eligible)} quant "
          f"Bachelor's/Master's-eligible active, {len(recent_eligible)} within "
          f"last {RECENCY_DAYS}d, {len(new_matches)} new "
          f"({len(site_records)} from company sites).", file=sys.stderr)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"new_count={len(new_matches)}\n")


if __name__ == "__main__":
    main()
