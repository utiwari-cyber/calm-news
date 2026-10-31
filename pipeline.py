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
UA = "Mozilla/5.0 (CalmNews personal reader)"

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
    r"\bgossip\b", r"\bhow to\b", r"\bdeals?\b", r"\bsavings\b", r"pre-order", r"\bjobs? 20\d\d", r"recruitment",
    r"vacanc", r"admit card", r"\|\s*live\b", r"\beligibility\b", r"\bprice list\b", r"\bexplained\b", r"\bcheck (out )?(list|details)\b", r"\btrolled\b", r"\bweekend reads?\b", r"\bdeal(s)? of the day\b", r"\bbest .* to buy\b",
]
STOP = set("""a an the and or of to in on for at by with from as is are was were be been it its this that
these those after over into about than amid says said say new will would could may might not no up out
s has have had his her their they he she we you your our who what when where why how all more most""".split())


# ---------- fetch & parse ----------

def gn_url(query, section):
    hl, gl, ceid = ("en-IN", "IN", "IN:en") if section in ("india", "northeast", "lucknow") else ("en-AU", "AU", "AU:en")
    q = urllib.parse.quote(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
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
    if sec == "world" or src.get("dedicated"):
        return sec
    if re.search(CONFIG["section_keywords"][sec], f"{title} {desc[:300]}", re.I):
        return sec
    if sec in ("northeast", "lucknow"):  # local outlets also carry national news
        return "india" if re.search(CONFIG["section_keywords"]["india"], f"{title} {desc}", re.I) else "world"
    return "world"


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
        })
    return out


def fetch_all():
    articles, failures = [], []

    def one(src):
        url = gn_url(src["gn"], src["section"]) if "gn" in src else src["rss"]
        return parse_feed(src, fetch(url))

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(one, s): s for s in CONFIG["sources"]}
        for fut in cf.as_completed(futures):
            try:
                articles += fut.result()
            except Exception as ex:  # one bad feed never breaks the run
                failures.append(f"{futures[fut]['id']}: {str(ex)[:120]}")
    cutoff = NOW - dt.timedelta(hours=MAX_AGE_HOURS)
    fresh = [a for a in articles if not a["published"] or dt.datetime.fromisoformat(a["published"]) >= cutoff]
    seen, unique = set(), []
    for a in fresh:
        if a["link"] not in seen:
            seen.add(a["link"])
            unique.append(a)
    return unique, failures


# ---------- group same-story articles ----------

def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


def cluster(articles):
    articles.sort(key=lambda a: (a["tier"] if a["tier"] else 9, a["published"] or ""))
    clusters = []
    for a in articles:
        a["_tok"] = tokens(a["title"] + " " + a["desc"][:200])
        best, best_sim = None, 0.0
        for c in clusters:
            for m in c:
                shared = len(a["_tok"] & m["_tok"])
                sim = shared / (len(a["_tok"] | m["_tok"]) or 1)
                if shared >= 3 and sim > best_sim:
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

PROMPT = """You edit a calm, neutral news feed. For each story below return a JSON array, one object per story:
{"id": str,
 "scores": {"emotional": 0-5, "speculation": 0-5, "attribution": 0-5, "outrage": 0-5, "filler": 0-5},
 "impact": 0-5, "durability": 0-5,
 "headline": str, "summary": str, "why": str}

Scores (0 = clean, 5 = severe): emotional = words telling the reader how to feel; speculation = "could/set to/may"
carrying the story; attribution = claims with no named source; outrage = reactions instead of events;
filler = celebrity, viral posts, routine crime or accidents with no wider significance.
impact = how many people it affects; durability = will it still matter next month.

headline: max 12 words, present tense, no full stop, subject + verb + what happened, no questions,
no judging adjectives. Keep the key fact of the original headline (e.g. an FIR, an arrest, a number).
summary: 30-65 words. Order: what happened, who, where, when; then the key number or detail; then why it
matters ONLY if the input says so; then what is unknown ONLY if the input says so. Attribute claims to
the people or bodies making them ("the ministry said"). Never name the news outlet: the app shows sources
separately, so do not start with "Reuters reported that". STRICT: every fact must appear in the input. Never add context,
causes, consequences or background you were not given. No filler such as "this matters because",
"remains to be seen", "details were not provided". A short accurate summary beats a padded one.
If the input text is empty, write one sentence restating only what the headlines say.
Company-blog items: say "X announced".
why: under 12 words, why this is worth reading (e.g. "covered by 5 outlets; affects home loan rates").

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


def gemini(batch, key, models):
    """Try each model in turn; the free tier often answers 503 'high demand' for a while."""
    last = None
    for model in models:
        for wait in (0, 10, 30):
            time.sleep(wait)
            try:
                return gemini_call(batch, key, model)
            except urllib.error.HTTPError as ex:
                last = ex
                if ex.code not in (429, 500, 503):
                    break
            except (TimeoutError, urllib.error.URLError) as ex:
                last = ex
    raise last


def gemini_call(batch, key, model):
    payload = [{
        "id": s["id"], "section": s["section"],
        "sources": sorted({m["source"] for m in s["members"]}),
        "headlines": [m["title"] for m in s["members"][:4]],
        "text": max((m["desc"] for m in s["members"]), key=len)[:700],
    } for s in batch]
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT + json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=150) as r:
        data = json.loads(r.read())
    return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])


def ai_pass(candidates, log):
    key = os.environ.get("GEMINI_API_KEY")
    models = os.environ.get("GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash").split(",")
    cache = load_cache()
    todo = []
    for s in candidates:
        hit = next((cache[m["link"]] for m in s["members"] if m["link"] in cache), None)
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
                results = {r["id"]: r for r in gemini(batch, key, models)}
            except Exception as ex:
                log.append(f"AI: stopped after {i} stories ({str(ex)[:100]})")
                break
            for s in batch:
                r = results.get(s["id"])
                if r:
                    r["_at"] = NOW.isoformat()
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
            s["score"] = s["prescore"] + 1.5 * ai.get("impact", 2) + ai.get("durability", 2)
        else:
            s["score"] = s["prescore"]
        kept.append(s)
    return kept, dropped


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
            sources.append({"name": m["source"], "link": m["link"]})
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
    feed = json.dumps({"generated_at": NOW.isoformat(), "cards": cards}, indent=1, ensure_ascii=False)
    (OUT / "feed.json").write_text(feed)
    (ROOT / "app" / "feed.json").write_text(feed)  # the app serves this copy

    lines = [f"# Calm News review — {NOW:%d %b %Y %H:%M} UTC", "", "## Run", *[f"- {x}" for x in stats + log], "",
             f"## Feed ({len(cards)} cards)", ""]
    for c in cards:
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
    candidates = [s for sec, lst in per_section.items() for s in lst[:CONFIG["section_caps"].get(sec, 3) * 2]]
    scored, ai_dropped = ai_pass(candidates, log)
    chosen = select(scored)

    stats = [f"{len(articles)} articles from {len(CONFIG['sources']) - len(failures)}/{len(CONFIG['sources'])} feeds",
             f"{len(stories)} stories after grouping, {len(kept)} passed rules, {len(candidates)} sent to scoring",
             *[f"feed failed: {f}" for f in failures]]
    cards = write(chosen, dropped + ai_dropped, stats, log)
    print("\n".join(stats + log))
    print(f"{len(cards)} cards → out/feed.json, review → out/review.md")


if __name__ == "__main__":
    main()
