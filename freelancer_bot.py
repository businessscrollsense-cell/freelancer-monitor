#!/usr/bin/env python3
"""
Freelancer.com Job Monitor Bot
Checks for new matching projects and sends Telegram notifications.
Runs in a loop, checking every 5 minutes.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not installed. Run: pip3 install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths — always resolved relative to this script, works from any cron context
# ---------------------------------------------------------------------------
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE   = os.path.join(SCRIPT_DIR, "settings.json")
SEEN_IDS_FILE   = os.path.join(SCRIPT_DIR, "seen_ids.json")
RECENT_FILE     = os.path.join(SCRIPT_DIR, "recent_alerts.json")
SKILL_CACHE     = os.path.join(SCRIPT_DIR, "skill_ids_cache.json")
LAST_RUN_FILE   = os.path.join(SCRIPT_DIR, "last_run.json")
LOG_FILE        = os.path.join(SCRIPT_DIR, "bot.log")

FREELANCER_API  = "https://www.freelancer.com/api/projects/0.1"
SKILL_CACHE_TTL = 86400   # Refresh skill IDs once a day
ID_RETENTION    = 7 * 24 * 3600  # Keep seen IDs for 7 days

# ---------------------------------------------------------------------------
# Logging — writes to bot.log alongside the script
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def log(msg, level="info"):
    getattr(logging, level)(msg)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        log(f"Could not write {path}: {e}", "error")
        return False

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings():
    """Read settings fresh every run so web-form changes apply immediately."""
    settings = load_json(SETTINGS_FILE, {})
    if not settings:
        log("ERROR: settings.json is missing or empty.", "error")
        sys.exit(1)
    required = ["freelancer_token", "telegram_bot_token", "telegram_chat_id"]
    for key in required:
        if not settings.get(key):
            log(f"ERROR: '{key}' is missing from settings.json.", "error")
            sys.exit(1)
    return settings

# ---------------------------------------------------------------------------
# Seen-IDs management
# ---------------------------------------------------------------------------
def load_seen_ids():
    data = load_json(SEEN_IDS_FILE, {})
    # Migrate legacy list format
    if isinstance(data, list):
        data = {str(i): time.time() for i in data}
    return {str(k): float(v) for k, v in data.items()}

def cleanup_and_save(seen_ids):
    cutoff = time.time() - ID_RETENTION
    cleaned = {k: v for k, v in seen_ids.items() if v > cutoff}
    save_json(SEEN_IDS_FILE, cleaned)
    return cleaned

# ---------------------------------------------------------------------------
# Skill-ID resolution with daily caching
# ---------------------------------------------------------------------------
def _extract_jobs_list(resp_json):
    """
    Extract a flat list of job dicts from any shape the API returns.
    Logs the raw top-level keys so we can see the structure.
    """
    log(f"Jobs API response top-level keys: {list(resp_json.keys()) if isinstance(resp_json, dict) else type(resp_json).__name__}")
    if not isinstance(resp_json, dict):
        log(f"Unexpected response type: {type(resp_json).__name__}")
        return []

    result = resp_json.get("result")
    log(f"result type: {type(result).__name__}, value preview: {str(result)[:200]}")

    if result is None:
        return []
    if isinstance(result, list):
        # result is directly a list of job objects
        return [j for j in result if isinstance(j, dict)]
    if isinstance(result, dict):
        # result may have a "jobs" key (list or dict) or "result" may itself be a job dict
        jobs_val = result.get("jobs")
        if isinstance(jobs_val, list):
            return [j for j in jobs_val if isinstance(j, dict)]
        if isinstance(jobs_val, dict):
            # jobs keyed by id
            return [j for j in jobs_val.values() if isinstance(j, dict)]
        # Sometimes the result dict IS the jobs dict (id -> job_obj)
        # If values look like job objects, treat them that way
        sample = next(iter(result.values()), None) if result else None
        if isinstance(sample, dict) and "name" in sample:
            return [j for j in result.values() if isinstance(j, dict)]
    return []


def get_skill_ids(skills, token):
    """
    Map human-readable skill names to Freelancer job IDs.
    Results are cached for 24 hours in skill_ids_cache.json.
    Falls back to empty list (caller handles no-skill-filter case).
    """
    cache = load_json(SKILL_CACHE, {})
    if cache and time.time() - cache.get("timestamp", 0) < SKILL_CACHE_TTL:
        mapping = cache.get("skills", {})
        ids = [mapping[s] for s in skills if s in mapping]
        if ids:
            log(f"Using cached skill IDs ({len(ids)} matched)")
            return ids

    log("Fetching skill IDs from Freelancer API…")
    headers = {"Authorization": f"Bearer {token}"}
    mapping = {}

    # Log one raw probe call so we can see the exact API shape
    try:
        probe = requests.get(
            f"{FREELANCER_API}/jobs/",
            params={"limit": 3},
            headers=headers,
            timeout=10,
        )
        log(f"Jobs API probe status: {probe.status_code}")
        log(f"Jobs API probe raw (first 500 chars): {probe.text[:500]}")
    except Exception as e:
        log(f"Jobs API probe failed: {e}", "warning")

    for skill in skills:
        try:
            resp = requests.get(
                f"{FREELANCER_API}/jobs/",
                params={"query": skill, "limit": 25},
                headers=headers,
                timeout=10,
            )
            log(f"Skill '{skill}': status={resp.status_code}, raw={resp.text[:300]}")
            if resp.status_code == 200:
                jobs = _extract_jobs_list(resp.json())
                log(f"Skill '{skill}': extracted {len(jobs)} job entries")
                # Prefer exact case-insensitive match; fall back to first result
                matched = next(
                    (j for j in jobs if j.get("name", "").lower() == skill.lower()),
                    jobs[0] if jobs else None,
                )
                if matched:
                    mapping[skill] = matched["id"]
                    log(f"Mapped '{skill}' -> id={matched['id']} name='{matched.get('name')}'")
            time.sleep(0.25)  # polite rate limiting
        except Exception as e:
            log(f"Skill lookup failed for '{skill}': {e}", "warning")

    save_json(SKILL_CACHE, {"timestamp": time.time(), "skills": mapping})
    log(f"Resolved {len(mapping)}/{len(skills)} skill IDs")
    return list(mapping.values())

# ---------------------------------------------------------------------------
# Fetch projects from Freelancer API
# ---------------------------------------------------------------------------
def fetch_projects(skill_ids, lookback_minutes, token):
    """
    Retrieve active projects posted within the last `lookback_minutes` minutes,
    filtered by the given skill IDs.
    """
    from_time = int(time.time()) - (lookback_minutes * 60)
    headers = {"Authorization": f"Bearer {token}"}

    # Build query params — use a list of tuples so repeated keys work correctly
    params = [
        ("limit", 100),
        ("sort_field", "time_submitted"),
        ("sort_order", "desc"),
        ("full_description", "true"),
        ("job_details", "true"),
        ("user_details", "true"),
        ("from_time", from_time),
    ]
    for sid in skill_ids:
        params.append(("jobs[]", sid))

    try:
        resp = requests.get(
            f"{FREELANCER_API}/projects/active",
            params=params,
            headers=headers,
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {}) or {}
        log(f"API error {resp.status_code}: {resp.text[:300]}", "error")
    except requests.exceptions.Timeout:
        log("Freelancer API request timed out.", "error")
    except Exception as e:
        log(f"Freelancer API request failed: {e}", "error")

    return {}

# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------
def build_country_set(settings):
    """Return a lowercase set of allowed country names."""
    countries = settings.get("countries", [])
    country_set = {c.lower() for c in countries}
    # Handle common UAE aliases
    if "united arab emirates" in country_set or "uae" in country_set:
        country_set.add("united arab emirates")
        country_set.add("uae")
    return country_set

def country_allowed(country_name, allowed_set):
    return bool(country_name) and country_name.lower() in allowed_set

def budget_ok(project, settings):
    p_type   = project.get("type", "fixed")
    budget   = project.get("budget", {}) or {}
    min_b    = float(budget.get("minimum") or 0)
    max_b    = float(budget.get("maximum") or 0)

    if p_type == "hourly":
        # Include all hourly projects regardless of rate
        return True

    # For fixed projects use the higher budget bound if available
    effective = max(min_b, max_b) if max_b else min_b
    return effective >= float(settings.get("min_fixed_budget", 450))

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt_budget(project):
    p_type = project.get("type", "fixed")
    budget = project.get("budget", {}) or {}
    min_b  = float(budget.get("minimum") or 0)
    max_b  = float(budget.get("maximum") or 0)
    sign   = (project.get("currency") or {}).get("sign", "$")

    if p_type == "hourly":
        if max_b and max_b != min_b:
            return f"{sign}{min_b:.0f}–{sign}{max_b:.0f}/hr"
        return f"{sign}{min_b:.0f}/hr"
    else:
        if max_b and max_b != min_b:
            return f"{sign}{min_b:.0f}–{sign}{max_b:.0f}"
        return f"{sign}{min_b:.0f}"

def get_skill_names(project, jobs_dict):
    names = []
    for job in project.get("jobs", []) or []:
        jid  = str(job.get("id", ""))
        name = (jobs_dict.get(jid) or {}).get("name", "")
        if name:
            names.append(name)
    return names

def project_link(project):
    seo = (project.get("seo_url") or "").strip("/")
    if seo:
        return f"https://www.freelancer.com/projects/{seo}"
    return f"https://www.freelancer.com/projects/{project.get('id', '')}"

def fmt_posted(ts):
    if not ts:
        return "Unknown"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def build_telegram_message(project, country, skill_names):
    desc = (project.get("description") or "").strip()
    preview = desc[:300] + ("…" if len(desc) > 300 else "")
    skills_str = ", ".join(skill_names[:12]) if skill_names else "N/A"

    return (
        "🚀 NEW PROJECT MATCH\n\n"
        f"📋 Title: {project.get('title', 'N/A')}\n"
        f"💰 Budget: {fmt_budget(project)}\n"
        f"🌍 Country: {country}\n"
        f"🏷️ Skills: {skills_str}\n"
        f"📝 Description: {preview}\n"
        f"🔗 Link: {project_link(project)}\n"
        f"⏰ Posted: {fmt_posted(project.get('time_submitted'))}"
    )

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message, bot_token, chat_id):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": str(chat_id),
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log(f"Telegram error {resp.status_code}: {resp.text[:200]}", "error")
    except Exception as e:
        log(f"Telegram send failed: {e}", "error")
    return False

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def save_recent_alert(project, country, skill_names):
    alerts = load_json(RECENT_FILE, [])
    alerts.insert(0, {
        "id":         project.get("id"),
        "title":      project.get("title", ""),
        "budget":     fmt_budget(project),
        "country":    country,
        "skills":     skill_names[:12],
        "link":       project_link(project),
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "posted_at":  project.get("time_submitted"),
    })
    save_json(RECENT_FILE, alerts[:5])

def save_last_run(projects_checked, alerts_sent):
    save_json(LAST_RUN_FILE, {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "projects_checked": projects_checked,
        "alerts_sent":      alerts_sent,
    })

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log("=" * 55)
    log("RUNNING VERSION 2")
    log("Freelancer Monitor started")

    # --- Load everything fresh ---
    settings       = load_settings()
    token          = settings["freelancer_token"]
    tg_token       = settings["telegram_bot_token"]
    tg_chat        = str(settings["telegram_chat_id"])
    skills         = settings.get("skills", [])
    lookback       = int(settings.get("lookback_minutes", 10))
    allowed        = build_country_set(settings)

    seen_ids       = load_seen_ids()
    log(f"Loaded {len(seen_ids)} previously seen project IDs")

    # --- Resolve skill IDs ---
    skill_ids = get_skill_ids(skills, token)
    if not skill_ids:
        log("WARNING: Could not resolve any skill IDs — fetching ALL recent projects.", "warning")

    # --- Fetch from Freelancer ---
    log(f"Fetching projects posted in the last {lookback} minutes…")
    result = fetch_projects(skill_ids, lookback, token)

    if not result:
        log("No result from Freelancer API. Will try again next run.")
        save_last_run(0, 0)
        return

    projects  = result.get("projects", []) or []
    users     = result.get("users", {})    or {}
    jobs_dict = result.get("jobs", {})     or {}
    log(f"Received {len(projects)} project(s) from API")

    # --- Process each project ---
    new_seen    = dict(seen_ids)
    alerts_sent = 0
    now         = time.time()
    cutoff_ts   = now - (lookback * 60)  # client-side guard

    for project in projects:
        proj_id = str(project.get("id", ""))
        if not proj_id:
            continue

        # Always mark as seen regardless of filters (prevents re-processing)
        new_seen[proj_id] = now

        # Skip already seen
        if proj_id in seen_ids:
            continue

        # Extra client-side time guard (in case from_time isn't supported)
        ts = float(project.get("time_submitted") or 0)
        if ts and ts < cutoff_ts:
            continue

        # Country filter
        owner_id   = str(project.get("owner_id", ""))
        owner      = users.get(owner_id) or {}
        location   = (owner.get("location") or {})
        country_obj = (location.get("country") or {})
        country_name = country_obj.get("name", "") or ""

        if not country_allowed(country_name, allowed):
            continue

        # Budget filter
        if not budget_ok(project, settings):
            continue

        # Compose and send notification
        skill_names = get_skill_names(project, jobs_dict)
        message     = build_telegram_message(project, country_name, skill_names)

        if send_telegram(message, tg_token, tg_chat):
            log(f"Alert sent: [{proj_id}] {project.get('title', '')[:60]}")
            save_recent_alert(project, country_name, skill_names)
            alerts_sent += 1
            time.sleep(0.5)  # Avoid Telegram rate limits

    # --- Persist state ---
    cleaned = cleanup_and_save(new_seen)
    log(f"Saved {len(cleaned)} seen IDs (after 7-day cleanup)")
    log(f"Done — checked {len(projects)}, sent {alerts_sent} alert(s).")

    save_last_run(len(projects), alerts_sent)


if __name__ == "__main__":
    while True:
        main()
        time.sleep(300)
