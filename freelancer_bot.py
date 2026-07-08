#!/usr/bin/env python3
"""
Freelancer.com Job Monitor Bot
Checks for new matching projects and sends Telegram notifications.
Runs in a loop, checking every 5 minutes.
"""

import json
import logging
import os
import queue
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
_seen_lock = threading.Lock()  # Guards seen_ids file across poll + websocket threads

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
    "uzbekistan", "kazakhstan", "moldova", "albania", "kosovo",
    "bolivia", "paraguay", "honduras", "guatemala", "el salvador",
    "morocco", "algeria", "tunisia", "libya", "sudan",
    "cameroon", "tanzania", "uganda", "zimbabwe", "zambia",
    "senegal", "ivory coast", "mali", "burkina faso",
}

_SKILL_KEYWORDS = [
    "wordpress", "php", "mobile app", "android", "ios", "swift",
    "web app", "web application", "website design", "website build",
    "website development", "figma", "html", "css", "javascript",
    "react", "next.js", "node.js", "typescript", "bootstrap", "tailwind",
    "mysql", "postgresql", "rest api", "graphql", "api integration",
    "saas platform", "saas build", "saas develop", "crm build", "crm develop",
    "ecommerce", "shopify", "woocommerce", "stripe integration",
    "flutter", "dart", "react native", "lms", "learning management",
    "web scraping", "automation build", "zapier", "make.com",
    "podio", "airtable", "dashboard build", "ai chatbot", "chatgpt integration",
    "openai", "prompt engineering", "app development", "app build",
    "bug fix", "debug", "fix my", "complete my", "finish my",
    "redesign", "figma to", "convert figma", "pwa",
    "developer", "web developer", "app developer", "software developer",
    "build a website", "build a web", "build an app", "build a platform",
    "need a developer", "looking for a developer", "hire a developer",
    "wordpress developer", "php developer", "react developer",
    "website", "web platform", "online platform", "digital platform",
    "e-commerce", "online store", "booking system", "management system",
    "custom website", "custom app", "custom platform"
]

BLOCKLIST_KEYWORDS = [
    # Sales and business roles
    "sales role", "sales executive", "sales manager", "sales rep",
    "commission based", "commission only", "results based",
    "cold call", "appointment setter", "telemarketing",
    "market expansion", "business development", "partnership",
    "student recruiter", "recruiter", "outbound sales",
    "contact specialist", "business contact", "calling businesses",
    "phone outreach", "outbound calling", "call businesses",
    "customer service representative", "customer service agent",
    "customer support agent", "live chat agent", "chat support agent",
    "sales closer", "deal closer", "closer needed", "closer wanted",
    "client support liaison", "support liaison", "client support representative",
    "guest communication", "guest support", "guest services",
    "english speaking client support", "english speaking support",
    "virtual assistant", "personal assistant needed",

    # Creative/design non-web
    "logo design", "graphic design", "brochure", "flyer",
    "3d design", "3d model", "rendering", "architectural",
    "interior design", "furniture design", "paint", "dresser",
    "video creation", "video edit", "explainer video", "animation",
    "motion graphic", "youtube", "tiktok", "instagram reel",

    # Writing and content
    "copywriting", "content writer", "blog writing", "article writing",
    "academic writing", "paper editing", "proofreading", "translation",
    "transcription", "ghostwriter",

    # Technical non-web
    "unity", "unreal engine", "game development", "game design",
    "3d modelling", "blender", "autocad",
    "network engineer", "sysadmin", "devops", "kubernetes",
    "penetration test", "pen test", "ethical hacking",
    "data analyst", "data science", "machine learning model",
    "nutanix", "vmware", "cisco", "IVR", "call routing",

    # Data entry
    "data entry", "copy paste", "data processing", "text entry",
    "microsoft access", "ms access",

    # Digital / affiliate marketing
    "facebook ads", "google ads", "paid ads", "ppc", "sem",
    "social media management", "social media marketing",
    "email marketing campaign", "seo audit", "seo strategy",
    "digital marketing strategy", "media buyer",
    "affiliate marketing", "affiliate program", "affiliate commission",
    "influencer marketing", "influencer outreach", "brand ambassador",
    "bulk marketing", "mass marketing", "performance marketing",
    "promote my app", "promote my product", "app promotion",
    "product launch", "go-to-market", "launch campaign",
    "acquire users", "user acquisition", "growth hacking",

    # Team / agency / recruiter projects
    "team of developers", "team of devs", "team of freelancers",
    "build and market", "build market and scale", "build, market",
    "looking for a team", "need a team", "hire a team",
    "agency preferred", "we are a startup looking",
    "equity", "revenue share", "revenue sharing",
    "white label", "white-label", "subcontractor", "sub-contractor",
    "no direct client contact", "no direct client communication",
    "reseller", "outsourcing partner", "ongoing pipeline of projects",

    # Local/physical jobs
    "within 150km", "local job", "on-site", "onsite required",
    "photography", "photographer", "photo shoot",

    # Analysis / planning / design non-web
    "rate limits analysis",
    "site planning", "dwelling", "autocad", "residential design",
    "poster", "slide design",

    # Engineering non-web
    "structural analysis", "structural engineer", "structural design",
    "civil engineer", "mechanical engineer",

    # Research / academic
    "medical research", "clinical research", "literature review",
    "research design", "systematic review", "peer review",

    # E-commerce ops (non-build)
    "ebay listing", "ebay seller", "product listing", "order fulfillment",
    "order fulfilment", "dropshipping", "amazon seller", "amazon fba",

    # Finance / trading
    "trading bot", "trading algorithm", "algo trading", "algorithmic trading",
    "forex", "crypto trading", "stock trading", "financial trading",

    # ML / computer vision
    "pose estimation", "computer vision", "nlp model", "machine learning pipeline",
    "deep learning", "neural network", "llm fine", "model training",

    # Legacy languages / forensics (outside web dev stack)
    "cobol", "mainframe", "phone forensic", "computer forensic",
    "forensic recovery", "data recovery",
]

_BLOCKED_COUNTRY_PHRASES = [
    # India — currency symbols and self-identification
    "inr", "₹", "looking for indian", "indian developer",
    "india based", "india only", "from india", "based in india",
    # India — major cities (unambiguous; won't appear in AU/UK/US project descriptions)
    "mumbai", "pune", "bengaluru", "bangalore", "hyderabad",
    "chennai", "new delhi", "ahmedabad", "kolkata",
    # Sri Lanka — currency code is unambiguous
    "sri lanka", "sri lankan", "lkr",
    # Nigeria
    "nigeria", "nigerian",
    # Morocco
    "morocco", "moroccan",
    # Pakistan
    "pakistan", "pakistani",
    # Bangladesh
    "bangladesh", "bangladeshi",
    # Philippines
    "philippines", "philippine",
]

# Structured-data city check (client's profile city field, not description text).
# More reliable than text matching — works even when currency is USD and the
# project description never mentions location at all.
_BLOCKED_OWNER_CITIES = {
    "mumbai", "pune", "bengaluru", "bangalore", "hyderabad", "chennai",
    "new delhi", "delhi", "ahmedabad", "kolkata", "jaipur", "surat",
    "lucknow", "kanpur", "nagpur", "indore", "noida", "gurgaon",
    "gurugram", "chandigarh", "coimbatore", "thane", "navi mumbai",
    "vadodara", "bhopal", "patna", "ludhiana", "agra", "nashik",
    "faridabad", "meerut", "rajkot", "varanasi", "kochi", "vijayawada",
    "karachi", "lahore", "islamabad", "rawalpindi", "faisalabad",
    "dhaka", "chittagong",
    "colombo", "kandy",
    "lagos", "abuja", "port harcourt", "ibadan", "kano",
    "casablanca", "rabat", "marrakech", "fes",
    "manila", "cebu", "davao", "quezon city",
}

def owner_city_blocked(owner):
    """Return True if the client's profile city field is in a blocked-country city.
    Catches India-based clients who list budgets in USD with no country/currency tell."""
    city = ((owner.get("location") or {}).get("city") or "").strip().lower()
    return city in _BLOCKED_OWNER_CITIES


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
    """Return True if description text suggests a blocked-country client.
    Catches projects where the country field is blank in the API response."""
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ]).lower()
    return any(phrase in text for phrase in _BLOCKED_COUNTRY_PHRASES)


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
        # Freelancer's API no longer returns owner/user details for this token
        # (owner_id and users are null on every project as of 2026-07) — country
        # is unknown for 100% of projects, not just the untrustworthy ones. Denying
        # by default here blocks every project. Text-based signals (is_india_project,
        # currency, language) are the real filters until owner data comes back.
        return True
    name_lower = country_name.lower()
    if name_lower in _BLOCKED_COUNTRIES:
        return False  # Explicit blocklist takes priority
    return name_lower in allowed_set

_BLOCKED_CATEGORIES = {
    # Electronics / embedded / hardware
    "can-bus", "pcb-design", "pcb", "embedded-systems", "fpga",
    "circuit-design", "electronics", "verilog", "vhdl", "microcontroller",
    "electrical-engineering", "power-electronics",
    # Lead gen / sales
    "lead-generation", "leads", "sales", "telemarketing",
    # Academic / writing
    "academic-research", "article-writing", "copywriting", "ghostwriting",
    "script-writing", "creative-writing", "proofreading", "translation",
    "technical-writing", "business-writing",
    # Admin / VA
    "administrative-support", "virtual-assistant", "customer-service",
    # Engineering (non-web)
    "estimation", "structural-engineering", "mechanical-engineering",
    "civil-engineering", "electrical-engineering", "autocad", "cad",
    # GIS / mapping
    "arcgis",
    # Data entry / research
    "data-entry", "data-analysis", "data-science", "market-research",
    # Procurement
    "sourcing",
    # Reverse engineering
    "reverse-engineering",
    # Design (non-web)
    "logo-design", "graphic-design", "illustration", "caricature-illustration",
    "3d-modelling", "3d-cad", "3ds-max", "maya", "blender", "cinema-4d",
    "sketchup", "solidworks", "rendering", "landscape-design",
    "architecture", "interior-design", "fashion-design",
    # Media / video / audio
    "photography", "video-production", "animation", "motion-graphics",
    "audio-production", "voice-talent", "podcasts",
    "adobe-premiere-pro", "adobe-after-effects", "adobe-photoshop",
    "adobe-illustrator", "adobe-indesign", "final-cut-pro", "davinci-resolve",
    # Games
    "game-development", "unity", "construct-3", "godot", "unreal-engine",
    "pygame", "cocos2d", "corona-sdk",
    # Server / sysadmin / security (non-web-hosting)
    "virtual-private-server", "linux", "server-management",
    "network-administration", "network-security", "cybersecurity",
    "ethical-hacking", "penetration-testing",
    # Finance / legal / medical
    "accounting", "bookkeeping", "financial-planning", "legal",
    "medical-writing", "healthcare", "nursing",
    # Marketing (non-dev)
    "affiliate-marketing", "digital-marketing", "internet-marketing",
    "social-media-marketing", "link-building",
    # Product / business development
    "product-development", "product-launch", "business-plans",
    "startup-development",
    # Science / math
    "statistics", "mathematics", "physics", "chemistry",
    # Tutoring / language
    "chinese-tutoring", "english-tutoring", "language-tutoring", "tutoring",
    # Machine learning / BI tools
    "machine-learning", "tableau", "power-bi", "business-intelligence",
    # Writing (additional variants)
    "articles", "content-writing", "blogging",
    # Geotechnical / specialist engineering
    "geotechnical-engineering",
    # PCB variant slugs
    "pcb-design-and-layout",
    # Finance
    "financial-analysis", "financial-modeling", "financial-planning",
    # Audio / media services
    "audio-services", "audio-editing",
    # Research
    "research",
    # ERP / specialist platforms (require platform-specific skills)
    "dynamic-365", "sap",
    # Blockchain / crypto
    "blockchain", "ethereum", "solidity", "defi", "nft",
    # Legacy / niche languages outside web stack
    "cobol", "mainframe", "fortran", "pascal", "delphi",
    # Forensics / recovery (not web dev)
    "digital-forensics", "computer-forensics", "data-recovery",
    # Tutoring/teaching (not web dev)
    "english-teaching",
}


def category_blocked(project):
    """Return the Freelancer category slug if the project is in a blocked category."""
    seo = (project.get("seo_url") or "").strip("/")
    if not seo:
        return None
    category = seo.split("/")[0].lower()
    return category if category in _BLOCKED_CATEGORIES else None


_INTENT_WORDS = [
    "build", "develop", "create", "design", "integrate",
    "fix", "debug", "redesign", "migrate", "launch",
    "website", "app", "platform", "system", "tool",
    "developer", "engineer", "programmer", "coder",
]

def keyword_match(project):
    """Return the first matching keyword if title/description contains a skill keyword
    alongside at least one intent word (indicating a build/dev context)."""
    text = " ".join([
        project.get("title", "") or "",
        project.get("description", "") or "",
    ]).lower()
    has_intent = any(iw in text for iw in _INTENT_WORDS)
    if not has_intent:
        return None
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
    # Hard stop — raises immediately if eligibility was never confirmed
    if not project.get("eligibility_confirmed", False):
        raise Exception(f"SAFETY VIOLATION: draft_bid called without eligibility check on {project.get('title')}")

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
        # System prompt is cached — portfolio JSON is large and identical every call.
        # Cache TTL is 5 minutes; with 30-second polling the cache stays warm.
        cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=cached_system,
            messages=messages,
        )
        usage = response.usage
        log(
            f"Claude usage — input: {usage.input_tokens} "
            f"(cached: {getattr(usage, 'cache_read_input_tokens', 0)}) "
            f"output: {usage.output_tokens}"
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
                model="claude-sonnet-4-6",
                max_tokens=600,
                system=cached_system,
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


def check_project_eligibility(project_id, token, my_skill_ids, project=None):
    """GET full project details and check for bid blockers before calling Claude.

    Returns (eligible: bool, reason: str | None).
    Reasons prefixed with "SILENT:" are logged but do NOT trigger a Telegram message.
    Sets project["eligibility_confirmed"] = True on the passed-in dict when eligible.
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
        client_city = ((owner_detail.get("location") or {}).get("city") or "")
        log(f"Eligibility country check: project {project_id} owner country = '{client_country or 'BLANK'}' city = '{client_city or 'BLANK'}'")
        if client_country and client_country.lower() in _BLOCKED_COUNTRIES:
            return False, f"SILENT:Blocked country from project details ({client_country})"
        if owner_city_blocked(owner_detail):
            return False, f"SILENT:Blocked city from project details ({client_city})"

        # Check 2: NDA requirement — catch before calling Claude
        if upgrades.get("nda"):
            return False, "NDA:NDA signature required"

        # Check 3: non-English language field
        lang = (proj.get("language") or "").strip().lower()
        if lang and lang != "en":
            return False, f"SILENT:Non-English project (language={lang})"

        # Check 4: required skills the bidder doesn't have
        if my_skill_ids:
            required_jobs = proj.get("jobs", []) or []
            required_ids  = {str(j.get("id")) for j in required_jobs if j.get("id")}
            missing = required_ids - my_skill_ids
            if missing:
                return False, f"Missing required skills (IDs: {', '.join(sorted(missing))})"

        if project is not None:
            project["eligibility_confirmed"] = True
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
    """Return (amount, label) at 70% of max budget, or (None, reason) if budget missing.
    Amount is in the project's native currency — no conversion applied."""
    p_type = project.get("type", "fixed")
    budget = project.get("budget", {}) or {}
    min_b  = float(budget.get("minimum") or 0)
    max_b  = float(budget.get("maximum") or 0)
    sign   = (project.get("currency") or {}).get("sign", "$")

    if not max_b:
        return None, "missing or zero maximum budget"

    amount = max(round(max_b * 0.70), round(min_b))
    label  = f"{sign}{amount} (70% of {sign}{max_b:.0f} max {'hourly rate' if p_type == 'hourly' else 'budget'})"
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
                "sealed":               True,
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
# Per-project bid pipeline — the ONLY place draft_bid() is called
# ---------------------------------------------------------------------------
def mark_seen_immediately(project_id):
    """Load seen IDs from disk, stamp this ID, and flush back. Thread-safe.
    Returns True if the project was NOT yet seen (i.e. we should process it),
    False if it was already seen (duplicate — skip). The lock prevents the
    websocket thread and the poll loop from both processing the same project."""
    with _seen_lock:
        seen = load_seen_ids()
        if str(project_id) in seen:
            return False  # already claimed by another thread
        seen[str(project_id)] = time.time()
        cleanup_and_save(seen)
        return True


def process_project(project, token, portfolio, tg_token, tg_chat, my_skill_ids, jobs_dict, country_name):
    """Single authoritative pipeline: mark seen → eligibility → Claude → submit.

    This is the ONLY function in the file that calls draft_bid().
    draft_bid() is physically unreachable unless check_project_eligibility()
    returns True — there is no other code path that reaches it.
    """
    project_id = str(project.get("id", ""))
    title      = project.get("title", "")[:80]
    link       = project_link(project)
    budget     = fmt_budget(project)

    # STEP 1: Mark seen immediately — atomic, thread-safe. Returns False if
    # another thread (websocket or poll loop) already claimed this project.
    if not mark_seen_immediately(project_id):
        log(f"SKIPPED [duplicate - already claimed] {title}")
        return False

    # STEP 2: Eligibility check — MUST happen before Claude
    eligible, skip_reason = check_project_eligibility(project_id, token, my_skill_ids, project)
    if not eligible:
        if skip_reason.startswith("NDA:"):
            log(f"NDA REQUIRED [eligibility] {title}")
            send_telegram(
                f"📝 NDA PROJECT\n\n"
                f"📋 Project: {title}\n"
                f"🔗 {link}\n"
                f"💰 Budget: {budget}\n"
                f"🌍 Country: {country_name}\n\n"
                f"Sign the NDA at the project page, then bid manually.",
                tg_token, tg_chat,
            )
        else:
            silent         = skip_reason.startswith("SILENT:")
            display_reason = skip_reason[7:] if silent else skip_reason
            log(f"SKIPPED [eligibility] {title} — {display_reason}")
            if not silent:
                send_telegram(f"⛔ SKIPPED - {display_reason}:\n{title}\n{link}", tg_token, tg_chat)
        return False

    log(f"ELIGIBLE: {title} — calling Claude now")

    # STEP 3: Bid amount (only reached if eligible)
    amount, amount_label = calc_bid_amount(project)
    if amount is None:
        log(f"SKIPPED [no bid amount] {title} — {amount_label}", "warning")
        return False
    log(f"Bid amount: {amount_label}")

    # STEP 4: Draft bid — only reached if eligible
    skill_names = get_skill_names(project, jobs_dict)
    bid_text = draft_bid(project, skill_names, portfolio)
    if not bid_text:
        log(f"DRAFT FAILED: {title}")
        return False
    log_portfolio_chosen(bid_text, portfolio)

    # STEP 5: Submit
    success, error = submit_bid(project, bid_text, amount, token)
    if error == "ALREADY_BID":
        log(f"SKIPPED [already bid] {title}")
        return False
    if error == "WRONG_LANGUAGE":
        log(f"SKIPPED [wrong language] {title}", "warning")
        send_telegram(f"⛔ SKIPPED - Wrong language: {title}", tg_token, tg_chat)
        return False
    if error == "TOO_FAST":
        log("TOO_FAST — waiting 45 seconds and retrying once...", "warning")
        time.sleep(45)
        success, error = submit_bid(project, bid_text, amount, token)
        if error == "TOO_FAST":
            log(f"SKIPPED [still too fast] {title}", "warning")
            return False

    SEP = "\u2500" * 25
    if success:
        tg_msg = (
            f"✅ BID PLACED\n\n"
            f"📋 Project: {title}\n"
            f"🔗 {link}\n"
            f"💰 Budget: {budget}\n"
            f"🌍 Country: {country_name}\n\n"
            f"{SEP}\n\n{bid_text}\n\n{SEP}"
        )
    else:
        tg_msg = (
            f"⚠️ BID FAILED\n\n"
            f"📋 Project: {title}\n"
            f"🔗 {link}\n"
            f"💰 Budget: {budget}\n"
            f"🌍 Country: {country_name}\n"
            f"❌ Reason: {error}\n\n"
            f"{SEP}\n\n{bid_text}\n\n{SEP}"
        )

    if send_telegram(tg_msg, tg_token, tg_chat):
        save_recent_alert(project, country_name, skill_names)
    return success


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

    new_seen = dict(seen_ids)
    now      = time.time()
    counts = {
        "seen": 0, "currency": 0, "country": 0, "india": 0,
        "language": 0, "budget": 0,
        "blocklist": 0, "category": 0, "skill": 0, "eligibility": 0,
        "spam_client": 0, "local_job": 0,
    }

    alerts_sent = 0

    for project in projects:
        proj_id = str(project.get("id", ""))
        if not proj_id:
            continue

        title_short = f"\"{project.get('title', '')[:60]}\""

        # --- Filters ---

        if proj_id in seen_ids:
            counts["seen"] += 1
            log(f"FILTERED [seen] {title_short}")
            continue

        owner_id     = str(project.get("owner_id", ""))
        owner        = users.get(owner_id) or {}
        country_name = (((owner.get("location") or {}).get("country") or {}).get("name") or "")

        if not country_allowed(country_name, allowed):
            counts["country"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [country] {title_short} country=\"{country_name}\"")
            continue

        if owner_city_blocked(owner):
            counts["country"] += 1
            new_seen[proj_id] = now
            owner_city = ((owner.get("location") or {}).get("city") or "")
            log(f"FILTERED [country] {title_short} city=\"{owner_city}\" (currency was {(project.get('currency') or {}).get('code', '?')})")
            continue

        if (project.get("currency") or {}).get("code", "") == "INR":
            counts["currency"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [currency] {title_short} budget={fmt_budget(project)}")
            continue

        if not is_english(project):
            counts["language"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [language] {title_short} country=\"{country_name}\"")
            continue

        if not budget_ok(project, settings):
            counts["budget"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [budget] {title_short} budget={fmt_budget(project)}")
            continue

        if is_india_project(project):
            counts["india"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [india] {title_short}")
            continue

        blocked_cat = category_blocked(project)
        if blocked_cat:
            counts["category"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [category] {title_short} category=\"{blocked_cat}\"")
            continue

        blocked_kw = blocklist_match(project)
        if blocked_kw:
            counts["blocklist"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [blocklist] {title_short} keyword=\"{blocked_kw}\"")
            continue

        matched_kw = keyword_match(project)
        if not matched_kw:
            counts["skill"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [skill] {title_short}")
            continue

        # Spam client filter — contacted too many freelancers
        invited = int(project.get("invited_freelancer_count") or 0)
        if invited > 20:
            counts["spam_client"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [spam client - contacted 20+ freelancers] {title_short}")
            continue

        # High bid count — too many bids already, won't be seen
        bid_count = int((project.get("bid_stats") or {}).get("bid_count") or 0)
        if bid_count > 50:
            counts["spam_client"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [too many bids - {bid_count}] {title_short}")
            continue

        # Local job filter — requires physical presence
        if project.get("local"):
            counts["local_job"] += 1
            new_seen[proj_id] = now
            log(f"FILTERED [local job] {title_short}")
            continue

        # --- All filters passed — hand off to process_project() ---
        log(
            f"PASSED [{proj_id}] \"{project.get('title', '')[:60]}\" "
            f"budget={fmt_budget(project)} country=\"{country_name}\" keyword=\"{matched_kw}\""
        )
        new_seen[proj_id] = now  # keep final save consistent with mark_seen_immediately
        result = process_project(project, token, portfolio, tg_token, tg_chat, my_skill_ids, jobs_dict, country_name)
        if result:
            alerts_sent += 1

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
        f"category: {counts['category']} | "
        f"blocklist: {counts['blocklist']} | "
        f"skill: {counts['skill']} | "
        f"spam_client: {counts['spam_client']} | "
        f"local_job: {counts['local_job']} | "
        f"eligibility: {counts['eligibility']}"
    )

    cleaned = cleanup_and_save(new_seen)
    log(f"Saved {len(cleaned)} seen IDs (after 3-day cleanup)")
    log(f"Done — checked {len(projects)}, sent {alerts_sent} alert(s).")

    save_last_run(len(projects), alerts_sent)


# ---------------------------------------------------------------------------
# Websocket listener — real-time new project feed
# ---------------------------------------------------------------------------
_ws_queue: queue.Queue = queue.Queue()


def fetch_project_by_id(project_id, token):
    """Fetch full details for a single project ID including user and job details."""
    try:
        resp = requests.get(
            f"{FREELANCER_API}/projects/",
            params=[
                ("projects[]",       project_id),
                ("full_description", "true"),
                ("job_details",      "true"),
                ("user_details",     "true"),
            ],
            headers={"Freelancer-OAuth-V1": token},
            timeout=20,
        )
        if resp.status_code == 200:
            result    = resp.json().get("result", {}) or {}
            projects  = result.get("projects", {}) or {}
            # API returns a dict keyed by project ID
            project   = projects.get(str(project_id)) if isinstance(projects, dict) else (projects[0] if projects else None)
            users     = result.get("users",    {}) or {}
            jobs_dict = result.get("jobs",     {}) or {}
            return project, users, jobs_dict
        log(f"Websocket: project fetch failed ({resp.status_code}) for ID {project_id}", "warning")
    except Exception as e:
        log(f"Websocket: project fetch error for ID {project_id}: {e}", "warning")
    return None, {}, {}


def process_single_project(project_id, bot_state):
    """Run the full filter → eligibility → Claude → bid pipeline for one project ID.
    Mirrors the inner loop of main(); called from the websocket processor thread."""
    if bot_state and bot_state.get("paused"):
        return

    settings = load_settings()
    token    = settings["freelancer_token"]
    tg_token = settings["telegram_bot_token"]
    tg_chat  = str(settings["telegram_chat_id"])
    allowed  = build_country_set(settings)

    # Fetch skill IDs (needed for eligibility check)
    my_skill_ids = set()
    try:
        me     = requests.get(
            "https://www.freelancer.com/api/users/0.1/self/",
            headers={"Freelancer-OAuth-V1": token},
            params={"skill_details": "true"},
            timeout=10,
        ).json()
        jobs   = (me.get("result") or {}).get("jobs", []) or []
        my_skill_ids = {str(s.get("id")) for s in jobs if s.get("id")}
    except Exception as e:
        log(f"Websocket: could not fetch skill IDs: {e}", "warning")

    portfolio = load_json(PORTFOLIO_FILE, [])
    seen_ids  = load_seen_ids()
    now       = time.time()
    proj_id   = str(project_id)

    if proj_id in seen_ids:
        return  # Already handled by scan loop or a prior websocket event

    project, users, jobs_dict = fetch_project_by_id(proj_id, token)
    if not project:
        log(f"Websocket: could not fetch details for project {proj_id}", "warning")
        return

    title_short = f"\"{project.get('title', '')[:60]}\""

    # Country filter
    owner_id     = str(project.get("owner_id", ""))
    owner        = users.get(owner_id) or {}
    location     = (owner.get("location") or {})
    country_name = ((location.get("country") or {}).get("name") or "")
    if not country_allowed(country_name, allowed):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [country] {title_short} country=\"{country_name}\"")
        return

    if owner_city_blocked(owner):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [country] {title_short} city=\"{location.get('city', '')}\"")
        return

    # Currency filter
    if (project.get("currency") or {}).get("code", "") == "INR":
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [currency] {title_short}")
        return

    # Language filter
    if not is_english(project):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [language] {title_short}")
        return

    # Budget filter
    if not budget_ok(project, settings):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [budget] {title_short} budget={fmt_budget(project)}")
        return

    # India content filter
    if is_india_project(project):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [india] {title_short}")
        return

    # Category filter — block entire Freelancer job categories we can't bid on
    blocked_cat = category_blocked(project)
    if blocked_cat:
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [category] {title_short} category=\"{blocked_cat}\"")
        return

    # Blocklist filter
    blocked_kw = blocklist_match(project)
    if blocked_kw:
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [blocklist] {title_short} keyword=\"{blocked_kw}\"")
        return

    # Skill keyword filter
    matched_kw = keyword_match(project)
    if not matched_kw:
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [skill] {title_short}")
        return

    # Spam client filter — contacted too many freelancers
    invited = int(project.get("invited_freelancer_count") or 0)
    if invited > 20:
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [spam client - contacted 20+ freelancers] {title_short}")
        return

    # High bid count — too many bids already, won't be seen
    bid_count = int((project.get("bid_stats") or {}).get("bid_count") or 0)
    if bid_count > 50:
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [too many bids - {bid_count}] {title_short}")
        return

    # Local job filter — requires physical presence
    if project.get("local"):
        seen_ids[proj_id] = now; cleanup_and_save(seen_ids)
        log(f"FILTERED [local job] {title_short}")
        return

    # All filters passed — hand off to the single authoritative pipeline
    log(
        f"WEBSOCKET PASSED [{proj_id}] \"{project.get('title', '')[:60]}\" "
        f"country=\"{country_name}\" keyword=\"{matched_kw}\""
    )
    process_project(project, token, portfolio, tg_token, tg_chat, my_skill_ids, jobs_dict, country_name)


def ws_processor(bot_state):
    """Background thread: drain _ws_queue and run the bid pipeline for each project."""
    while True:
        project_id = _ws_queue.get()
        try:
            process_single_project(project_id, bot_state)
        except Exception as e:
            log(f"Websocket processor error for project {project_id}: {e}", "warning")
        finally:
            _ws_queue.task_done()


def listen_websocket(bot_state):
    """Connect to the Freelancer push websocket and queue new project IDs.
    Runs in a background thread; auto-reconnects on disconnect after 5 seconds.

    Auth + subscription format is based on the Freelancer push service at
    wss://www.freelancer.com/push. If the handshake format changes, update
    on_open() below. See: https://developers.freelancer.com
    """
    try:
        import websocket as websocket_client
    except ImportError:
        log("ERROR: websocket-client not installed. Run: pip install websocket-client", "error")
        return

    def get_token():
        return load_settings()["freelancer_token"]

    _reconnect_count = [0]

    def on_open(ws):
        count = _reconnect_count[0]
        if count == 0:
            log("WEBSOCKET: Connected — authenticated and subscribed to projects/posted")
        else:
            log(f"WEBSOCKET: Reconnected (attempt {count}) — authenticated and subscribed to projects/posted")
        token = get_token()
        ws.send(json.dumps({"command": "auth",      "token":   token}))
        ws.send(json.dumps({"command": "subscribe", "channel": "projects/posted"}))

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            log(f"WEBSOCKET EVENT: non-JSON message received: {str(message)[:120]}")
            return

        event   = data.get("event") or data.get("type") or data.get("channel", "")
        payload = data.get("data")  or data.get("payload") or data

        log(f"WEBSOCKET EVENT received: {event!r}")

        # Accept any of the event names Freelancer might use for new projects
        if event not in ("projects/posted", "newProject", "project.posted", "project"):
            return

        project_id = (
            payload.get("id")
            or payload.get("project_id")
            or (payload.get("project") or {}).get("id")
        )
        title = (
            payload.get("title")
            or (payload.get("project") or {}).get("title", "")
        )
        if not project_id:
            log(f"WEBSOCKET EVENT: matched event {event!r} but no project_id found in payload")
            return

        log(f"WEBSOCKET: New project received — \"{str(title)[:60]}\" — processing immediately")
        _ws_queue.put(str(project_id))

    def on_error(ws, error):
        log(f"WEBSOCKET: Error — {error}", "warning")

    def on_close(ws, close_status_code, close_msg):
        log(f"WEBSOCKET: Disconnected (code={close_status_code}, msg={close_msg}) — reconnecting in 5 seconds", "warning")

    while True:
        try:
            token = get_token()
            ws = websocket_client.WebSocketApp(
                f"wss://www.freelancer.com/push?token={token}",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log(f"WEBSOCKET: Crashed — {e}", "warning")
        _reconnect_count[0] += 1
        time.sleep(5)


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

    # Start websocket processor (drains _ws_queue)
    _ws_proc = threading.Thread(target=ws_processor, args=(bot_state,), daemon=True)
    _ws_proc.start()
    log("Websocket processor thread started.")

    # Start websocket listener (connects to Freelancer push service)
    _ws_listener = threading.Thread(target=listen_websocket, args=(bot_state,), daemon=True)
    _ws_listener.start()
    log("Websocket listener thread started.")

    while True:
        main(bot_state)
        time.sleep(30)
