"""Collect popular Claude skill repositories via the official GitHub API.

Writes data/YYYY-MM-DD.json (KST date) and data/latest.json.
- top_total: top 15 by total stars
- top_growth: top 15 by stars gained in the last 7 days (from stargazer timestamps)
"""
import json
import os
import sys
import time
import base64
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(dt.timezone.utc)
WINDOW_DAYS = 7
CUTOFF = NOW - dt.timedelta(days=WINDOW_DAYS)
ERRORS = []

QUERIES = [
    "topic:claude-skills",
    "topic:claude-skill",
    "topic:claude-code-skills",
    "topic:agent-skills",
    "topic:anthropic-skills",
    "claude skills in:name,description",
    "claude skill in:name,description",
    "SKILL.md claude in:readme",
]

KEYWORDS = ("skill",)
CONTEXT = ("claude", "anthropic", "skill.md", "agent skill")


def req(path, accept="application/vnd.github+json", retries=3):
    url = path if path.startswith("http") else API + path
    r = urllib.request.Request(url, headers={
        "Accept": accept,
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skill-radar",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < retries - 1:
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time()) + 2) if reset else 30
                wait = min(wait, 60)
                print(f"rate limited on {url}, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            ERRORS.append(f"HTTP {e.code} {url}")
            return None
        except Exception as e:  # network errors: log and move on
            ERRORS.append(f"{type(e).__name__} {url}: {e}")
            if attempt < retries - 1:
                time.sleep(5)
                continue
            return None
    return None


def is_skill_repo(item):
    text = " ".join([
        item.get("full_name") or "",
        item.get("description") or "",
        " ".join(item.get("topics") or []),
    ]).lower()
    return any(k in text for k in KEYWORDS) and any(c in text for c in CONTEXT)


def search_candidates():
    found = {}
    for q in QUERIES:
        params = urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": 50})
        data = req(f"/search/repositories?{params}")
        time.sleep(2.5)  # search API: 30 req/min
        if not data:
            continue
        for it in data.get("items", []):
            if it.get("fork") or not is_skill_repo(it):
                continue
            fn = it["full_name"]
            entry = found.setdefault(fn, {
                "full_name": fn,
                "url": it["html_url"],
                "stars": it["stargazers_count"],
                "description": it.get("description") or "",
                "topics": it.get("topics") or [],
                "created_at": it.get("created_at"),
                "pushed_at": it.get("pushed_at"),
                "archived": it.get("archived", False),
                "matched_queries": [],
            })
            entry["matched_queries"].append(q)
    return list(found.values())


def stars_in_window(full_name, total):
    """Count stargazers whose starred_at is within the window by paging backward from the last page."""
    if total == 0:
        return 0
    last_page = (total + 99) // 100
    if last_page > 400:  # GitHub caps stargazer pagination
        return None
    count = 0
    page = last_page
    while page >= 1:
        data = req(f"/repos/{full_name}/stargazers?per_page=100&page={page}",
                   accept="application/vnd.github.star+json")
        if data is None:
            return None
        if not data:
            page -= 1
            continue
        stop = False
        for s in reversed(data):
            ts = s.get("starred_at")
            if not ts:
                continue
            if dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) >= CUTOFF:
                count += 1
            else:
                stop = True
                break
        if stop:
            break
        page -= 1
        if last_page - page >= 10:  # >1,000 stars/week: stop and report as lower bound
            return {"lower_bound": count}
    return count


def readme_excerpt(full_name, limit=1200):
    data = req(f"/repos/{full_name}/readme")
    if not data or "content" not in data:
        return ""
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
    except Exception:
        return ""
    return " ".join(text.split())[:limit]


def load_baseline(today):
    """Most recent earlier snapshot at least 6 days old, else the oldest earlier one."""
    import glob
    files = sorted(f for f in glob.glob("data/20*.json") if os.path.basename(f)[:10] < today)
    if not files:
        return None, None
    target = (dt.date.fromisoformat(today) - dt.timedelta(days=6)).isoformat()
    older = [f for f in files if os.path.basename(f)[:10] <= target]
    path = older[-1] if older else files[0]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    snap = {c["full_name"]: c["stars"] for c in data.get("all_candidates", [])}
    return os.path.basename(path)[:10], snap


def main():
    today = NOW.astimezone(KST).strftime("%Y-%m-%d")
    cands = search_candidates()
    cands.sort(key=lambda c: c["stars"], reverse=True)
    print(f"candidates={len(cands)}", flush=True)

    base_date, base = load_baseline(today)
    window_days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(base_date)).days if base_date else None
    for c in cands:
        if base and c["full_name"] in base:
            c["stars_gained"] = c["stars"] - base[c["full_name"]]
        else:
            c["stars_gained"] = None  # new candidate or no baseline yet
        created = dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        age = max((NOW - created).days, 1)
        c["age_days"] = age
        c["stars_per_day_since_creation"] = round(c["stars"] / age, 1)

    top_total = cands[:15]
    measured = [c for c in cands if isinstance(c.get("stars_gained"), int)]
    top_growth = sorted(measured, key=lambda c: c["stars_gained"], reverse=True)[:15]
    # Interim indicator when no baseline exists: young repos ranked by stars/day since creation
    rising_new = sorted([c for c in cands if c["age_days"] <= 30],
                        key=lambda c: c["stars_per_day_since_creation"], reverse=True)[:15]

    for c in {c["full_name"]: c for c in top_total + top_growth + rising_new}.values():
        c["readme_excerpt"] = readme_excerpt(c["full_name"])

    out = {
        "generated_at_utc": NOW.isoformat(),
        "date_kst": today,
        "source": "GitHub REST API (search/repositories, readme)",
        "growth_method": "stars now minus stars in baseline snapshot",
        "growth_baseline_date": base_date,
        "growth_window_days": window_days,
        "queries": QUERIES,
        "candidate_count": len(cands),
        "top_total": top_total,
        "top_growth": top_growth,
        "rising_new_repos_30d": rising_new,
        "errors": ERRORS[:50],
        "all_candidates": [{k: c.get(k) for k in ("full_name", "url", "stars", "stars_gained")} for c in cands],
    }
    os.makedirs("data", exist_ok=True)
    for path in (f"data/{today}.json", "data/latest.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"top_total={len(top_total)} top_growth={len(top_growth)} rising_new={len(rising_new)} baseline={base_date}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        os.makedirs("data", exist_ok=True)
        with open("data/last_error.txt", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n" + "\n".join(ERRORS[:50]))
        raise
