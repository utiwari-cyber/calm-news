"""Calm News pipeline: fetch trusted feeds, group same-story articles, drop clickbait,
rewrite neutrally (optional, free Gemini tier), rank by importance, write feed.json.

Usage:  python3 pipeline.py            # uses GEMINI_API_KEY if set, else no rewrite
Stdlib only, so it runs anywhere (laptop or GitHub Actions) with no installs.
"""
import concurrent.futures as cf
import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "sources.json").read_text())
OUT = ROOT / "out"
STATE = ROOT / "state"
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE_HOURS = 36
MIN_WORDS = 35  # news cards with shorter summaries are dropped
PROMPT_VERSION = 2  # bump to re-rewrite cached stories after a prompt change
UA = "Mozilla/5.0 (CalmNews personal reader)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

TIER1_DOMAINS = ("reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "abc.net.au", "rnz.co.nz", "npr.org")
TIER_WEIGHT = {0: 1.5, 1: 3.0, 2: 2.0, 3: 1.5}

CLICKBAIT = [
    r"\bslams?\b", r"\bblasts?\b", r"\bdestroys?\b", r"\bshock(ing|ed)?\b", r"\bstunn(ing|ed)\b",
    r"jaw[- ]dropping", r"you won'?t believe", r"goes viral", r"\bviral\b", r"\bnetizens\b", r"\bepic\b",
    r"\bmeltdown\b", r"here'?s why", r"this is what", r"\bmust[- ]see\b", r"\bbombshell\b", r"\bfuming\b",
    r"\bfurious\b", r"\boutrage\b", r"\bslammed\b", r"\bbrutal(ly)?\b", r"\binsane\b", r"\bunbelievable\b",
    r"\bmind[- ]blowing\b", r"\bgame[- ]changer\b", r"\bjust\b.*\bdid\b",
]
EXCLUDE = [
    r"horoscope", r"\bphotos?:", r"\bwatch:", r"\bvideo:", r"\bin pics\b", r"\blive( updates| blog|:)",
    r"\bquiz\b", r"crossword", r"wordle", r"\bpodcast\b", r"\bcelebrit", r"\bbollywood\b", r"\bkardashian",
    r"\bgossip\b", r"\bhow to\b", r"\bbest deals?\b", r"\bdeals? (on|for)\b", r"black friday", r"\bsavings\b", r"pre-order", r"\bjobs? 20\d\d", r"recruitment",
    r"vacanc", r"admit card", r"\|\s*live\b", r"\beligibility\b", r"\bprice list\b", r"\bexplained\b", r"\bcheck (out )?(list|details)\b", r"\btrolled\b", r"\bweekend reads?\b", r"\bdeal(s)? of the day\b", r"\bbest .* to buy\b",
    r"\bcoupons?\b", r"promo codes?", r"\bbest .*\(20\d\d\)", r"\d+% off\b", r"\blast chance\b",
]
STOP = set("""a an the and or of to in on for at by with from as is are was were be been it its this that
these those after over into about than amid says said say new will would could may might not no up out
s has have had his her their they he she we you your our who what when where why how all more most""".split())


# ---------- fetch & parse ----------

def gn_url(query, section, days=1):
    hl, gl, ceid = ("en-IN", "IN", "IN:en") if section in ("india", "india_politics", "india_sports", "northeast", "lucknow") else ("en-AU", "AU", "AU:en")
    q = urllib.parse.quote(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url, ua=UA, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit) if limit else r.read()
    # Some feeds (e.g. DeepMind) send gzip even when it wasn't asked for
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, *names):
    # Returns the first matching child; avoid `a or b` on Elements, which are falsy when childless
    for name in names:
        for c in el:
            if local(c.tag) == name:
                return c
    return None


def image_of(el):
    for c in el.iter():
        name = local(c.tag)
        if name in ("content", "thumbnail") and c.get("url") and (c.get("medium") in (None, "image") or name == "thumbnail"):
            return c.get("url")
        if name == "enclosure" and (c.get("type") or "").startswith("image"):
            return c.get("url")
    return None


def domain_of(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def section_for(src, title, desc):
    """Broad feeds (e.g. a Reuters search) keep their section only if the story is about it."""
    sec = src["section"]
    text = f"{title} {desc[:300]}"
    for alt in src.get("route", []):  # e.g. a sports feed's cricket stories go to Cricket
        if re.search(CONFIG["section_keywords"][alt], text, re.I):
            return alt
    if sec == "world" or src.get("dedicated"):
        return sec
    if re.search(CONFIG["section_keywords"][sec], text, re.I):
        return sec
    if sec in ("northeast", "lucknow"):  # local outlets also carry national news
        return "india" if re.search(CONFIG["section_keywords"]["india"], f"{title} {desc}", re.I) else "world"
    return src.get("fallback", "world")


def parse_feed(src, raw):
    root = ET.fromstring(raw)
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        title = clean(getattr(child(e, "title"), "text", ""))
        link_el = child(e, "link")
        link = (link_el.get("href") if link_el is not None and link_el.get("href") else getattr(link_el, "text", "")) or ""
        desc_el = child(e, "description", "summary", "content")
        desc = clean(getattr(desc_el, "text", ""), 1200)
        date = parse_date(getattr(child(e, "pubDate", "published", "updated"), "text", None))
        name, tier = src["name"], src["tier"]

        if "gn" in src:  # Google News: keep only allowlisted publishers, take real publisher name
            s = child(e, "source")
            pub_url = s.get("url") if s is not None else ""
            dom = domain_of(pub_url)
            if not any(dom == d or dom.endswith("." + d) for d in src["domains"]):
                continue
            name = clean(s.text) if s is not None and s.text else name
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
            if desc.startswith(title[:40]):
                desc = ""  # GN descriptions just repeat the headline
            if dom.endswith(TIER1_DOMAINS):
                tier = 1

        if not title or not link:
            continue
        out.append({
            "source_id": src["id"], "source": name, "tier": tier, "section": section_for(src, title, desc),
            "title": title, "link": link.strip(), "desc": desc, "image": image_of(e),
            "published": date.isoformat() if date else None,
            "max_age_h": src.get("days", 0) * 24 or MAX_AGE_HOURS,
        })
    return [a for a in out if a["section"] != "skip"]  # off-topic items from a narrow feed


def fetch_all():
    articles, failures = [], []

    def one(src):
        url = gn_url(src["gn"], src["section"], src.get("days", 1)) if "gn" in src else src["rss"]
        return parse_feed(src, fetch(url))

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(one, s): s for s in CONFIG["sources"]}
        for fut in cf.as_completed(futures):
            try:
                articles += fut.result()
            except Exception as ex:  # one bad feed never breaks the run
                failures.append(f"{futures[fut]['id']}: {str(ex)[:120]}")
    fresh = [a for a in articles if not a["published"]
             or dt.datetime.fromisoformat(a["published"]) >= NOW - dt.timedelta(hours=a["max_age_h"])]
    seen, unique = set(), []
    for a in fresh:
        if a["link"] not in seen:
            seen.add(a["link"])
            unique.append(a)
    return unique, failures


# ---------- group same-story articles ----------

def tokens(text):
    words = (w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP)
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words}  # crude plural folding


def similarity(a, b):
    """Jaccard over title+text, or a strong headline overlap: headline-only items with
    different wording (common from Google News) still land in the same story."""
    shared = len(a["_tok"] & b["_tok"])
    jac = shared / (len(a["_tok"] | b["_tok"]) or 1) if shared >= 3 else 0
    st = len(a["_ttok"] & b["_ttok"])
    overlap = st / (min(len(a["_ttok"]), len(b["_ttok"])) or 1) if st >= 3 else 0
    return max(jac, 0.3 if overlap >= 0.6 else 0)


def cluster(articles):
    articles.sort(key=lambda a: (a["tier"] if a["tier"] else 9, a["published"] or ""))
    clusters = []
    for a in articles:
        a["_tok"] = tokens(a["title"] + " " + a["desc"][:200])
        a["_ttok"] = tokens(a["title"])
        best, best_sim = None, 0.0
        for c in clusters:
            for m in c:
                sim = similarity(a, m)
                if sim > best_sim:
                    best, best_sim = c, sim
        if best is not None and best_sim >= 0.28:
            best.append(a)
        else:
            clusters.append([a])
    return [make_story(c) for c in clusters]


def make_story(members):
    specific = [m["section"] for m in members if m["section"] != "world"]
    section = max(set(specific), key=specific.count) if specific else "world"
    lead = min(members, key=lambda m: (m["tier"] if m["tier"] else 9, -len(m["desc"])))
    return {
        "id": hashlib.sha1(lead["link"].encode()).hexdigest()[:12],
        "section": section, "lead": lead, "members": members,
        "strict": section in ("northeast", "lucknow"),
    }


# ---------- rules: corroboration, exclusions, word list ----------

def apply_rules(stories):
    kept, dropped = [], []
    for s in stories:
        title = s["lead"]["title"]
        low = title.lower()
        rule = CONFIG["corroboration"][s["section"]]
        names = {m["source"] for m in s["members"] if m["tier"] in rule["tiers"]}
        has_t1 = any(m["tier"] == rule.get("or_one_tier", -1) for m in s["members"])
        hits = [p for p in CLICKBAIT if re.search(p, low)]
        if title.endswith("?"):
            hits.append("question headline")
        if re.search(r"\b[A-Z]{3,}\s+[A-Z]{3,}\b", title) or "BREAKING" in title:
            hits.append("all caps")

        if any(re.search(p, low) for p in EXCLUDE) or len(title.split()) < 4:
            dropped.append((s, "excluded category"))
        elif len(names) < rule["min_sources"] and not has_t1:
            dropped.append((s, f"waiting for corroboration ({len(names)} source)"))
        elif hits and (s["strict"] or len(hits) >= 2):
            dropped.append((s, "clickbait words: " + ", ".join(h.strip("\\b") for h in hits)))
        else:
            s["penalty"] = len(hits)
            s["prescore"] = prescore(s)
            kept.append(s)
    return kept, dropped


def prescore(s):
    by_source = {}
    for m in s["members"]:
        by_source[m["source"]] = max(by_source.get(m["source"], 0), TIER_WEIGHT[m["tier"]])
    coverage = min(sum(by_source.values()), 12)
    dates = [dt.datetime.fromisoformat(m["published"]) for m in s["members"] if m["published"]]
    age_h = (NOW - min(dates)).total_seconds() / 3600 if dates else 12
    recency = max(0.0, 2.0 * (1 - age_h / 24))
    no_text = 3 if max(len(m["desc"]) for m in s["members"]) < 80 else 0
    return coverage + recency - 2 * s["penalty"] - no_text


# ---------- AI: sensationalism score + neutral rewrite (free Gemini tier) ----------

PROMPT = """You edit a calm but engaging news feed: factual like a wire service, readable like a good magazine.
For each story below return a JSON array, one object per story:
{"id": str,
 "scores": {"emotional": 0-5, "speculation": 0-5, "attribution": 0-5, "outrage": 0-5, "filler": 0-5},
 "impact": 0-5, "durability": 0-5,
 "headline": str, "summary": str, "why": str}

Scores rate the SOURCE material (0 = clean, 5 = severe): emotional = words telling the reader how to feel;
speculation = "could/set to/may" carrying the story; attribution = claims with no named source; outrage = reactions
instead of events; filler = celebrity, viral posts, routine crime or accidents with no wider significance.
impact = how many people it affects; durability = will it still matter next month.

headline: 7-13 words, present tense, no full stop. Make it specific and interesting: lead with the most striking
concrete fact in the input (a number, a name, a first, a record, a surprising detail) and use precise, active verbs,
so a curious reader wants to read on. It must stay strictly true to the input: no exaggeration, no questions,
no "here's why", no puns that bend the meaning, no adjectives judging the event.
summary: 50-80 words in 3-4 short sentences. Write like a sharp, warm editor: open with the most interesting fact,
then who, where and when, then the key number or detail, then the context or what happens next ONLY if the
input gives it. Active voice, plain words, varied sentence starts. Attribute claims to the people or bodies
making them ("the ministry said"). Never name the news outlet (the app shows sources separately) and never
write "according to reports", "according to the headline" or similar. STRICT: every fact must appear in the input;
never add causes, consequences or background you were not given. No filler such as "this matters because" or
"remains to be seen". If the input is too thin for 50 words, write what it supports; never pad.
Company-blog items: say "X announced".
why: under 12 words, why this is worth reading (e.g. "affects home loan rates for millions").

Stories:
"""


def load_cache():
    p = STATE / "ai_cache.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_cache(cache):
    STATE.mkdir(exist_ok=True)
    cutoff = (NOW - dt.timedelta(days=3)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("_at", "") >= cutoff}
    (STATE / "ai_cache.json").write_text(json.dumps(cache, indent=1))


def gemini(prompt, key, models, temperature=0.3):
    """Try each model in turn; the free tier often answers 503 'high demand' for a while."""
    last = None
    for model in models:
        for wait in (0, 10, 30):
            time.sleep(wait)
            try:
                return gemini_call(prompt, key, model, temperature)
            except urllib.error.HTTPError as ex:
                if not (last and ex.code == 404):  # a retired model shouldn't hide the real (quota) error
                    last = ex
                if ex.code not in (429, 500, 503):
                    break
            except (TimeoutError, urllib.error.URLError) as ex:
                last = ex
    raise last


def story_payload(batch):
    return [{
        "id": s["id"], "section": s["section"],
        "sources": sorted({m["source"] for m in s["members"]}),
        "headlines": [m["title"] for m in s["members"][:4]],
        "text": max((m["desc"] for m in s["members"]), key=len)[:1400],
    } for s in batch]


def gemini_call(prompt, key, model, temperature):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=150) as r:
        data = json.loads(r.read())
    return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])


GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
MODELS = os.environ.get("GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest").split(",")


def ai_pass(candidates, log):
    key, models = GEMINI_KEY, MODELS
    cache = load_cache()
    todo = []
    for s in candidates:
        hit = next((cache[m["link"]] for m in s["members"]
                    if cache.get(m["link"], {}).get("_v") == PROMPT_VERSION), None)
        if hit:
            s["ai"] = hit
        else:
            todo.append(s)
    if not key:
        log.append(f"AI: no GEMINI_API_KEY set, {len(todo)} stories left un-rewritten")
    else:
        for i in range(0, len(todo), 10):  # 10 stories per request keeps us far inside free daily limits
            batch = todo[i:i + 10]
            try:
                prompt = PROMPT + json.dumps(story_payload(batch), ensure_ascii=False)
                results = {r["id"]: r for r in gemini(prompt, key, models)}
            except Exception as ex:
                log.append(f"AI: stopped after {i} stories ({str(ex)[:100]})")
                break
            for s in batch:
                r = results.get(s["id"])
                if r:
                    r["_at"], r["_v"] = NOW.isoformat(), PROMPT_VERSION
                    s["ai"] = r
                    for m in s["members"]:
                        cache[m["link"]] = r
            time.sleep(7)  # stay under ~10 requests/minute
        save_cache(cache)

    kept, dropped = [], []
    for s in candidates:
        ai = s.get("ai")
        if ai:
            sc = ai["scores"]
            total = sum(sc.values())
            limit = 5 if s["strict"] else 8
            if total > limit or max(sc.values()) >= 5:
                dropped.append((s, f"AI sensationalism {total}/25 {sc}"))
                continue
            if len(ai.get("summary", "").split()) < MIN_WORDS:
                dropped.append((s, f"too short ({len(ai.get('summary', '').split())} words)"))
                continue
            s["score"] = s["prescore"] + 1.5 * ai.get("impact", 2) + ai.get("durability", 2)
        elif len(fallback_summary(s).split()) < MIN_WORDS:
            dropped.append((s, "too short (no rewrite)"))
            continue
        else:
            s["score"] = s["prescore"]
        kept.append(s)
    return kept, dropped


# ---------- fuller text for headline-only stories ----------

def resolve_gn(link):
    """Google News article links hide the publisher URL behind a JS redirect; this is the
    same two-step lookup the news.google.com page does."""
    aid = link.split("/articles/", 1)[1].split("?", 1)[0]
    page = fetch(f"https://news.google.com/articles/{aid}", BROWSER_UA).decode("utf8", "ignore")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], aid, int(ts), sg]
    form = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])}).encode()
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=form, headers={
        "User-Agent": BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
    return json.loads(json.loads(raw.split("\n\n")[1])[0][2])[1]


JUNK_PARA = re.compile(r"cookie|subscribe|sign up|newsletter|javascript|all rights reserved|advertis|click here|log ?in", re.I)


def page_text(url):
    page = fetch(url, BROWSER_UA, 400_000).decode("utf8", "ignore")
    meta = re.search(r'<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]*)"', page) \
        or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="(?:og:description|description)"', page)
    parts = [clean(meta.group(1))] if meta else []
    for para in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", page, re.S):
        t = clean(para)
        if len(t) > 80 and not JUNK_PARA.search(t) and t not in parts:
            parts.append(t)
        if sum(map(len, parts)) > 1200:
            break
    return " ".join(parts)[:1400]


def enrich(candidates, log):
    """Stories whose feeds gave little more than a headline get text from the article page,
    so the rewrite has enough to work with (and fewer cards fall under MIN_WORDS)."""
    path = STATE / "text_cache_v2.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    thin = [s for s in candidates if max(len(m["desc"]) for m in s["members"]) < 300]

    def one(s):
        lead = min(s["members"], key=lambda m: (m["source"] == "Reuters", m["tier"] or 9))  # Reuters pages refuse bots
        if lead["link"] not in cache:
            try:
                url = resolve_gn(lead["link"]) if "news.google.com" in lead["link"] else lead["link"]
                cache[lead["link"]] = {"url": url, "text": page_text(url), "_at": NOW.isoformat()}
            except Exception:
                cache[lead["link"]] = {"url": None, "text": "", "_at": NOW.isoformat()}
        hit = cache[lead["link"]]
        if hit["url"]:
            lead["real_link"] = hit["url"]
        if len(hit["text"]) > len(lead["desc"]):
            lead["desc"] = hit["text"]
            return True

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        got = sum(1 for ok in pool.map(one, thin) if ok)
    log.append(f"enrich: {got}/{len(thin)} thin stories got article text")
    cutoff = (NOW - dt.timedelta(days=3)).isoformat()
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in cache.items() if v["_at"] >= cutoff}))


# ---------- final de-duplication ----------

DEDUP_PROMPT = """Below are news cards as [id, headline]. Group the ids of cards that report the SAME specific event or
announcement (same people, same action), even if worded differently. Cards on the same broad topic but about different
events are NOT duplicates. Return a JSON array of groups, each an array of 2+ ids. Return [] if there are none.

Cards:
"""


def headline_of(s):
    return s["ai"]["headline"] if s.get("ai") else s["lead"]["title"]


def dedupe(stories, log):
    """Clustering misses some repeats (different wording, cached rewrites shared across clusters).
    Merge them: the best-scored copy survives and inherits the others' sources."""
    stories.sort(key=lambda s: -s["score"])
    for s in stories:
        s["_htok"] = tokens(headline_of(s) + " " + (s.get("ai") or {}).get("summary", "")[:150])
    parent = {s["id"]: s["id"] for s in stories}
    order = {s["id"]: n for n, s in enumerate(stories)}

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    def join(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:  # keep the higher-scored (earlier) one as root
            ra, rb = sorted((ra, rb), key=order.get)
            parent[rb] = ra

    for i, a in enumerate(stories):
        for b in stories[i + 1:]:
            shared = len(a["_htok"] & b["_htok"])
            if shared >= 4 and shared / (min(len(a["_htok"]), len(b["_htok"])) or 1) >= 0.6:
                join(a["id"], b["id"])
    if GEMINI_KEY:
        try:
            listing = [[s["id"], headline_of(s)] for s in stories]
            for group in gemini(DEDUP_PROMPT + json.dumps(listing, ensure_ascii=False), GEMINI_KEY, MODELS, 0):
                ids = [g for g in group if g in parent]
                for other in ids[1:]:
                    join(ids[0], other)
        except Exception as ex:
            log.append(f"dedupe: AI pass skipped ({str(ex)[:80]})")
    by_id = {s["id"]: s for s in stories}
    kept = []
    for s in stories:
        r = root(s["id"])
        if r == s["id"]:
            kept.append(s)
        else:
            by_id[r]["members"] += s["members"]
    log.append(f"dedupe: merged {len(stories) - len(kept)} duplicate stories")
    return kept


# ---------- fun facts (Wikipedia "Did you know") ----------

PICTURED = re.compile(r"\s*\((?:pictured|left|right|centre|center|top|bottom|shown)[^)]*\)")


def wiki_json(url):
    return json.loads(fetch(url, "CalmNews/1.0 (personal news reader)"))


def fun_facts(n, log):
    try:
        data = wiki_json("https://en.wikipedia.org/w/api.php?action=parse&page=Wikipedia:Recent_additions"
                         "&redirects=1&prop=text&format=json&formatversion=2")
        hooks = re.findall(r"<li[^>]*>\s*\.\.\. (that .*?)</li>", data["parse"]["text"], re.S)[:60]
    except Exception as ex:
        log.append(f"facts: failed ({str(ex)[:80]})")
        return []
    random.shuffle(hooks)
    cards = []
    for h in hooks:
        if len(cards) >= n:
            break
        page = re.search(r'<b>(?:<i>)?<a href="/wiki/([^"#]+)"', h)
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h)))  # no spaces where links were
        fact = PICTURED.sub("", text)[5:].rstrip("?").strip()
        if not page or len(fact.split()) < 8:
            continue
        try:
            info = wiki_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{page.group(1)}")
        except Exception:
            continue
        words = (info.get("extract") or "").split()
        if len(words) < 25:
            continue
        cards.append({
            "id": hashlib.sha1(fact.encode()).hexdigest()[:12], "section": "facts",
            "headline": fact[:1].upper() + fact[1:], "summary": " ".join(words[:75]) + ("…" if len(words) > 75 else ""),
            "rewritten": False, "original_headline": fact, "why": f"About: {info.get('title', '')}",
            "sources": [{"name": "Wikipedia", "link": info["content_urls"]["desktop"]["page"]}],
            "image": (info.get("thumbnail") or {}).get("source"), "published": None, "score": 0,
        })
    return cards


# ---------- quiz ----------

def quiz_card(kind, question, options, answer, explain, source):
    order = list(range(len(options)))
    random.shuffle(order)
    return {
        "id": hashlib.sha1((kind + question).encode()).hexdigest()[:12], "section": "quiz", "type": "quiz",
        "kind": kind, "question": question, "options": [options[i] for i in order], "answer": order.index(answer),
        "explain": explain, "sources": [source] if source else [],
    }


def geography_quiz(n):
    countries = json.loads((ROOT / "geography.json").read_text())
    out = []
    for c in random.sample(countries, n):
        near = [x for x in countries if x["continent"] == c["continent"] and x["country"] != c["country"]]
        kind = random.choice(["capital", "country"] + (["currency"] if c.get("currency") else []))
        if kind == "capital":
            wrong = random.sample([x["capital"] for x in near], 3)
            q, opts, explain = f"What is the capital of {c['country']}?", [c["capital"]] + wrong, f"{c['capital']} is the capital of {c['country']}."
        elif kind == "country":
            wrong = random.sample([x["country"] for x in near], 3)
            q, opts, explain = f"{c['capital']} is the capital of which country?", [c["country"]] + wrong, f"{c['capital']} is the capital of {c['country']}."
        else:
            pool = list(dict.fromkeys(x["currency"] for x in near if x["currency"] != c["currency"]))  # neighbours first
            wrong = random.sample(pool, min(3, len(pool)))
            others = [x["currency"] for x in countries if x["currency"] not in wrong + [c["currency"]]]
            wrong += random.sample(sorted(set(others)), 3 - len(wrong))
            q, opts, explain = f"Which currency is used in {c['country']}?", [c["currency"]] + wrong, f"{c['country']} uses the {c['currency']}."
        wiki = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(c["country"].replace(" ", "_"))
        out.append(quiz_card("Geography", q, opts, 0, explain, {"name": "Wikipedia", "link": wiki}))
    return out


def history_quiz(n, log):
    today = NOW.astimezone(dt.timezone(dt.timedelta(hours=10)))
    try:
        events = wiki_json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{today:%m}/{today:%d}")["selected"]
    except Exception as ex:
        log.append(f"history quiz: failed ({str(ex)[:80]})")
        return []
    out = []
    for e in random.sample(events, min(n, len(events))):
        year, text = e["year"], PICTURED.sub("", e["text"])
        spread = 20 if year > 1900 else 60
        wrong = set()
        while len(wrong) < 3:
            y = year + random.randint(-spread, spread)
            if y != year and y <= today.year:
                wrong.add(y)
        link = e["pages"][0]["content_urls"]["desktop"]["page"] if e.get("pages") else None
        out.append(quiz_card("History", f"On this day ({today:%-d %B}), in which year did this happen? {text}",
                             [str(year)] + [str(y) for y in wrong], 0, f"It happened on {today:%-d %B} {year}.",
                             {"name": "Wikipedia", "link": link} if link else None))
    return out


CURRENT_QUIZ_PROMPT = """Write {n} multiple-choice quiz questions for a casual reader, testing today's news. Use ONLY facts stated
in the cards below; each question must be answerable from one card. Mix topics and countries. Keep questions under 25
words, make the 3 wrong options plausible but clearly wrong given the card. Return a JSON array of
{{"card_id": str, "question": str, "options": [4 strings], "answer": index 0-3 of the correct option, "explain": one short sentence}}.

Cards:
"""


def current_affairs_quiz(n, cards, log):
    if not GEMINI_KEY:
        return []
    news = [c for c in cards if c["rewritten"]]
    sample = random.sample(news, min(40, len(news)))
    by_id = {c["id"]: c for c in sample}
    listing = [{"id": c["id"], "headline": c["headline"], "summary": c["summary"]} for c in sample]
    try:
        items = gemini(CURRENT_QUIZ_PROMPT.format(n=n) + json.dumps(listing, ensure_ascii=False), GEMINI_KEY, MODELS, 0.4)
    except Exception as ex:
        log.append(f"current affairs quiz: failed ({str(ex)[:80]})")
        return []
    out = []
    for q in items:
        c, opts, ans = by_id.get(q.get("card_id")), q.get("options", []), q.get("answer")
        if c and len(opts) == 4 and isinstance(ans, int) and 0 <= ans < 4:
            out.append(quiz_card("Current affairs", q["question"], opts, ans, q.get("explain", ""), c["sources"][0]))
    return out[:n]


# ---------- select, write ----------

def select(stories):
    caps, size = CONFIG["section_caps"], CONFIG["feed_size"]
    stories.sort(key=lambda s: -s["score"])
    chosen, counts = [], {}
    for s in stories:
        if len(chosen) >= size["max"]:
            break
        if counts.get(s["section"], 0) < caps.get(s["section"], 3):
            chosen.append(s)
            counts[s["section"]] = counts.get(s["section"], 0) + 1
    for s in stories:  # thin news day: top up past the caps to reach the minimum
        if len(chosen) >= size["min"]:
            break
        if s not in chosen:
            chosen.append(s)
    random.shuffle(chosen)
    return chosen


OUTLET_LEAD = re.compile(r"^(?:[A-Z][\w.&'-]*(?: [A-Z][\w.&'-]*)*(?:,? (?:and )?)?){1,6} reported(?: on \w+)? that ")


def strip_outlet_lead(text):
    """Drop a leading "Reuters and BBC reported that" — the source chips already say it."""
    m = OUTLET_LEAD.match(text)
    if not m:
        return text
    rest = text[m.end():]
    return rest[:1].upper() + rest[1:]


def fallback_summary(s):
    text = max((m["desc"] for m in s["members"]), key=len)
    words = text.split()
    return " ".join(words[:60]) + ("…" if len(words) > 60 else "")


def card(s):
    ai, lead = s.get("ai"), s["lead"]
    sources, seen = [], set()
    for m in sorted(s["members"], key=lambda m: m["tier"] if m["tier"] else 9):
        if m["source"] not in seen:
            seen.add(m["source"])
            sources.append({"name": m["source"], "link": m.get("real_link") or m["link"]})
    return {
        "id": s["id"], "section": s["section"],
        "headline": ai["headline"] if ai else lead["title"],
        "summary": strip_outlet_lead(ai["summary"]) if ai else fallback_summary(s),
        "rewritten": bool(ai), "original_headline": lead["title"],
        "why": (ai.get("why") or "")[:1].upper() + (ai.get("why") or "")[1:] if ai else f"Covered by {len(sources)} source{'s' if len(sources) > 1 else ''}",
        "sources": sources, "image": next((m["image"] for m in s["members"] if m["image"]), None),
        "published": lead["published"], "score": round(s["score"], 2),
    }


def write(chosen, dropped, stats, log):
    OUT.mkdir(exist_ok=True)
    cards = [card(s) for s in chosen]
    extras = fun_facts(14, log) + geography_quiz(6) + history_quiz(5, log) + current_affairs_quiz(6, cards, log)
    log.append(f"extras: {len(extras)} fun facts and quiz cards")
    cards += extras
    random.shuffle(cards)
    feed = json.dumps({"generated_at": NOW.isoformat(), "cards": cards}, indent=1, ensure_ascii=False)
    (OUT / "feed.json").write_text(feed)
    (ROOT / "app" / "feed.json").write_text(feed)  # the app serves this copy

    lines = [f"# Calm News review — {NOW:%d %b %Y %H:%M} UTC", "", "## Run", *[f"- {x}" for x in stats + log], "",
             f"## Feed ({len(cards)} cards)", ""]
    for c in cards:
        if c.get("type") == "quiz":
            lines += [f"### [quiz/{c['kind']}] {c['question']}", " · ".join(
                f"**{o}**" if i == c["answer"] else o for i, o in enumerate(c["options"])), ""]
            continue
        lines += [f"### [{c['section']}] {c['headline']}",
                  f"{c['summary']}", "",
                  f"*{c['why']}* · " + ", ".join(f"[{x['name']}]({x['link']})" for x in c["sources"]),
                  *([f"Original headline: {c['original_headline']}"] if c["rewritten"] else []), ""]
    lines += ["## Dropped", ""]
    for s, why in sorted(dropped, key=lambda d: d[1]):
        lines.append(f"- [{s['section']}] {s['lead']['title']} — {s['lead']['source']} — *{why}*")
    (OUT / "review.md").write_text("\n".join(lines))
    return cards


def main():
    log = []
    articles, failures = fetch_all()
    stories = cluster(articles)
    kept, dropped = apply_rules(stories)

    # Only the strongest few per section reach the AI, to stay inside free limits
    per_section = {}
    for s in sorted(kept, key=lambda s: -s["prescore"]):
        per_section.setdefault(s["section"], []).append(s)
    candidates = [s for sec, lst in per_section.items() for s in lst[:int(CONFIG["section_caps"].get(sec, 3) * 2.5)]]
    enrich(candidates, log)
    scored, ai_dropped = ai_pass(candidates, log)
    chosen = select(dedupe(scored, log))

    stats = [f"{len(articles)} articles from {len(CONFIG['sources']) - len(failures)}/{len(CONFIG['sources'])} feeds",
             f"{len(stories)} stories after grouping, {len(kept)} passed rules, {len(candidates)} sent to scoring",
             *[f"feed failed: {f}" for f in failures]]
    cards = write(chosen, dropped + ai_dropped, stats, log)
    print("\n".join(stats + log))
    print(f"{len(cards)} cards → out/feed.json, review → out/review.md")


if __name__ == "__main__":
    main()
