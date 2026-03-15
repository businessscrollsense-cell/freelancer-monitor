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

try:
    import anthropic as anthropic_sdk
except ImportError:
    anthropic_sdk = None

# ---------------------------------------------------------------------------
# Paths — always resolved relative to this script, works from any cron context
# ---------------------------------------------------------------------------
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE   = os.path.join(SCRIPT_DIR, "settings.json")
CONFIG_FILE     = os.path.join(SCRIPT_DIR, "config.json")
PORTFOLIO_FILE  = os.path.join(SCRIPT_DIR, "portfolio.json")
SEEN_IDS_FILE   = os.path.join(SCRIPT_DIR, "seen_ids.json")
RECENT_FILE     = os.path.join(SCRIPT_DIR, "recent_alerts.json")
LAST_RUN_FILE   = os.path.join(SCRIPT_DIR, "last_run.json")
LOG_FILE        = os.path.join(SCRIPT_DIR, "bot.log")

FREELANCER_API  = "https://www.freelancer.com/api/projects/0.1"
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
    """Read settings fresh every run so changes apply immediately.

    Non-secret config (skills, countries, budgets) comes from config.json (committed).
    Credentials come from environment variables, falling back to settings.json (local only).
    """
    # Start with committed non-secret config
    settings = load_json(CONFIG_FILE, {})

    # Merge local settings.json on top (credentials + any local overrides)
    local = load_json(SETTINGS_FILE, {})
    settings.update(local)

    # Environment variables take final precedence for credentials
    for env_var, key in [
        ("FREELANCER_TOKEN",   "freelancer_token"),
        ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
        ("TELEGRAM_CHAT_ID",   "telegram_chat_id"),
    ]:
        val = os.environ.get(env_var)
        if val:
            settings[key] = val

    required = ["freelancer_token", "telegram_bot_token", "telegram_chat_id"]
    for key in required:
        if not settings.get(key):
            log(f"ERROR: '{key}' missing — set the env var or add it to settings.json.", "error")
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
# Fetch projects from Freelancer API
# ---------------------------------------------------------------------------
def fetch_projects(lookback_minutes, token):
    """Retrieve the 50 most recent active projects, no server-side skill filter."""
    from_time = int(time.time()) - (lookback_minutes * 60)
    headers   = {"Authorization": f"Bearer {token}"}
    params    = [
        ("limit",        50),
        ("sort_field",   "time_submitted"),
        ("sort_order",   "desc"),
        ("full_description", "true"),
        ("job_details",  "true"),
        ("user_details", "true"),
        ("from_time",    from_time),
    ]
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
_BLOCKED_COUNTRIES = {
    "nigeria", "india", "pakistan", "bangladesh", "indonesia",
    "philippines", "vietnam", "nepal", "sri lanka", "ghana",
    "kenya", "ethiopia",
}

_SKILL_KEYWORDS = {
    # Web development
    "wordpress", "php", "javascript", "js", "react", "react.js", "next.js",
    "nextjs", "vue.js", "angular", "node", "typescript", "html", "css",
    "bootstrap", "tailwind", "django", "laravel", "webflow", "bubble.io",
    # Design
    "figma", "graphic design", "web design", "website design", "ux design",
    "ui design",
    # SEO & content
    "seo", "copywriting", "content strategy", "content writing", "blog",
    # Marketing
    "digital marketing", "social media management", "social media marketing",
    # Web/app types
    "website", "web app", "web application", "saas", "crm",
    "ecommerce", "e-commerce", "shopify", "woocommerce", "stripe",
    # APIs & databases
    "rest api", "graphql api", "api integration", "api development",
    "graphql", "postgresql", "mysql", "database",
    # AI
    "artificial intelligence", "chatbot", "openai", "chatgpt", "prompt engineering",
    # Mobile
    "mobile app", "swift", "ios", "android",
}

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
    if not country_name:
        return True  # Unknown country — let it through
    name_lower = country_name.lower()
    if name_lower in _BLOCKED_COUNTRIES:
        return False  # Explicit blocklist takes priority
    return name_lower in allowed_set

def keyword_match(project):
    """Return the first matching keyword if title/description contains a skill keyword."""
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ]).lower()
    for kw in _SKILL_KEYWORDS:
        if kw in text:
            return kw
    return None

try:
    from langdetect import detect as _langdetect, LangDetectException
except ImportError:
    _langdetect = None
    LangDetectException = Exception

_FOREIGN_WORDS = {
    # Spanish
    "somos", "estamos", "necesitamos", "buscamos", "queremos", "tenemos",
    "para", "con", "los", "las", "una", "uno", "del", "que", "por",
    "como", "este", "esta", "pero", "muy", "más", "nos", "nuestro",
    "nuestros", "empresa", "proyecto", "desarrollo", "aplicación",
    # Portuguese
    "das", "dos", "para", "com", "uma", "que", "por", "como",
    "nossa", "nosso", "estamos", "precisamos", "buscamos", "temos",
    "desenvolvimento", "empresa", "projeto", "aplicativo",
    # French
    "nous", "notre", "pour", "avec", "une", "les", "des", "qui",
    "que", "sur", "pas", "mais", "vous", "est", "sont", "dans",
    "développement", "entreprise", "projet",
    # German
    "wir", "für", "und", "der", "die", "das", "mit", "eine", "einen",
    "suchen", "brauchen", "unser", "unsere", "entwicklung", "projekt",
    # Italian
    "per", "con", "una", "che", "del", "dei", "delle", "siamo",
    "cerchiamo", "abbiamo", "nostro", "nostra", "sviluppo", "progetto",
}

_INDONESIAN_WORDS = {
    "saya", "kami", "yang", "untuk", "dengan", "dalam", "dan", "ini",
    "dari", "tidak", "akan", "pada", "atau", "juga", "bisa", "anda",
    "nya", "itu", "sudah", "karena",
}

def is_english(project):
    """Return False if the text is detected as non-English.

    Uses two methods:
    1. langdetect library (if installed)
    2. Word-list checks for Indonesian (3+ hits) and other foreign languages (2+ hits)
    """
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ])
    if not text.strip():
        return True  # Nothing to check — let it through

    # Method 1: langdetect
    if _langdetect and len(text) > 20:
        try:
            lang = _langdetect(text)
            if lang != "en":
                return False
        except LangDetectException:
            pass  # Fall through to word-list checks

    # Method 2a: Indonesian word list (3+ hits)
    words = set(w.strip(".,!?\"'()[]{}:;").lower() for w in text.split())
    if len(words & _INDONESIAN_WORDS) >= 3:
        return False

    # Method 2b: Other foreign languages (2+ hits)
    if len(words & _FOREIGN_WORDS) >= 2:
        return False

    return True

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
# Bid drafting via Claude API
# ---------------------------------------------------------------------------
BID_PROMPT = """You are writing a freelance bid on behalf of Anne Sharp, a senior full-stack web developer.

Write a bid for the project below using EXACTLY this structure and style:

STRUCTURE:
1. Opening Hook — one or two sentences showing you read the brief. Do NOT start with "I". Start with the project, the problem, or an observation. Make it specific to this post.
Bad: "This is exactly the kind of project I thrive on."
Good: "A lot of scheduling tools feel clunky because the logic is bolted on after the fact — building it properly from the start is where I'd focus."

2. Proof You Read Carefully — reference one or two specific details, goals, or constraints from the job post. Name the actual thing they mentioned — the tech stack, the deadline pressure, the audience, the integration they need. Phrase it naturally, as though continuing a thought, not ticking a box.

3. Relevant Experience — Mini Story — two to three sentences describing something genuinely similar Anne has handled. Lead with what was built or solved, then mention the outcome or benefit. Name tools or approaches naturally, not as a keyword list.
Example style: "I built a multi-tenant booking system for a London-based clinic using Supabase and Next.js — the client reduced their admin overhead significantly because the logic was automated end-to-end rather than patched together."

4. Authority and Trust — one sentence that conveys reliability and professionalism. Write it fresh each time — something a real person would say, not a brochure line. Rotate the angle: sometimes communication, sometimes process, sometimes long-term thinking, sometimes ownership mentality. Never use the same phrasing twice.

5. Recent Previous Projects — choose 1 or 2 items from the portfolio below that are GENUINELY relevant to this project. Only include a link if it clearly relates. If only one fits, use one. Use this exact format:
Recent work:
* [url]

6. Close and CTA — one natural sentence inviting next steps, written fresh based on what this specific client needs. Sometimes offer a plan, a quick question, or a specific first step.

Sign off with:
Regards, Anne S.

STYLE RULES (non-negotiable):
* 100–150 words total, NOT counting the sign-off and portfolio links
* No bullet points or lists in the body copy
* No greetings, no flattery, no filler phrases like "I'd love to help" or "I'm perfect for this"
* No generic claims — every sentence should be something only Anne could say about this specific project
* Short paragraphs so the bid is easy to skim
* Vary sentence rhythm naturally — do not write every sentence to the same length
* Sound like a person who read the post twice and is responding honestly

PORTFOLIO (use only what's relevant):
{portfolio}

PROJECT DETAILS:
Title: {title}
Budget: {budget}
Skills: {skills}
Description:
{description}"""


def _format_portfolio(portfolio):
    """Format portfolio list into a readable string for the prompt."""
    lines = []
    for item in portfolio:
        lines.append(
            f"- {item.get('name', '')}: {item.get('url', '')} — {item.get('description', '')} "
            f"[keywords: {', '.join(item.get('keywords', []))}]"
        )
    return "\n".join(lines) if lines else "No portfolio items available."


def draft_bid(project, country_name, skill_names):
    """Call Claude API to draft a bid for the project. Returns the bid text or None."""
    if anthropic_sdk is None:
        log("Bid drafting skipped — 'anthropic' package not installed.", "warning")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("Bid drafting skipped — ANTHROPIC_API_KEY not set.", "warning")
        return None

    portfolio     = load_json(PORTFOLIO_FILE, [])
    title         = project.get("title", "N/A")
    description   = (project.get("description") or "").strip()[:3000]
    budget        = fmt_budget(project)
    skills_str    = ", ".join(skill_names) if skill_names else "N/A"
    portfolio_str = _format_portfolio(portfolio)

    prompt = BID_PROMPT.format(
        title=title,
        budget=budget,
        skills=skills_str,
        description=description,
        portfolio=portfolio_str,
    )

    try:
        client   = anthropic_sdk.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        bid_text = next(
            (b.text for b in response.content if b.type == "text"), None
        )
        return bid_text
    except Exception as e:
        log(f"Bid drafting failed: {e}", "warning")
        return None

# ---------------------------------------------------------------------------
# Bid submission via Freelancer API
# ---------------------------------------------------------------------------
def submit_bid(project, bid_text, token):
    """Submit bid to Freelancer API. Returns (success, error_message)."""
    proj_id = project.get("id")
    p_type  = project.get("type", "fixed")
    budget  = project.get("budget", {}) or {}
    amount  = float(budget.get("minimum") or 500) if p_type != "hourly" else 500

    try:
        resp = requests.post(
            "https://www.freelancer.com/api/projects/0.1/bids/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "project_id":           proj_id,
                "amount":               amount,
                "period":               7,
                "milestone_percentage": 100,
                "description":          bid_text,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log(f"Bid submitted for project {proj_id}")
            return True, None
        error = resp.json().get("message") or resp.text[:200]
        log(f"Bid submission failed ({resp.status_code}): {error}", "warning")
        return False, error
    except Exception as e:
        log(f"Bid submission error: {e}", "warning")
        return False, str(e)

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
    settings = load_settings()
    token    = settings["freelancer_token"]
    tg_token = settings["telegram_bot_token"]
    tg_chat  = str(settings["telegram_chat_id"])
    lookback = int(settings.get("lookback_minutes", 10))
    allowed  = build_country_set(settings)

    seen_ids = load_seen_ids()
    log(f"Loaded {len(seen_ids)} previously seen project IDs")

    # --- Fetch from Freelancer (no server-side skill filter) ---
    log(f"Fetching projects posted in the last {lookback} minutes…")
    result = fetch_projects(lookback, token)

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
            log(f"FILTERED [too_old] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)}")
            continue

        # Country filter
        owner_id   = str(project.get("owner_id", ""))
        owner      = users.get(owner_id) or {}
        location   = (owner.get("location") or {})
        country_obj = (location.get("country") or {})
        country_name = country_obj.get("name", "") or ""

        if not country_allowed(country_name, allowed):
            reason = "blocklist" if country_name.lower() in _BLOCKED_COUNTRIES else "not_allowed"
            log(f"FILTERED [country/{reason}] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)} country=\"{country_name}\"")
            continue

        # Currency filter — reject INR projects
        currency_code = (project.get("currency") or {}).get("code", "")
        if currency_code == "INR":
            log(f"FILTERED [currency] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)} country=\"{country_name}\"")
            continue

        # Language filter — reject non-English projects
        if not is_english(project):
            log(f"FILTERED [language] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)} country=\"{country_name}\"")
            continue

        # Budget filter
        if not budget_ok(project, settings):
            log(f"FILTERED [budget] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)} country=\"{country_name}\"")
            continue

        # Skill keyword filter — client-side check on title + description
        matched_kw = keyword_match(project)
        if not matched_kw:
            log(f"FILTERED [skill] [{proj_id}] \"{project.get('title', '')[:60]}\" budget={fmt_budget(project)} country=\"{country_name}\"")
            continue

        # All filters passed
        skill_names = get_skill_names(project, jobs_dict)
        log(
            f"PASSED [{proj_id}] \"{project.get('title', '')[:60]}\" "
            f"budget={fmt_budget(project)} country=\"{country_name}\" "
            f"keyword=\"{matched_kw}\""
        )

        title  = project.get("title", "N/A")
        budget = fmt_budget(project)

        # Draft bid with Claude
        bid = draft_bid(project, country_name, skill_names)
        if not bid:
            log(f"Skipping alert — bid drafting failed for [{proj_id}]")
            continue

        # Submit bid to Freelancer
        success, error = submit_bid(project, bid, token)

        if success:
            tg_msg = (
                f"✅ BID PLACED\n"
                f"📋 {title}\n"
                f"💰 {budget}\n\n"
                f"✍️ BID SENT:\n{bid}"
            )
        else:
            tg_msg = (
                f"⚠️ BID FAILED — {error}\n"
                f"📋 {title}\n"
                f"💰 {budget}\n\n"
                f"✍️ DRAFT:\n{bid}"
            )

        if send_telegram(tg_msg, tg_token, tg_chat):
            save_recent_alert(project, country_name, skill_names)
            alerts_sent += 1

        time.sleep(2)  # Avoid rate limiting between bids

    # --- Persist state ---
    cleaned = cleanup_and_save(new_seen)
    log(f"Saved {len(cleaned)} seen IDs (after 7-day cleanup)")
    log(f"Done — checked {len(projects)}, sent {alerts_sent} alert(s).")

    save_last_run(len(projects), alerts_sent)


if __name__ == "__main__":
    while True:
        main()
        time.sleep(30)
