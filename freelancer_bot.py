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
import threading
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
ID_RETENTION    = 3 * 24 * 3600  # Keep seen IDs for 3 days

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
def fetch_projects(token):
    """Retrieve the 100 most recent active projects, no server-side skill filter."""
    headers = {"Freelancer-OAuth-V1": token}
    params  = [
        ("limit",            50),
        ("sort_field",       "time_submitted"),
        ("sort_order",       "desc"),
        ("full_description", "true"),
        ("job_details",      "true"),
        ("user_details",     "true"),
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
    "kenya", "ethiopia", "egypt", "myanmar", "cambodia",
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

BLOCKLIST_KEYWORDS = [
    # Sales / outreach
    "cold call", "cold caller", "appointment setter", "appointment setting",
    "telemarketing", "telesales", "outbound call", "phone call", "mass messaging",
    "whatsapp blast", "sms blast", "lead generation", "sales rep", "sales representative",
    "closer", "commission only", "commission-only", "results-based pay",
    # Data / admin
    "data entry", "copy paste", "copy-paste", "excel data", "web scraping",
    "scrape", "scraper", "virtual assistant", "va needed", "personal assistant",
    # Support
    "customer support", "customer service", "live chat", "chat support",
    # Finance / legal
    "bookkeeping", "accounting", "payroll", "tax",
    "stock investment", "investment guidance", "financial advisor",
    # Design / media
    "logo design", "graphic design", "logo",
    "video creation", "video edit", "video editing",
    "image edit", "background removal", "photo edit", "photoshop", "illustrator",
    "youtube", "tiktok", "instagram reel",
    # Writing / translation
    "content creation", "copywriting", "article writing", "blog writing",
    "translations", "translator", "transcription", "proofreading",
    # Security / misc
    "pen test", "penetration test", "security audit", "geopolitical",
]

_INDIA_PHRASES = [
    "inr", "₹", "prayagraj", "looking for indian", "indian developer",
    "india based", "india only", "from india", "based in india",
]


def blocklist_match(project):
    """Return the first matching blocklist keyword, or None."""
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ]).lower()
    for kw in BLOCKLIST_KEYWORDS:
        if kw in text:
            return kw
    return None


def is_india_project(project):
    """Return True if description text suggests an India-based client."""
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ]).lower()
    return any(phrase in text for phrase in _INDIA_PHRASES)


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

MIN_HOURLY_RATE = 15  # Reject hourly projects paying less than this

def budget_ok(project, settings):
    p_type   = project.get("type", "fixed")
    budget   = project.get("budget", {}) or {}
    min_b    = float(budget.get("minimum") or 0)
    max_b    = float(budget.get("maximum") or 0)

    if p_type == "hourly":
        # Reject if max hourly rate is below minimum (use max if set, else min)
        effective_hourly = max_b if max_b else min_b
        return effective_hourly >= MIN_HOURLY_RATE

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
BID_SYSTEM_TEMPLATE = (
    "You are writing a Freelancer.com bid for Anne Sharp, a senior web developer "
    "and digital marketer. Here is her full portfolio — pick the 1-2 most relevant items "
    "based on the job description and reference them naturally in the bid. Only include "
    "portfolio URLs that are genuinely relevant. Vary your selections — do not always "
    "pick the same project. Return only the bid text, no commentary.\n\n"
    "Your bids must be between 80 and 120 words maximum. Not a word more. Be punchy and "
    "concise. Every sentence must earn its place. Cut anything that can be implied.\n\n"
    "Do not use em dashes, en dashes, or hyphens anywhere in the bid text under any "
    "circumstances. Rewrite any sentence that would require a dash.\n\n"
    "Portfolio:\n{portfolio}"
)

BID_USER_TEMPLATE = """\
Write a bid for this project:
Title: {title}
Description: {description}
Budget: {budget}
Skills: {skills}

Follow this exact structure and rules:

STRUCTURE:
1. Opening Hook
Write one or two sentences that show you read the brief and have a genuine reaction to it. Do not open with I — start with the project, the problem, or an observation. Make it specific enough that it could only work for this post.

2. Proof You Read Carefully
Reference one or two specific details, goals, or constraints from the job post. Do not be vague. Name the actual thing they mentioned — the tech stack, the deadline pressure, the audience, the integration they need. Phrase it naturally, as though continuing a thought.

3. Relevant Experience — Mini Story
Two to three sentences describing something genuinely similar you have handled. Lead with what you built or solved, then mention the outcome or benefit. Name tools or approaches where relevant. Pick the most relevant portfolio project and reference it naturally with its URL.

4. Authority and Trust
One sentence that conveys reliability and professionalism. Write it fresh — sound like a real person, not a brochure. Rotate the angle each time: sometimes communication, sometimes process, sometimes ownership mentality.

5. Recent Previous Projects
Use this exact format — only include URLs genuinely relevant to this project (1-2 max).
Use a hyphen (-) before each portfolio link, not an asterisk (*):
Recent work:
- [url]

6. Close and CTA
End with one natural sentence inviting next steps based on what this specific client needs.

Sign-off: Regards, Anne S.

STYLE RULES:
* 80-120 words total, not including sign-off and links
* No bullet points or lists in the body copy
* No greetings, no flattery, no filler phrases like I would love to help or I am perfect for this
* No generic claims — every sentence specific to this project
* Short paragraphs, easy to skim
* Vary sentence rhythm naturally
* Sound like a person who read the post twice and is responding honestly"""


def draft_bid(project, skill_names, portfolio):
    """Call Claude API to draft a bid for the project. Returns the bid text or None."""
    if anthropic_sdk is None:
        log("Bid drafting skipped — 'anthropic' package not installed.", "warning")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("Bid drafting skipped — ANTHROPIC_API_KEY not set.", "warning")
        return None

    title       = project.get("title", "N/A")
    description = (project.get("description") or "").strip()[:3000]
    budget      = fmt_budget(project)
    skills_str  = ", ".join(skill_names) if skill_names else "N/A"
    portfolio_json = json.dumps(portfolio, indent=2) if portfolio else "No portfolio available."

    system_prompt = BID_SYSTEM_TEMPLATE.format(portfolio=portfolio_json)
    user_prompt   = BID_USER_TEMPLATE.format(
        title=title,
        description=description,
        budget=budget,
        skills=skills_str,
    )

    def clean(text):
        return (
            text
            .replace("—", "-")
            .replace("–", "-")
            .replace(" - ", " ")
            .replace("- ", "")
            .replace("* http", "- http")
        )

    def word_count(text):
        return len(text.split())

    try:
        client   = anthropic_sdk.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content": user_prompt}]
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            system=system_prompt,
            messages=messages,
        )
        bid_text = next((b.text for b in response.content if b.type == "text"), None)
        if not bid_text:
            return None
        bid_text = clean(bid_text)

        wc = word_count(bid_text)
        if wc > 120:
            log(f"Bid too long ({wc} words) — asking Claude to trim.")
            messages.append({"role": "assistant", "content": bid_text})
            messages.append({"role": "user", "content": (
                "This bid is too long. Trim it to under 120 words while keeping the hook, "
                "the relevant experience, the portfolio links, and the sign-off. "
                "Remove any sentence that isn't essential."
            )})
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=600,
                system=system_prompt,
                messages=messages,
            )
            bid_text = next((b.text for b in response.content if b.type == "text"), bid_text)
            bid_text = clean(bid_text)
            wc = word_count(bid_text)

        log(f"Bid written: {wc} words")
        return bid_text
    except Exception as e:
        log(f"Bid drafting failed: {e}", "warning")
        return None

def log_portfolio_chosen(bid_text, portfolio):
    """Scan the bid text for portfolio URLs and log which items Claude chose."""
    if not bid_text or not portfolio:
        return
    chosen = [item["name"] for item in portfolio if item.get("url", "") in bid_text]
    if chosen:
        log(f"Portfolio chosen: {', '.join(chosen)}")
    else:
        log("Portfolio chosen: none matched in bid text")


# ---------------------------------------------------------------------------
# Pre-bid eligibility check
# ---------------------------------------------------------------------------
def fetch_my_skill_ids(token):
    """Fetch Anne's registered skill IDs from the Freelancer API at startup."""
    try:
        resp = requests.get(
            "https://www.freelancer.com/api/users/0.1/self/",
            headers={"Freelancer-OAuth-V1": token},
            params={"skill_details": "true"},
            timeout=10,
        )
        if resp.status_code != 200:
            log(f"Could not fetch skill IDs ({resp.status_code}) — skill check disabled.", "warning")
            return set()
        skills = resp.json().get("result", {}).get("jobs", []) or []
        ids = {str(s.get("id")) for s in skills if s.get("id")}
        names = [s.get("name", "") for s in skills if s.get("name")]
        log(f"Registered skills ({len(ids)}): {', '.join(sorted(names))}")
        return ids
    except Exception as e:
        log(f"Could not fetch skill IDs: {e} — skill check disabled.", "warning")
        return set()


def check_project_eligibility(project_id, token, my_skill_ids):
    """GET full project details and check for bid blockers before calling Claude.

    Returns (eligible: bool, reason: str | None).
    Reasons prefixed with "SILENT:" are logged but do NOT trigger a Telegram message.
    """
    try:
        resp = requests.get(
            f"{FREELANCER_API}/projects/{project_id}/",
            headers={"Freelancer-OAuth-V1": token},
            params={"full_description": "true", "job_details": "true", "user_details": "true"},
            timeout=10,
        )
        if resp.status_code != 200:
            log(f"Pre-bid check failed ({resp.status_code}) for {project_id} — blocking to avoid wasted Claude call.", "warning")
            return False, "SILENT:Pre-bid API check failed"

        data     = resp.json().get("result", {}) or {}
        proj     = data if "upgrades" in data else (data.get("project") or data)
        upgrades = proj.get("upgrades", {}) or {}

        # Check 1: client country from project details (catches mismatches with bulk fetch)
        users_detail = data.get("users", {}) or {}
        owner_id_str = str(proj.get("owner_id", ""))
        owner_detail = users_detail.get(owner_id_str) or {}
        client_country = (
            (owner_detail.get("location") or {})
            .get("country", {}) or {}
        ).get("name", "") or ""
        if client_country and client_country.lower() in _BLOCKED_COUNTRIES:
            return False, f"SILENT:Blocked country from project details ({client_country})"

        # Check 2: non-English language field
        lang = (proj.get("language") or "").strip().lower()
        if lang and lang != "en":
            return False, f"SILENT:Non-English project (language={lang})"

        # Check 3: sealed / NDA / preferred-bidder restrictions
        if upgrades.get("sealed") or upgrades.get("NDA"):
            return False, "Preferred bidders only"

        # Check 4: required skills the bidder doesn't have
        if my_skill_ids:
            required_jobs = proj.get("jobs", []) or []
            required_ids  = {str(j.get("id")) for j in required_jobs if j.get("id")}
            missing = required_ids - my_skill_ids
            if missing:
                return False, f"Missing required skills (IDs: {', '.join(sorted(missing))})"

        return True, None
    except Exception as e:
        log(f"Pre-bid eligibility check error for {project_id}: {e} — blocking to avoid wasted Claude call.", "warning")
        return False, "SILENT:Pre-bid check exception"


# ---------------------------------------------------------------------------
# Bid submission via Freelancer API
# ---------------------------------------------------------------------------
def parse_bid_error(response_json):
    """Extract a human-readable reason from a failed Freelancer API response."""
    try:
        status     = response_json.get("status", "")
        message    = response_json.get("message", "")
        error_code = response_json.get("error_code", "")
        combined   = f"{status} {message} {error_code}".lower()

        if "too fast" in combined or "rate" in combined or "throttl" in combined or "slow down" in combined:
            return "TOO_FAST"
        elif "language" in combined or "different language" in combined or "wrong language" in combined:
            return "WRONG_LANGUAGE"
        elif "nda" in combined:
            return "NDA signature required — bid manually"
        elif "preferred" in combined:
            return "Preferred bidders only — bid manually if qualified"
        elif "sla" in combined:
            return "SLA agreement required — bid manually"
        elif "not enough bids" in combined or "no bids" in combined:
            return "Out of bids — top up Freelancer account"
        elif "already bid" in combined or "duplicate" in combined:
            return "ALREADY_BID"
        elif "closed" in combined or "expired" in combined:
            return "Project closed or expired"
        elif "not allowed" in combined or "enotallowed" in combined:
            return "Bid not allowed (check project restrictions)"
        elif message:
            return f"API error: {message}"
        elif status:
            return f"API status: {status}"
        else:
            return "Unknown error — check Railway logs"
    except Exception:
        return "Could not parse error response"


BIDDER_ID = 83207744

def calc_bid_amount(project):
    """Return (amount, label) at 70% of max budget, or (None, reason) if budget missing."""
    p_type = project.get("type", "fixed")
    budget = project.get("budget", {}) or {}
    max_b  = float(budget.get("maximum") or 0)

    if not max_b:
        return None, "missing or zero maximum budget"

    amount = round(max_b * 0.70)
    label  = f"${amount} (70% of ${max_b:.0f} max {'hourly rate' if p_type == 'hourly' else 'budget'})"
    return amount, label


def submit_bid(project, bid_text, amount, token):
    """Submit bid to Freelancer API. Returns (success, reason_string)."""
    proj_id = project.get("id")

    try:
        resp = requests.post(
            "https://www.freelancer.com/api/projects/0.1/bids/",
            headers={"Freelancer-OAuth-V1": token},
            json={
                "project_id":           proj_id,
                "bidder_id":            BIDDER_ID,
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
        reason = parse_bid_error(resp.json())
        log(f"Bid submission failed ({resp.status_code}): {reason}", "warning")
        return False, reason
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
# Telegram command listener (runs in background thread)
# ---------------------------------------------------------------------------
def telegram_command_listener(bot_token, chat_id, bot_state):
    """Poll for Telegram bot commands (/pause, /play, /status) in a background thread."""
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params=params,
                timeout=35,
            )
            if resp.status_code != 200:
                time.sleep(5)
                continue
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                msg_chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if msg_chat_id != chat_id:
                    continue
                if text == "/pause":
                    bot_state["paused"] = True
                    log("Bot paused via Telegram command.")
                    send_telegram("⏸ Bot paused. Send /play to resume.", bot_token, chat_id)
                elif text == "/play":
                    bot_state["paused"] = False
                    log("Bot resumed via Telegram command.")
                    send_telegram("✅ Bot resumed. Scanning every 30 seconds.", bot_token, chat_id)
                elif text == "/status":
                    if bot_state["paused"]:
                        send_telegram("⏸ Bot is paused. Send /play to resume.", bot_token, chat_id)
                    else:
                        send_telegram("✅ Bot is running. Scanning every 30 seconds.", bot_token, chat_id)
        except Exception as e:
            log(f"Command listener error: {e}", "warning")
            time.sleep(5)


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
def main(bot_state=None):
    if bot_state and bot_state.get("paused"):
        log("Bot is paused — skipping scan.")
        return

    log("=" * 55)
    log("RUNNING VERSION 2")
    log("Freelancer Monitor started")

    # --- Load everything fresh ---
    settings = load_settings()
    token    = settings["freelancer_token"]
    tg_token = settings["telegram_bot_token"]
    tg_chat  = str(settings["telegram_chat_id"])
    allowed  = build_country_set(settings)

    # --- Verify Freelancer token and fetch registered skills ---
    my_skill_ids = set()
    try:
        me = requests.get(
            "https://www.freelancer.com/api/users/0.1/self/",
            headers={"Freelancer-OAuth-V1": token},
            params={"skill_details": "true"},
            timeout=10,
        ).json()
        result = me.get("result", {}) or {}
        user_id = result.get("id")
        if user_id:
            log(f"Logged in as Freelancer user ID: {user_id}")
        else:
            log("ERROR: Could not fetch Freelancer user ID — bids will fail. Check FREELANCER_TOKEN.", "error")
        skills = result.get("jobs", []) or []
        my_skill_ids = {str(s.get("id")) for s in skills if s.get("id")}
        skill_names  = [s.get("name", "") for s in skills if s.get("name")]
        log(f"Registered skills ({len(my_skill_ids)}): {', '.join(sorted(skill_names))}")
    except Exception as e:
        log(f"ERROR: Could not fetch Freelancer user ID — bids will fail. Check FREELANCER_TOKEN. ({e})", "error")

    # Load portfolio once at startup
    portfolio = load_json(PORTFOLIO_FILE, [])
    if portfolio:
        log(f"Loaded {len(portfolio)} portfolio item(s)")
    else:
        log("No portfolio loaded — bids will be written without portfolio examples.", "warning")

    seen_ids = load_seen_ids()
    log(f"Loaded {len(seen_ids)} previously seen project IDs")

    # --- Fetch from Freelancer (no server-side skill filter) ---
    log("Fetching 100 most recent projects…")
    result = fetch_projects(token)

    if not result:
        log("No result from Freelancer API. Will try again next run.")
        save_last_run(0, 0)
        return

    projects  = result.get("projects", []) or []
    users     = result.get("users", {})    or {}
    jobs_dict = result.get("jobs", {})     or {}
    log(f"Received {len(projects)} project(s) from API")

    # --- Pass 1: filter all projects ---
    # new_seen starts as a copy of old seen_ids; new entries are added only at
    # decision points (filter rejection or completed bid attempt).
    new_seen    = dict(seen_ids)
    alerts_sent = 0
    now         = time.time()
    qualified   = []  # (project, country_name, skill_names)

    counts = {"seen": 0, "currency": 0, "country": 0, "india": 0, "language": 0, "budget": 0, "new_client": 0, "blocklist": 0, "skill": 0}
    six_months_ago = now - (180 * 24 * 3600)

    for project in projects:
        proj_id = str(project.get("id", ""))
        if not proj_id:
            continue

        title_short = f"\"{project.get('title', '')[:60]}\""

        # Skip already seen (timestamp already in new_seen via dict copy)
        if proj_id in seen_ids:
            counts["seen"] += 1
            log(f"FILTERED [seen] {title_short}")
            continue

        # Country filter (resolve early — needed for other log lines too)
        owner_id    = str(project.get("owner_id", ""))
        owner       = users.get(owner_id) or {}
        location    = (owner.get("location") or {})
        country_obj = (location.get("country") or {})
        country_name = country_obj.get("name", "") or ""

        if not country_allowed(country_name, allowed):
            counts["country"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [country] {title_short} country=\"{country_name}\"")
            continue

        # Currency filter — reject INR projects
        currency_code = (project.get("currency") or {}).get("code", "")
        if currency_code == "INR":
            counts["currency"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [currency] {title_short} budget={fmt_budget(project)}")
            continue

        # Language filter — reject non-English projects
        if not is_english(project):
            counts["language"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [language] {title_short} country=\"{country_name}\"")
            continue

        # Budget filter
        if not budget_ok(project, settings):
            counts["budget"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [budget] {title_short} budget={fmt_budget(project)}")
            continue

        # Client quality filter — reject brand-new accounts with zero history
        rep      = (owner.get("employer_reputation") or {})
        history  = (rep.get("entire_history") or {})
        complete = int(history.get("complete") or 0)
        reviews  = int(history.get("reviews") or 0)
        reg_date = float(owner.get("registration_date") or 0)
        if complete == 0 and reviews == 0 and reg_date > 0 and reg_date > six_months_ago:
            counts["new_client"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [new client - no history] {title_short} country=\"{country_name}\"")
            continue

        # India content filter — catches India-based clients with blank country field
        if is_india_project(project):
            counts["india"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [india] {title_short}")
            continue

        # Blocklist filter — rejects irrelevant job types before skill check
        blocked_kw = blocklist_match(project)
        if blocked_kw:
            counts["blocklist"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [blocklist] {title_short} keyword=\"{blocked_kw}\"")
            continue

        # Skill keyword filter — client-side check on title + description
        matched_kw = keyword_match(project)
        if not matched_kw:
            counts["skill"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [skill] {title_short}")
            continue

        # All filters passed — mark seen and persist to disk immediately so a crash
        # mid-bid never causes a duplicate attempt on the next scan.
        skill_names = get_skill_names(project, jobs_dict)
        log(
            f"PASSED [{proj_id}] \"{project.get('title', '')[:60]}\" "
            f"budget={fmt_budget(project)} country=\"{country_name}\" "
            f"keyword=\"{matched_kw}\""
        )
        new_seen[proj_id] = now
        cleanup_and_save(new_seen)
        log(f"Marked seen immediately: \"{project.get('title', '')[:60]}\"")
        qualified.append((project, country_name, skill_names))

    if counts["seen"] > 40:
        log("WARNING: Most projects already seen — waiting for new postings", "warning")

    log(
        f"Scan summary — checked {len(projects)} | "
        f"seen: {counts['seen']} | "
        f"country: {counts['country']} | "
        f"currency: {counts['currency']} | "
        f"india: {counts['india']} | "
        f"language: {counts['language']} | "
        f"budget: {counts['budget']} | "
        f"new_client: {counts['new_client']} | "
        f"blocklist: {counts['blocklist']} | "
        f"skill: {counts['skill']} | "
        f"sent to Claude: {len(qualified)}"
    )

    # --- Pass 2: draft and submit bids ---
    total = len(qualified)
    for bid_num, (project, country_name, skill_names) in enumerate(qualified, 1):
        proj_id = str(project.get("id", ""))
        title   = project.get("title", "N/A")
        budget  = fmt_budget(project)
        link    = project_link(project)

        # Delay between bids — first one fires immediately
        if bid_num == 1:
            log(f"Bid {bid_num} of {total} — submitting immediately")
        else:
            log(f"Bid {bid_num} of {total} — waiting 30 seconds first...")
            time.sleep(30)

        # Pre-bid eligibility check — MUST pass before Claude is called
        eligible, reason = check_project_eligibility(proj_id, token, my_skill_ids)
        if not eligible:
            silent = reason.startswith("SILENT:")
            display_reason = reason[7:] if silent else reason
            log(f"SKIPPED [{proj_id}] \"{title[:60]}\" — {display_reason}")
            new_seen[proj_id] = now
            if not silent:
                send_telegram(f"⛔ SKIPPED - {display_reason}:\n{title}\n{link}", tg_token, tg_chat)
            continue

        # Calculate bid amount (70% of max budget)
        amount, amount_label = calc_bid_amount(project)
        if amount is None:
            log(f"Skipping [{proj_id}] \"{title[:60]}\" — {amount_label}", "warning")
            continue
        log(f"Bid amount calculated: {amount_label}")

        # Confirmed: eligibility passed, amount known — safe to call Claude
        log(f"Eligibility check passed for \"{title[:60]}\" — calling Claude now")
        print("CALLING CLAUDE NOW")  # Explicit stdout marker — must never appear before eligibility check

        # Draft bid with Claude
        bid = draft_bid(project, skill_names, portfolio)
        if not bid:
            log(f"Skipping alert — bid drafting failed for [{proj_id}]")
            continue
        log_portfolio_chosen(bid, portfolio)

        # Submit bid — retry once on TOO_FAST
        success, error = submit_bid(project, bid, amount, token)
        if error == "ALREADY_BID":
            log(f"SKIPPED [{proj_id}] \"{title[:60]}\" — already bid (silent, no Telegram)")
            continue  # already marked seen before Claude was called
        if error == "WRONG_LANGUAGE":
            log(f"SKIPPED [{proj_id}] \"{title[:60]}\" — wrong language (API rejection)", "warning")
            send_telegram(f"⛔ SKIPPED - Wrong language: {title}", tg_token, tg_chat)
            continue
        if error == "TOO_FAST":
            log("Bidding too fast — waiting 45 seconds and retrying once...", "warning")
            time.sleep(45)
            success, error = submit_bid(project, bid, amount, token)
            if error == "TOO_FAST":
                log(f"SKIPPED [{proj_id}] \"{title[:60]}\" — still too fast after retry, will retry next scan.", "warning")
                # Do NOT mark seen — let it retry next scan
                continue

        # Mark seen now that the bid attempt is complete (success or permanent failure)
        new_seen[proj_id] = now

        SEP = "\u2500" * 25
        if success:
            tg_msg = (
                f"✅ BID PLACED\n\n"
                f"📋 Project: {title}\n"
                f"🔗 {link}\n"
                f"💰 Budget: {budget}\n"
                f"🌍 Country: {country_name}\n\n"
                f"{SEP}\n\n"
                f"{bid}\n\n"
                f"{SEP}"
            )
        else:
            tg_msg = (
                f"⚠️ BID FAILED\n\n"
                f"📋 Project: {title}\n"
                f"🔗 {link}\n"
                f"💰 Budget: {budget}\n"
                f"🌍 Country: {country_name}\n"
                f"❌ Reason: {error}\n\n"
                f"{SEP}\n\n"
                f"{bid}\n\n"
                f"{SEP}"
            )

        if send_telegram(tg_msg, tg_token, tg_chat):
            save_recent_alert(project, country_name, skill_names)
            alerts_sent += 1

    # --- Persist state ---
    cleaned = cleanup_and_save(new_seen)
    log(f"Saved {len(cleaned)} seen IDs (after 3-day cleanup)")
    log(f"Done — checked {len(projects)}, sent {alerts_sent} alert(s).")

    save_last_run(len(projects), alerts_sent)


if __name__ == "__main__":
    # Load settings once for startup message and command listener
    _startup_settings = load_settings()
    _tg_token = _startup_settings["telegram_bot_token"]
    _tg_chat  = str(_startup_settings["telegram_chat_id"])

    # Shared pause state
    bot_state = {"paused": False}

    # Send startup notification
    send_telegram(
        "🤖 Freelancer bot started. Send /status to check, /pause to pause.",
        _tg_token, _tg_chat,
    )

    # Start Telegram command listener in background
    _listener = threading.Thread(
        target=telegram_command_listener,
        args=(_tg_token, _tg_chat, bot_state),
        daemon=True,
    )
    _listener.start()
    log("Telegram command listener started (responds to /pause, /play, /status).")

    while True:
        main(bot_state)
        time.sleep(30)
