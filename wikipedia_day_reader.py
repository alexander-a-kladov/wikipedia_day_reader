#!/usr/bin/env python3
"""
Wikipedia Day Reader
Local:  pure-Python TF-IDF + keyword vocabulary (zero dependencies).
AI mode: optional external AI via free-tier APIs (Gemini, Groq, OpenRouter).
API keys stored in ai_keys.json next to this script.
"""

import html as html_module
import json
import math
import os
import queue
import re
import threading
import time
import webbrowser
from collections import Counter
from datetime import date
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error

# ── AI PROVIDERS ──────────────────────────────────────────────────────────────
# All listed providers have a FREE tier — no credit card required.

AI_PROVIDERS = {
    "gemini": {
        "name": "Google Gemini 2.0 Flash",
        "model": "gemini-2.0-flash",
        "free": True,
        "signup": "https://aistudio.google.com/apikey",
        "note": "Free: 15 req/min · 1 500 req/day",
    },
    "groq": {
        "name": "Groq — Llama 3.1 8B",
        "model": "llama-3.1-8b-instant",
        "free": True,
        "signup": "https://console.groq.com/keys",
        "note": "Free: 30 req/min · 14 400 req/day",
    },
    "openrouter": {
        "name": "OpenRouter (free models)",
        "model": "google/gemma-3-4b-it:free",   # updated default; user can override
        "free": True,
        "signup": "https://openrouter.ai/keys",
        "note": "Free models available — choose any :free model below",
        "model_override_key": "openrouter_model",   # key in _keys_store for custom model
    },
}

KEYS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_keys.json")
CACHE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
NOTES_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "notes")
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "images")


def load_keys() -> dict:
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ⚠ Cannot read {KEYS_FILE}: {e}")
        return {}


def save_keys(keys: dict):
    try:
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(keys, f, indent=2, ensure_ascii=False)
        print(f"  ✓ Keys saved to {KEYS_FILE}")
    except Exception as e:
        print(f"  ⚠ Cannot write {KEYS_FILE}: {e}")


# ── RESULT CACHE ──────────────────────────────────────────────────────────────

def _date_key(date_str: str) -> str:
    """Return MM-DD from a YYYY-MM-DD string (year ignored for file naming)."""
    parts = date_str.split("-")
    if len(parts) == 3:
        return f"{parts[1]}-{parts[2]}"
    return date_str  # fallback: return as-is


def _cache_path(date_str: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{_date_key(date_str)}.json")


def cache_save(date_str: str, payload: dict) -> str:
    """Save classification result to cache; return ISO timestamp."""
    import datetime as dt
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload["_saved_at"] = ts
    with open(_cache_path(date_str), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=None)
    return ts


def cache_load(date_str: str) -> dict | None:
    """Load cached result for date; return None if not found."""
    p = _cache_path(date_str)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_info(date_str: str) -> dict:
    """Return {exists, saved_at} for date."""
    p = _cache_path(date_str)
    if not os.path.exists(p):
        return {"exists": False, "saved_at": None}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {"exists": True, "saved_at": d.get("_saved_at")}
    except Exception:
        return {"exists": False, "saved_at": None}


# ── NOTES (per-entry annotations) ─────────────────────────────────────────────

def _notes_path(date_str: str) -> str:
    os.makedirs(NOTES_DIR, exist_ok=True)
    return os.path.join(NOTES_DIR, f"{_date_key(date_str)}.json")


def notes_load(date_str: str) -> dict:
    """Load notes dict {wiki_url: {text, images[]}} for date."""
    p = _notes_path(date_str)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def notes_save(date_str: str, notes: dict):
    with open(_notes_path(date_str), "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


# ── IMAGE STORAGE ─────────────────────────────────────────────────────────────

def image_save(date_str: str, wiki_key: str, filename: str, data: bytes) -> str:
    """Save image bytes, return relative URL path served by the app."""
    import hashlib
    os.makedirs(IMAGES_DIR, exist_ok=True)
    ext  = os.path.splitext(filename)[1].lower() or ".jpg"
    h    = hashlib.md5(data).hexdigest()[:10]
    name = f"{_date_key(date_str)}_{h}{ext}"
    path = os.path.join(IMAGES_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return f"/images/{name}"


def image_delete(url_path: str) -> bool:
    """Delete image by its /images/<name> path. Returns True if deleted."""
    name = url_path.lstrip("/images/").lstrip("images/")
    path = os.path.join(IMAGES_DIR, os.path.basename(name))
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _parse_retry_after(body: str, headers) -> float:
    """Extract wait seconds from Retry-After header or error message body."""
    # 1. Standard Retry-After header
    try:
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            return max(1.0, float(ra))
    except Exception:
        pass
    # 2. Groq / OpenAI style: "try again in 9.41s"
    m = re.search(r"try again in\s+([\d.]+)s", body, re.I)
    if m:
        return max(1.0, float(m.group(1))) + 0.5
    # 3. Fallback
    return 15.0


def _http_post(url: str, payload: dict, headers: dict,
               timeout: int = 40, max_retries: int = 4) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    full_headers = {"User-Agent": _UA, "Accept": "application/json", **headers}

    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data,
                                     headers=full_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                wait = _parse_retry_after(body, e.headers)
                if attempt < max_retries - 1:
                    print(f"  ⏳ Rate limit (429) — жду {wait:.1f}с (попытка {attempt+1}/{max_retries})…")
                    time.sleep(wait)
                    continue
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}")

    raise RuntimeError("Превышено максимальное число попыток (rate limit)")


# ── AI PROMPT / RESPONSE HELPERS ──────────────────────────────────────────────

def _ai_prompt(entries, categories, russian_desc, include_russian):
    cat_lines = "\n".join(
        f'  "{c["id"]}": {c["label"]} — {c["description"]}' for c in categories
    )
    if include_russian:
        cat_lines += f'\n  "russians": Русские/Советские — {russian_desc}'
    numbered = "\n".join(f"{i}: {strip_html(e)}" for i, e in enumerate(entries))
    system = (
        "You are a strict semantic classifier for historical Wikipedia entries.\n"
        "Return ONLY a JSON object mapping each entry index (string) to an array "
        "of matching category ids.\n"
        "Rules:\n"
        "- Match ONLY if the entry is DIRECTLY and PRIMARILY about that category.\n"
        "- Military battles, wars, conquests, politics, elections, treaties → NO science/art/literature.\n"
        "- Founding of a city, capture of territory, coronation → NO science/art/education.\n"
        "- A person publishing a scientific paper → science. Publishing a novel → literature.\n"
        "- Empty array [] when the entry does not clearly fit any category.\n"
        "- Do NOT assign a category just because a country or person name sounds related.\n"
        'Example: {"0":["science"],"1":[],"2":["art","education"],"3":[]}\n'
        "Return ONLY valid JSON, no markdown, no explanation."
    )
    user = f"Categories:\n{cat_lines}\n\nClassify each entry:\n{numbered}"
    return system, user


def _parse_ai_json(text: str, n: int) -> dict:
    text = re.sub(r"```[a-z]*\n?|\n?```", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {str(i): [] for i in range(n)}
    try:
        raw = json.loads(m.group())
        return {str(k): (v if isinstance(v, list) else []) for k, v in raw.items()}
    except Exception:
        return {str(i): [] for i in range(n)}


def _vocab_confirm(text_lower: str, cat_id: str) -> bool:
    """
    Secondary guard: confirm AI assignment has at least one vocabulary word match.
    'russians' is skipped (context-dependent names don't need keyword match).
    """
    if cat_id == "russians":
        return True
    kws = VOCAB.get(cat_id, set())
    if not kws:
        return True  # unknown cat — pass through
    words  = set(re.findall(r"[a-z']+", text_lower))
    tokens = re.findall(r"[a-z']+", text_lower)
    bgrams = {tokens[i] + " " + tokens[i+1] for i in range(len(tokens) - 1)}
    return bool((words | bgrams) & kws)


# ── PROVIDER IMPLEMENTATIONS ──────────────────────────────────────────────────

def _ai_gemini(entries, categories, russian_desc, include_russian, key, model):
    sys_p, usr_p = _ai_prompt(entries, categories, russian_desc, include_russian)
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    resp = _http_post(url, {
        "system_instruction": {"parts": [{"text": sys_p}]},
        "contents": [{"parts": [{"text": usr_p}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }, {"Content-Type": "application/json"})
    text = resp["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_ai_json(text, len(entries))


def _ai_groq(entries, categories, russian_desc, include_russian, key, model):
    sys_p, usr_p = _ai_prompt(entries, categories, russian_desc, include_russian)
    resp = _http_post("https://api.groq.com/openai/v1/chat/completions", {
        "model": model, "temperature": 0, "max_tokens": 2048,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user",   "content": usr_p}],
    }, {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    return _parse_ai_json(resp["choices"][0]["message"]["content"], len(entries))


def _ai_openrouter(entries, categories, russian_desc, include_russian, key, model):
    sys_p, usr_p = _ai_prompt(entries, categories, russian_desc, include_russian)
    resp = _http_post("https://openrouter.ai/api/v1/chat/completions", {
        "model": model, "temperature": 0, "max_tokens": 2048,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user",   "content": usr_p}],
    }, {"Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "http://localhost:8765",
        "X-Title": "WikiDayReader"})
    return _parse_ai_json(resp["choices"][0]["message"]["content"], len(entries))


_AI_FNS = {"gemini": _ai_gemini, "groq": _ai_groq, "openrouter": _ai_openrouter}

# Smaller batches for providers with tight token-per-minute limits
_AI_BATCH = {"gemini": 40, "groq": 15, "openrouter": 30}
AI_BATCH = 40  # default fallback


def classify_entries_ai(entries, categories, russian_desc, include_russian,
                         provider, key, model, progress_cb=None,
                         vocab_filter=False):
    """
    Classify using external AI.
    vocab_filter=True: reject AI assignments where zero VOCAB words match
    (catches hallucinations like military events → science).
    """
    fn = _AI_FNS.get(provider)
    if not fn:
        raise ValueError(f"Unknown provider: {provider}")
    result = {c["id"]: [] for c in categories}
    if include_russian:
        result["russians"] = []
    n = len(entries)
    batch_size = _AI_BATCH.get(provider, AI_BATCH)
    for start in range(0, n, batch_size):
        batch        = entries[start:start + batch_size]
        batch_texts  = [strip_html(e).lower() for e in batch]
        mapping      = fn(batch, categories, russian_desc, include_russian, key, model)
        for idx_s, cats in mapping.items():
            idx = int(idx_s)
            if idx >= len(batch):
                continue
            entry   = batch[idx]
            tlo     = batch_texts[idx]
            for cid in (cats or []):
                if cid not in result:
                    continue
                # Postfilter: skip if AI assigned but zero vocab words match
                if vocab_filter and not _vocab_confirm(tlo, cid):
                    continue
                result[cid].append(entry)
        if progress_cb:
            progress_cb(min(start + batch_size, n), n)
    return result

# ── PURE-PYTHON TF-IDF ───────────────────────────────────────────────────────

def tokenize(text):
    """Extract lowercase word tokens and bigrams."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    bigrams = [words[i] + "_" + words[i+1] for i in range(len(words) - 1)]
    return words + bigrams


def build_tfidf(corpus):
    """
    Fit TF-IDF on corpus (list of strings).
    Returns (idf_table, tokenized_corpus).
    """
    tokenized = [tokenize(doc) for doc in corpus]
    N = len(tokenized)
    df = Counter()
    for tokens in tokenized:
        for t in set(tokens):
            df[t] += 1
    # Smooth IDF: log((N+1)/(df+1)) + 1
    idf = {t: math.log((N + 1) / (c + 1)) + 1 for t, c in df.items()}
    return idf, tokenized


def make_vec(tokens, idf):
    """Build L2-normalised TF-IDF vector as a dict."""
    tf = Counter(tokens)
    total = len(tokens) or 1
    vec = {}
    for t, count in tf.items():
        w = (count / total) * idf.get(t, 1.0)
        if w > 0:
            vec[t] = w
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def cosine(a, b):
    """Cosine similarity between two sparse vectors (dicts)."""
    if len(a) > len(b):
        a, b = b, a
    return sum(a[t] * b[t] for t in a if t in b)


# ── KEYWORD VOCABULARIES ─────────────────────────────────────────────────────
# Large synonym sets for robust matching without any ML library.

VOCAB = {
    # event categories
    "science": {
        "physicist","chemist","biologist","mathematician","astronomer","geologist",
        "physician","doctor","scientist","engineer","researcher","discoverer",
        "zoologist","neuroscientist","botanist","oceanographer","paleontologist",
        "meteorologist","discovery","experiment","theory","research","laboratory",
        "nuclear","atom","atomic","dna","rna","genome","gene","vaccine","vaccination",
        "space","satellite","telescope","observatory","microscope","x-ray","laser",
        "electricity","electromagnetism","thermodynamics","quantum","relativity",
        "evolution","genetics","bacteria","virus","antibiotic","surgery","calculus",
        "algebra","geometry","computer","computing","algorithm","robotics",
        "chemistry","physics","biology","mathematics","medicine","scientific",
        "astronomical","geological","pharmaceutical","epidemiological","clinical",
        "natural knowledge","natural philosophy","learned society","royal society",
        "akademie","académie","academia","scientific society","observatory founded",
        "discovers","discovered","isolates","isolated","synthesizes","synthesized",
        "invents","invented","patent granted","first described","first observed",
    },
    "art": {
        "painter","sculptor","artist","illustrator","filmmaker","director",
        "choreographer","architect","architecture","photographer","printmaker",
        "ceramist","engraver","muralist","watercolorist","caricaturist","opera",
        "ballet","symphony","concerto","concert","theatre","theater","exhibition",
        "premiere","canvas","mural","fresco","painting","sculpture","performance",
        "cinema","film","movie","animation","gallery","museum","artwork","masterpiece",
        "portrait","landscape","abstract","installation","choreography","dance",
    },
    "education": {
        "university","universities","college","colleges","school","schools","academy",
        "academies","library","libraries","seminary","polytechnic","faculty","campus",
        "founded","established","institute","institution","lyceum","conservatory",
        "conservatoire","kindergarten",
    },
    "literature": {
        "author","writer","poet","novelist","playwright","essayist","biographer",
        "poem","poems","poetry","novel","novels","story","stories","tale","tales",
        "essay","essays","book","books","published","literary","fiction","nonfiction",
        "prose","verse","drama","script","memoir","autobiography","anthology",
        "literature","journalist","chronicle","fable","satire","sonnet","ode",
        "magazine","newspaper","editorial","reporter",
        "playwright","tragedian","lyricist","librettist","screenwriter","diarist",
    },
    # birth categories
    "scientists": {
        "physicist","chemist","biologist","mathematician","astronomer","geologist",
        "physician","doctor","scientist","engineer","researcher","neuroscientist",
        "botanist","zoologist","oceanographer","paleontologist","meteorologist",
        "statistician","crystallographer","biochemist","immunologist","epidemiologist",
        "archaeologist","anthropologist","psychologist","pharmacologist","pathologist",
        "geneticist","cytologist","virologist","computer scientist","astrophysicist",
        "cosmologist","entomologist","ornithologist","mycologist","taxonomist",
    },
    "artists": {
        "painter","sculptor","artist","illustrator","photographer","printmaker",
        "ceramist","engraver","muralist","watercolorist","draughtsman","caricaturist",
        "animator","miniaturist","lithographer","mosaic","glassblower","colorist",
        "graphic designer","visual artist","performance artist","installation artist",
    },
    "composers": {
        "composer","pianist","violinist","cellist","guitarist","conductor","organist",
        "soprano","mezzo-soprano","contralto","tenor","baritone","bass","singer",
        "musician","songwriter","instrumentalist","lutenist","harpsichordist",
        "flautist","oboist","clarinetist","bassoonist","trumpeter","trombonist",
        "percussionist","drummer","bandleader","arranger","choirmaster","cantor",
    },
    "inventors": {
        "inventor","inventors","invention","inventions","patent","patents",
        "engineer","engineers","engineering","technologist","pioneer","pioneers",
        "industrialist","entrepreneur","mechanic","machinist","aviation","locomotive",
        "automobile","telephone","telegraph","radio","television","printing","steam",
        "turbine","aerospace","electrical engineer","civil engineer",
        "mechanical engineer","chemical engineer",
    },
    # russian detection
    "russians": {
        "russian","russians","soviet","russia","ussr","moscow","saint petersburg",
        "leningrad","petrograd","stalingrad","kyiv","kiev","ukraine","ukrainian",
        "belarus","belarusian","byelorussian","georgian","armenian","azerbaijani",
        "kazakh","uzbek","tajik","turkmen","kirghiz","moldovan","latvian","estonian",
        "lithuanian","romanov","bolshevik","tsarist","cossack","siberian",
        "russian empire","red army","communist","kremlin","imperial russia",
    },
}


def keyword_hits(text_lower, cat_id):
    """Count vocabulary word matches in text (whole-word and bigram)."""
    kws = VOCAB.get(cat_id, set())
    if not kws:
        return 0
    words = set(re.findall(r"[a-z']+", text_lower))
    toks  = re.findall(r"[a-z']+", text_lower)
    bgrams = {toks[i] + " " + toks[i+1] for i in range(len(toks) - 1)}
    return len((words | bgrams) & kws)


# ── CLASSIFIER ───────────────────────────────────────────────────────────────

def classify_entries(entries, categories, russian_desc,
                     include_russian, tfidf_thr, kw_min, progress_cb=None):
    """
    Hybrid: TF-IDF cosine similarity + keyword vocabulary.
    Entry is accepted if EITHER signal meets the threshold.
    Returns {cat_id: [html_entry, ...]}.
    """
    cat_ids   = [c["id"]          for c in categories]
    cat_descs = [c["description"] for c in categories]
    if include_russian:
        cat_ids.append("russians")
        cat_descs.append(russian_desc)

    entry_texts = [strip_html(e) for e in entries]

    # Fit TF-IDF on descriptions + entries together
    corpus = cat_descs + entry_texts
    idf, tokenized = build_tfidf(corpus)

    cat_vecs   = [make_vec(tokenized[i],                    idf) for i in range(len(cat_descs))]
    entry_vecs = [make_vec(tokenized[len(cat_descs) + i],   idf) for i in range(len(entry_texts))]

    result = {cid: [] for cid in cat_ids}
    n = len(entries)

    for i, (entry, text) in enumerate(zip(entries, entry_texts)):
        tlo = text.lower()
        for k, cid in enumerate(cat_ids):
            sim  = cosine(entry_vecs[i], cat_vecs[k])
            hits = keyword_hits(tlo, cid)
            if sim >= tfidf_thr or hits >= kw_min:
                result[cid].append(entry)
        if progress_cb and (i + 1) % 20 == 0:
            progress_cb(i + 1, n)

    if progress_cb:
        progress_cb(n, n)

    return result


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ── DEFAULT CONFIG ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "event_categories": [
        {
            "id": "science", "label": "Наука",
            "description": (
                "scientific discovery experiment physics chemistry biology medicine "
                "astronomy mathematics space nuclear atom DNA vaccine research "
                "laboratory theory invention technology computing engineering"
            ),
        },
        {
            "id": "art", "label": "Искусство",
            "description": (
                "painting sculpture opera ballet symphony film cinema theatre museum "
                "gallery exhibition architecture photography concert performance premiere "
                "artist painter sculptor composer choreographer"
            ),
        },
        {
            "id": "education", "label": "Образование",
            "description": (
                "university college school academy library institute founded established "
                "education faculty campus conservatory seminary polytechnic"
            ),
        },
        {
            "id": "literature", "label": "Литература",
            "description": (
                "author writer poet novelist playwright poem novel book published "
                "literature essay biography fiction prose drama story literary"
            ),
        },
    ],
    "birth_categories": [
        {
            "id": "scientists", "label": "Учёные",
            "description": (
                "physicist chemist biologist mathematician astronomer geologist "
                "physician doctor scientist researcher neuroscientist computer scientist "
                "archaeologist psychologist pharmacologist"
            ),
        },
        {
            "id": "artists", "label": "Художники",
            "description": (
                "painter sculptor artist illustrator photographer printmaker "
                "ceramist engraver muralist visual artist graphic designer"
            ),
        },
        {
            "id": "composers", "label": "Композиторы",
            "description": (
                "composer musician pianist violinist conductor opera singer songwriter "
                "cellist guitarist organist instrumentalist choirmaster"
            ),
        },
        {
            "id": "inventors", "label": "Изобретатели",
            "description": (
                "inventor engineer industrialist technologist pioneer patent "
                "entrepreneur mechanical electrical aerospace locomotive automobile"
            ),
        },
    ],
    "russian_description": (
        "Russian Soviet Ukraine Belarus Georgia Armenia Azerbaijan Kazakhstan "
        "Russian Empire USSR Moscow Saint Petersburg Leningrad Romanov Bolshevik "
        "Siberia Cossack Kremlin imperial Russia"
    ),
    "tfidf_threshold": 0.07,
    "keyword_min_hits": 1,
}


# ── WIKIPEDIA ─────────────────────────────────────────────────────────────────

def fetch_wikipedia(title):
    url = f"https://en.wikipedia.org/wiki/{title}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 WikiDayReader/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8"), url
    except Exception as e:
        return None, str(e)


def parse_wikipedia_raw(content):
    class WP(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_ev = self.in_bi = self.in_h2 = self.in_li = False
            self.events = []; self.births = []
            self.li_html = ""; self.h2t = ""
            self.skip = {"sup", "style", "script"}; self.sd = 0

        def handle_starttag(self, tag, attrs):
            ad = dict(attrs)
            if tag in self.skip: self.sd += 1; return
            if self.sd: return
            if tag == "h2": self.in_h2 = True; self.h2t = ""
            elif tag == "li" and (self.in_ev or self.in_bi):
                self.in_li = True; self.li_html = ""
            elif tag == "a" and self.in_li:
                href = ad.get("href", "")
                if href.startswith("/wiki/"):
                    href = "https://en.wikipedia.org" + href
                self.li_html += f'<a href="{href}" target="_blank">'

        def handle_endtag(self, tag):
            if tag in self.skip: self.sd = max(0, self.sd - 1); return
            if self.sd: return
            if tag == "h2":
                t = self.h2t.strip().lower()
                self.in_ev = "event" in t
                self.in_bi = "birth" in t
                self.in_h2 = False
            elif tag == "li" and self.in_li:
                e = self.li_html.strip()
                if e:
                    (self.events if self.in_ev else self.births).append(e)
                self.in_li = False
            elif tag == "a" and self.in_li:
                self.li_html += "</a>"

        def handle_data(self, data):
            if self.sd: return
            if self.in_h2: self.h2t += data
            elif self.in_li: self.li_html += html_module.escape(data)

    p = WP()
    try: p.feed(content)
    except Exception: pass
    return p.events, p.births


# ── JOB STATE ─────────────────────────────────────────────────────────────────

_job_queue: queue.Queue = queue.Queue()
config_store = dict(DEFAULT_CONFIG)
_keys_store:  dict = {}   # loaded at startup


def run_job(events_raw, births_raw, cfg,
            use_ai_events=False, use_ai_births=False, ai_provider="gemini"):
    _job_queue.queue.clear()

    def push(obj):
        _job_queue.put(json.dumps(obj, ensure_ascii=False))

    def _get_ai(provider):
        key   = _keys_store.get(provider, "").strip()
        # Allow per-provider model override stored in keys file
        override_key = AI_PROVIDERS.get(provider, {}).get("model_override_key")
        model = (_keys_store.get(override_key, "").strip()
                 or AI_PROVIDERS[provider]["model"])
        if not key:
            raise ValueError(
                f"API key for '{AI_PROVIDERS[provider]['name']}' not set. "
                f"Add it in ⚙ Settings → AI."
            )
        return key, model

    def run():
        try:
            thr   = float(cfg.get("tfidf_threshold",  DEFAULT_CONFIG["tfidf_threshold"]))
            kwmin = int(cfg.get("keyword_min_hits",    DEFAULT_CONFIG["keyword_min_hits"]))
            pname = AI_PROVIDERS.get(ai_provider, {}).get("name", ai_provider)

            # ── Events ───────────────────────────────────────────────────
            if use_ai_events:
                key, model = _get_ai(ai_provider)
                push({"type": "status",
                      "text": f"AI события ({pname})…"})
                ev_res = classify_entries_ai(
                    events_raw, cfg["event_categories"],
                    cfg["russian_description"], False,
                    ai_provider, key, model,
                    progress_cb=lambda d, t: push({
                        "type": "progress",
                        "pct": int(d / max(t, 1) * 50),
                        "text": f"AI события: {d}/{t}",
                    })
                )
            else:
                push({"type": "status", "text": "Классифицирую события (локально)…"})
                ev_res = classify_entries(
                    events_raw, cfg["event_categories"],
                    cfg["russian_description"], False, thr, kwmin,
                    progress_cb=lambda d, t: push({
                        "type": "progress",
                        "pct": int(d / max(t, 1) * 50),
                        "text": f"События: {d} из {t}",
                    })
                )

            # ── Births ────────────────────────────────────────────────────
            if use_ai_births:
                key, model = _get_ai(ai_provider)
                push({"type": "status",
                      "text": f"AI рождения ({pname})…"})
                bi_res = classify_entries_ai(
                    births_raw, cfg["birth_categories"],
                    cfg["russian_description"], True,
                    ai_provider, key, model,
                    progress_cb=lambda d, t: push({
                        "type": "progress",
                        "pct": 50 + int(d / max(t, 1) * 50),
                        "text": f"AI рождения: {d}/{t}",
                    })
                )
            else:
                push({"type": "status", "text": "Классифицирую рождения (локально)…"})
                bi_res = classify_entries(
                    births_raw, cfg["birth_categories"],
                    cfg["russian_description"], True, thr, kwmin,
                    progress_cb=lambda d, t: push({
                        "type": "progress",
                        "pct": 50 + int(d / max(t, 1) * 50),
                        "text": f"Рождения: {d} из {t}",
                    })
                )

            push({"type": "done", "result": {
                "events": [
                    {"id": c["id"], "label": c["label"],
                     "entries": ev_res.get(c["id"], [])}
                    for c in cfg["event_categories"]
                ],
                "births": [
                    {"id": c["id"], "label": c["label"],
                     "entries": bi_res.get(c["id"], [])}
                    for c in cfg["birth_categories"]
                ],
                "births_russian": bi_res.get("russians", []),
            }})
        except Exception as ex:
            push({"type": "error", "text": str(ex)})

    threading.Thread(target=run, daemon=True).start()


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Читатель Дней Истории</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=Source+Serif+4:opsz,wght@8..60,300;8..60,400&display=swap" rel="stylesheet">
<style>
  :root{--bg:#faf8f4;--sf:#fff;--sf2:#f5f2ec;--ac:#7c4f2a;--ac2:#c17f3b;--tx:#1e1a14;--tx2:#5c5244;--bd:#ddd8ce;--ru:#8b2d2d;--r:6px;--ai:#1a5c8a;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Source Serif 4',Georgia,serif;background:var(--bg);color:var(--tx);min-height:100vh;}
  header{background:var(--ac);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:14px;border-bottom:3px solid var(--ac2);}
  header h1{font-family:'Playfair Display',serif;font-size:20px;font-weight:600;}
  .sub{font-size:11px;opacity:.75;margin-top:2px;}
  .ctrl{background:var(--sf);border-bottom:1px solid var(--bd);padding:10px 28px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
  input[type=date]{font-family:'Source Serif 4',serif;font-size:15px;padding:5px 10px;border:1.5px solid var(--bd);border-radius:var(--r);background:var(--bg);color:var(--tx);}
  button{font-family:'Source Serif 4',serif;font-size:13px;padding:6px 15px;border-radius:var(--r);border:1.5px solid;cursor:pointer;transition:all .15s;}
  .bp{background:var(--ac);border-color:var(--ac);color:#fff;}
  .bp:hover{background:var(--ac2);border-color:var(--ac2);}
  .bp:disabled{opacity:.45;cursor:not-allowed;}
  .bs{background:transparent;border-color:var(--bd);color:var(--tx2);}
  .bs:hover{border-color:var(--ac2);color:var(--ac);}
  .bsave{background:transparent;border-color:#5a8a5a;color:#2d6b2d;}
  .bsave:hover{background:#edf7ed;border-color:#2d6b2d;}
  .bsave:disabled{opacity:.45;cursor:not-allowed;}
  .bload{background:transparent;border-color:#5a7a8a;color:#1e5070;}
  .bload:hover{background:#e8f2f8;border-color:#1e5070;}
  /* saved badge */
  .saved-badge{display:none;align-items:center;gap:5px;font-size:12px;
    padding:4px 10px;border-radius:var(--r);background:#edf7ed;
    border:1.5px solid #7ab88a;color:#1a5c2a;white-space:nowrap;}
  .saved-badge.visible{display:flex;}
  .saved-badge .sb-icon{font-size:13px;}
  .saved-badge .sb-date{font-weight:600;}
  .saved-badge .sb-time{opacity:.75;}
  /* AI toggle strip */
  .ai-strip{display:flex;align-items:center;gap:7px;padding:5px 11px;border-radius:var(--r);background:var(--sf2);border:1.5px solid var(--bd);transition:all .2s;}
  .ai-strip.active{background:#e8f2fb;border-color:#8ab8d8;}
  .ai-strip label{font-size:13px;color:var(--tx2);cursor:pointer;user-select:none;}
  .ai-strip.active label{color:var(--ai);font-weight:600;}
  .ai-sep{width:1px;height:20px;background:var(--bd);margin:0 2px;}
  input[type=checkbox]{width:14px;height:14px;cursor:pointer;accent-color:var(--ai);}
  select{font-family:'Source Serif 4',serif;font-size:13px;padding:3px 7px;border:1.5px solid var(--bd);border-radius:var(--r);background:var(--sf);color:var(--tx);cursor:pointer;}
  select:focus{outline:none;border-color:#8ab8d8;}
  .main{display:grid;grid-template-columns:242px 1fr;min-height:calc(100vh - 108px);}
  .sidebar{background:var(--sf);border-right:1px solid var(--bd);padding:14px 12px;overflow-y:auto;}
  .stitle{font-family:'Playfair Display',serif;font-size:11px;font-weight:600;color:var(--tx2);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid var(--bd);}
  .ssec{margin-bottom:18px;}
  .ci{display:flex;align-items:center;gap:7px;padding:4px 5px;border-radius:4px;margin-bottom:2px;cursor:pointer;transition:background .1s;}
  .ci:hover{background:var(--sf2);}
  .cdot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
  .clbl{font-size:13px;flex:1;}
  .ccnt{font-size:11px;color:var(--tx2);background:var(--sf2);padding:1px 5px;border-radius:9px;min-width:18px;text-align:center;}
  .ctgl{width:13px;height:13px;border:1.5px solid var(--bd);border-radius:3px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;}
  .ctgl.on{background:var(--ac);border-color:var(--ac);color:#fff;}
  .content{padding:22px 30px;overflow-y:auto;}
  .welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:360px;text-align:center;color:var(--tx2);}
  .wi{font-size:44px;margin-bottom:12px;}
  .welcome h2{font-family:'Playfair Display',serif;font-size:20px;color:var(--ac);margin-bottom:7px;}
  .welcome p{line-height:1.7;font-size:14px;}
  @keyframes spin{to{transform:rotate(360deg)}}
  .sp{width:16px;height:16px;border:2px solid var(--bd);border-top-color:var(--ac);border-radius:50%;animation:spin .7s linear infinite;display:inline-block;vertical-align:middle;margin-right:5px;}
  .pw{padding:18px 0 8px;}
  .pl{font-size:14px;color:var(--tx2);margin-bottom:7px;}
  .pt{height:5px;background:var(--sf2);border-radius:3px;overflow:hidden;margin-bottom:5px;}
  .pf{height:100%;background:var(--ac2);border-radius:3px;transition:width .35s ease;}
  .pd{font-size:11px;color:var(--tx2);}
  .sb{margin-bottom:24px;}
  .sh{display:flex;align-items:center;gap:9px;margin-bottom:10px;padding-bottom:7px;border-bottom:2px solid;}
  .si{width:23px;height:23px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;flex-shrink:0;}
  .st{font-family:'Playfair Display',serif;font-size:16px;font-weight:600;}
  .ss{font-size:11px;color:var(--tx2);margin-left:auto;}
  .entry{padding:6px 10px;border-left:3px solid transparent;margin-bottom:2px;border-radius:0 4px 4px 0;font-size:14px;line-height:1.6;transition:background .1s;}
  .entry:hover{background:var(--sf2);}
  .entry:hover .note-btn{opacity:1;}
  .entry a{color:var(--ac);text-decoration:none;}
  .entry a:hover{text-decoration:underline;}
  /* note button on entry */
  .note-btn{opacity:0;transition:opacity .15s;border:none;background:none;cursor:pointer;
    font-size:13px;padding:1px 4px;border-radius:3px;margin-left:4px;vertical-align:middle;color:var(--tx2);}
  .note-btn:hover{background:var(--sf2);color:var(--ac);}
  .note-btn.has-note{opacity:1;color:var(--ac2);}
  /* note post preview under entry */
  .note-post{margin:4px 10px 8px 14px;border:1.5px solid var(--bd);border-radius:6px;
    overflow:hidden;background:var(--sf);}
  .note-post-text{padding:8px 12px;font-size:13px;line-height:1.6;color:var(--tx);}
  .note-post-text a{color:var(--ac);}
  .note-post-imgs{display:flex;flex-wrap:wrap;gap:6px;padding:0 10px 10px;}
  .note-post-imgs img{height:90px;width:auto;border-radius:4px;object-fit:cover;cursor:pointer;
    border:1.5px solid var(--bd);}
  /* note editor modal */
  .note-modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;
    align-items:center;justify-content:center;z-index:200;}
  .note-modal.open{display:flex;}
  .note-box{background:var(--sf);border-radius:10px;padding:22px;width:640px;
    max-width:96vw;max-height:90vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,.25);}
  .note-box h3{font-family:'Playfair Display',serif;font-size:16px;color:var(--ac);margin-bottom:4px;}
  .note-source{font-size:12px;color:var(--tx2);margin-bottom:12px;word-break:break-all;}
  .note-source a{color:var(--ai);}
  /* note editor - contenteditable */
  .note-editor{width:100%;min-height:110px;font-family:'Source Serif 4',serif;font-size:14px;
    padding:9px;border:1.5px solid var(--bd);border-radius:6px;
    background:var(--bg);color:var(--tx);line-height:1.6;outline:none;
    cursor:text;}
  .note-editor:focus{border-color:var(--ac2);}
  .note-editor a{color:var(--ac);text-decoration:underline;}
  .note-editor:empty:before{content:attr(placeholder);color:#aaa;pointer-events:none;}
  /* editor toolbar */
  .note-toolbar{display:flex;gap:5px;margin-bottom:5px;flex-wrap:wrap;}
  .note-toolbar button{font-size:12px;padding:3px 9px;border:1px solid var(--bd);
    border-radius:4px;background:var(--sf);color:var(--tx2);cursor:pointer;
    font-family:'Source Serif 4',serif;}
  .note-toolbar button:hover{background:var(--sf2);border-color:var(--ac2);color:var(--ac);}
  /* link insert popup */
  .link-popup{position:fixed;z-index:250;background:var(--sf);border:1.5px solid var(--bd);
    border-radius:8px;padding:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);width:340px;
    display:none;}
  .link-popup.open{display:block;}
  .link-popup label{font-size:12px;color:var(--tx2);display:block;margin-bottom:3px;}
  .link-popup input{width:100%;font-size:13px;padding:5px 8px;border:1px solid var(--bd);
    border-radius:4px;background:var(--bg);font-family:'Source Serif 4',serif;
    margin-bottom:8px;}
  .link-popup-btns{display:flex;gap:7px;justify-content:flex-end;}
  .note-imgs-wrap{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px;}
  .note-img-thumb{position:relative;display:inline-block;}
  .note-img-thumb img{height:240px;width:auto;max-width:100%;border-radius:6px;object-fit:cover;
    border:1.5px solid var(--bd);cursor:pointer;}
  .note-img-thumb img:hover{border-color:var(--ac2);}
  .note-img-del{position:absolute;top:-7px;right:-7px;width:22px;height:22px;
    border-radius:50%;background:#c0392b;color:#fff;border:none;cursor:pointer;
    font-size:12px;display:flex;align-items:center;justify-content:center;line-height:1;}
  .note-img-del:hover{background:#a02020;}
  .note-upload-btn{font-size:12px;padding:5px 12px;color:var(--tx2);border-color:var(--bd);
    cursor:pointer;display:inline-flex;align-items:center;gap:5px;margin-bottom:10px;}
  .note-upload-btn:hover{border-color:var(--ac2);color:var(--ac);}
  .note-actions{display:flex;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--bd);}
  .note-actions .spacer{flex:1;}
  /* publication status block */
  .pub-block{margin-top:10px;padding:10px 12px;background:var(--sf2);border-radius:6px;
    border:1px solid var(--bd);}
  .pub-status-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
  .pub-status-row label{font-size:12px;color:var(--tx2);white-space:nowrap;}
  .pub-status-btns{display:flex;gap:0;border:1.5px solid var(--bd);border-radius:var(--r);overflow:hidden;}
  .pub-status-btns button{font-family:'Source Serif 4',serif;font-size:12px;padding:5px 12px;
    border:none;border-right:1px solid var(--bd);background:var(--sf);color:var(--tx2);
    cursor:pointer;transition:all .15s;white-space:nowrap;}
  .pub-status-btns button:last-child{border-right:none;}
  .pub-status-btns button:hover{background:var(--sf2);}
  .pub-status-btns button.active-draft{background:#f5f2ec;color:#5c5244;font-weight:600;}
  .pub-status-btns button.active-ready{background:#fff8e0;color:#7a5a00;font-weight:600;}
  .pub-status-btns button.active-published{background:#edf7ed;color:#1a5c2a;font-weight:600;}
  .pub-date-url{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px;}
  .pub-date-url label{font-size:12px;color:var(--tx2);white-space:nowrap;}
  .pub-date-url input[type=date],.pub-date-url input[type=url]{font-family:'Source Serif 4',serif;
    font-size:13px;padding:4px 8px;border:1px solid var(--bd);border-radius:4px;
    background:var(--sf);color:var(--tx);}
  .pub-date-url input[type=date]{width:150px;}
  .pub-date-url input[type=url]{flex:1;min-width:180px;}
  /* status badges on post */
  .note-pub-badge{display:flex;align-items:center;gap:6px;padding:5px 12px;
    font-size:12px;color:var(--tx2);border-top:1px solid var(--bd);}
  .note-pub-badge a{color:var(--ai);text-decoration:none;}
  .note-pub-badge a:hover{text-decoration:underline;}
  .status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
  .status-dot.draft{background:#aaa;}
  .status-dot.ready{background:#c8a800;}
  .status-dot.published{background:#5a8a5a;}
  /* image lightbox */
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;
    align-items:center;justify-content:center;z-index:300;cursor:zoom-out;}
  .lightbox.open{display:flex;}
  .lightbox img{max-width:92vw;max-height:90vh;border-radius:6px;box-shadow:0 4px 30px rgba(0,0,0,.6);}
  .rb{display:inline-block;font-size:9px;padding:1px 5px;background:#fce8e8;color:var(--ru);border-radius:9px;border:1px solid #f5c0c0;margin-left:5px;vertical-align:middle;}
  .empty{color:var(--tx2);font-style:italic;font-size:13px;padding:5px 10px;}
  .ptitle{font-family:'Playfair Display',serif;font-size:23px;color:var(--ac);margin-bottom:3px;}
  .pmeta{font-size:12px;color:var(--tx2);margin-bottom:17px;}
  .tabs{display:flex;gap:4px;margin-bottom:18px;}
  .tab{padding:5px 14px;font-size:14px;cursor:pointer;border-radius:var(--r);border:1.5px solid var(--bd);color:var(--tx2);font-family:'Source Serif 4',serif;transition:all .15s;background:var(--sf);}
  .tab.active{background:var(--ac);border-color:var(--ac);color:#fff;}
  .tc{display:none;} .tc.active{display:block;}
  .err{background:#fce8e8;color:#8b2d2d;padding:10px 14px;border-radius:6px;font-size:14px;border:1px solid #f5c0c0;margin-bottom:12px;}
  /* Modal */
  .mo{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;align-items:center;justify-content:center;z-index:100;}
  .mo.open{display:flex;}
  .md{background:var(--sf);border-radius:10px;padding:22px;width:700px;max-width:95vw;max-height:88vh;overflow-y:auto;box-shadow:0 10px 36px rgba(0,0,0,.22);}
  .md h2{font-family:'Playfair Display',serif;font-size:18px;margin-bottom:12px;color:var(--ac);}
  .mtabs{display:flex;gap:2px;margin-bottom:14px;border-bottom:1px solid var(--bd);}
  .mtab{padding:5px 13px 7px;font-size:12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;color:var(--tx2);}
  .mtab.active{border-bottom-color:var(--ac);color:var(--ac);font-weight:600;}
  .ce{display:none;} .ce.active{display:block;}
  .cr{display:flex;align-items:center;gap:7px;padding:8px;border:1px solid var(--bd);border-radius:6px;margin-bottom:6px;background:var(--bg);}
  .cr input{font-family:'Source Serif 4',serif;font-size:13px;padding:4px 7px;border:1px solid var(--bd);border-radius:4px;background:var(--sf);}
  .li{width:115px;flex-shrink:0;}
  .di{flex:1;}
  .dbtn{padding:4px 7px;font-size:11px;color:#c0392b;border-color:#f5c0c0;background:#fce8e8;flex-shrink:0;}
  .ma{display:flex;gap:8px;justify-content:flex-end;margin-top:15px;padding-top:12px;border-top:1px solid var(--bd);}
  .hint{font-size:12px;color:var(--tx2);margin-bottom:8px;line-height:1.5;}
  textarea{font-family:'Source Serif 4',serif;font-size:13px;padding:7px;border:1px solid var(--bd);border-radius:4px;resize:vertical;width:100%;}
  .srow{display:flex;align-items:center;gap:10px;margin:10px 0;}
  .srow label{font-size:13px;color:var(--tx2);width:200px;flex-shrink:0;}
  input[type=range]{flex:1;}
  .sval{font-size:13px;font-weight:600;color:var(--ac);width:32px;text-align:right;}
  /* AI settings tab */
  .provider-card{border:1.5px solid var(--bd);border-radius:8px;padding:14px;margin-bottom:12px;background:var(--bg);}
  .provider-card.has-key{border-color:#7ab88a;background:#f0faf2;}
  .prov-header{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
  .prov-name{font-size:14px;font-weight:600;color:var(--tx);}
  .prov-free{font-size:10px;padding:1px 6px;background:#d4edda;color:#1a6b2a;border-radius:9px;border:1px solid #a3d4ad;}
  .prov-note{font-size:11px;color:var(--tx2);margin-bottom:8px;}
  .prov-key-row{display:flex;gap:7px;align-items:center;}
  .prov-key-row input{flex:1;font-family:monospace;font-size:12px;padding:5px 8px;border:1px solid var(--bd);border-radius:4px;background:var(--sf);}
  .prov-key-row a{font-size:11px;color:var(--ai);white-space:nowrap;text-decoration:none;}
  .prov-key-row a:hover{text-decoration:underline;}
  .key-status{font-size:10px;margin-left:auto;padding:1px 7px;border-radius:9px;}
  .key-status.set{background:#d4edda;color:#1a6b2a;border:1px solid #a3d4ad;}
  .key-status.unset{background:#f5f2ec;color:var(--tx2);border:1px solid var(--bd);}
</style>
</head>
<body>
<script>
// Set today's date immediately so calendar isn't blank before init() runs
(function(){
  const d=new Date();
  const s=d.toISOString().split('T')[0];
  document.addEventListener('DOMContentLoaded',function(){
    const dp=document.getElementById('datePicker');
    if(dp&&!dp.value) dp.value=s;
  });
})();
</script>
<header>
  <div>
    <h1>📖 Читатель Дней Истории</h1>
    <div class="sub" id="hdrSub">Локальная классификация · TF-IDF + словарь · Wikipedia</div>
  </div>
</header>
<div class="ctrl">
  <input type="date" id="datePicker" onchange="onDateChange()"/>
  <button class="bload" id="uploadBtn" onclick="uploadResult()" disabled title="Загрузить сохранённый результат">📂 Открыть</button>
  <button class="bsave" id="saveBtn" onclick="saveResult()" disabled title="Сохранить результат">💾 Сохранить</button>
  <!-- saved indicator -->
  <div class="saved-badge" id="savedBadge">
    <span class="sb-icon">✅</span>
    <span>Сохранено:</span>
    <span class="sb-date" id="sbDate"></span>
    <span class="sb-time" id="sbTime"></span>
  </div>
  <!-- AI toggles -->
  <button class="bp" id="loadBtn" onclick="loadData()">Загрузить</button>
  <div class="ai-strip" id="aiStripEv">
    <input type="checkbox" id="aiChkEv" onchange="onAiToggle()">
    <label for="aiChkEv">🤖 AI события</label>
  </div>
  <div class="ai-strip" id="aiStripBi">
    <input type="checkbox" id="aiChkBi" onchange="onAiToggle()">
    <label for="aiChkBi">🤖 AI рождения</label>
  </div>
  <select id="aiProv" style="display:none" onchange="onProvChange()">
    <option value="gemini">Gemini 2.0 Flash</option>
    <option value="groq">Groq Llama 3.1</option>
    <option value="openrouter">OpenRouter (free)</option>
  </select>
  <button class="bs" onclick="openSettings()">⚙ Настройки</button>
</div>
<div class="main">
  <div class="sidebar">
    <div class="ssec"><div class="stitle">События</div><div id="EF"></div></div>
    <div class="ssec"><div class="stitle">Рождения</div><div id="BF"></div></div>
  </div>
  <div class="content" id="content">
    <div class="welcome">
      <div class="wi">📅</div>
      <h2>Выберите дату</h2>
      <p>Локальная классификация: только стандартная библиотека Python.<br>
      Или включите <b>Внешний AI</b> для более точного анализа<br>
      (бесплатные API: Gemini, Groq, OpenRouter).</p>
    </div>
  </div>
</div>

<div class="mo" id="MO" onclick="outClick(event)">
  <div class="md">
    <h2>⚙ Настройки</h2>
    <div class="mtabs">
      <div class="mtab active" onclick="smtab('events')">События</div>
      <div class="mtab" onclick="smtab('births')">Рождения</div>
      <div class="mtab" onclick="smtab('russian')">Русские</div>
      <div class="mtab" onclick="smtab('tune')">Параметры</div>
      <div class="mtab" onclick="smtab('ai')">🤖 AI ключи</div>
    </div>
    <div class="ce active" id="evE"></div>
    <div class="ce" id="biE"></div>
    <div class="ce" id="ruE"></div>
    <div class="ce" id="tuE"></div>
    <div class="ce" id="aiE"></div>
    <div class="ma">
      <button class="bs" onclick="closeSettings()">Отмена</button>
      <button class="bp" onclick="saveSettings()">Сохранить</button>
    </div>
  </div>
</div>

<!-- Note editor modal -->
<div class="note-modal" id="noteModal" onclick="noteModalOutClick(event)">
  <div class="note-box">
    <h3 id="noteTitle">Дополнительная информация</h3>
    <div class="note-source" id="noteSource"></div>
    <div class="note-imgs-wrap" id="noteImgs"></div>
    <label class="bs note-upload-btn" style="border:1.5px solid;border-radius:var(--r)">
      📎 Добавить картинку
      <input type="file" id="noteFileInput" accept="image/*" multiple style="display:none" onchange="uploadImages(event)">
    </label>
    <div class="note-toolbar">
      <button onclick="insertLink()" title="Вставить ссылку">🔗 Ссылка</button>
      <button onclick="document.execCommand('bold')" title="Жирный"><b>Ж</b></button>
      <button onclick="document.execCommand('italic')" title="Курсив"><i>К</i></button>
      <button onclick="clearNoteFormat()" title="Очистить форматирование">✕ Формат</button>
    </div>
    <div class="note-editor" id="noteEditor" contenteditable="true"
      placeholder="Введите текст. Для ссылки: выделите слово и нажмите 🔗 Ссылка..."></div>

    <!-- Link insert popup -->
    <div class="link-popup" id="linkPopup">
      <label>Текст ссылки:</label>
      <input type="text" id="linkText" placeholder="Текст который будет кликабельным">
      <label>URL адрес:</label>
      <input type="url" id="linkUrl" placeholder="https://...">
      <div class="link-popup-btns">
        <button class="bs" style="font-size:12px;padding:4px 10px" onclick="closeLinkPopup()">Отмена</button>
        <button class="bp" style="font-size:12px;padding:4px 10px" onclick="confirmInsertLink()">Вставить</button>
      </div>
    </div>
    <!-- Publication status block -->
    <div class="pub-block">
      <div class="pub-status-row">
        <label>Статус:</label>
        <div class="pub-status-btns">
          <button id="sDraft"     onclick="setPubStatus('draft')"     title="Черновик">✏️ Черновик</button>
          <button id="sReady"     onclick="setPubStatus('ready')"     title="Готова к публикации">📋 Готова</button>
          <button id="sPublished" onclick="setPubStatus('published')" title="Опубликовано">✅ Опубликовано</button>
        </div>
      </div>
      <div class="pub-date-url" id="pubDateUrl" style="display:none">
        <label>📅 Дата:</label>
        <input type="date" id="pubDate">
        <label>🔗 Ссылка:</label>
        <input type="url" id="pubUrl" placeholder="https://...">
      </div>
    </div>
    <div class="note-actions">
      <button class="bp" onclick="saveNote()">💾 Сохранить</button>
      <button class="bs" style="color:#c0392b;border-color:#f5c0c0" onclick="deleteNote()">🗑 Удалить заметку</button>
      <div class="spacer"></div>
      <button class="bs" onclick="closeNoteModal()">Закрыть</button>
    </div>
  </div>
</div>

<!-- Image lightbox -->
<div class="lightbox" id="lightbox" onclick="this.classList.remove('open')">
  <img id="lightboxImg" src="" alt="">
</div>

<script>
let config=null, data=null, sseSource=null, keysData={};
let notesData={};        // {wiki_url: {text, images[]}}
let _noteKey='';         // wiki_url currently being edited
let _noteDate='';        // date string of current edit session
let _pendingImgDels=[];  // image URLs queued for deletion on save
let activeFilters={events:{},births:{}}, activeTab='events';

const EC=['#2d5986','#8b3a62','#2d7a4a','#6b4c8b','#7a6b2d','#2d6b7a'];
const BC=['#4a6b36','#8b5e2d','#2d6b7a','#7a4a2d','#6b2d8b','#2d5e6b'];
const EI=['🔬','🎨','🏫','📚','⚙','🌍'];
const BI=['🔬','🎨','🎵','⚙','🏛','🌍'];

async function init(){
  const d=new Date();
  document.getElementById('datePicker').value=d.toISOString().split('T')[0];
  config=await (await fetch('/api/config')).json();
  keysData=await (await fetch('/api/keys')).json();
  renderFilters();
  const dv=document.getElementById('datePicker').value;
  await loadNotesForDate(dv);
  await checkCacheForDate(dv);
}

async function loadNotesForDate(dv){
  if(!dv){notesData={};return;}
  const r=await fetch('/api/notes?date='+dv);
  notesData=await r.json();
}

// ── AI TOGGLE ────────────────────────────────────────────────────────────────
function onAiToggle(){
  const onEv = document.getElementById('aiChkEv').checked;
  const onBi = document.getElementById('aiChkBi').checked;
  const anyOn = onEv || onBi;
  document.getElementById('aiStripEv').classList.toggle('active', onEv);
  document.getElementById('aiStripBi').classList.toggle('active', onBi);
  const sel = document.getElementById('aiProv');
  sel.style.display = anyOn ? '' : 'none';
  updateSubtitle();
}

function onProvChange(){ updateSubtitle(); }

function updateSubtitle(){
  const onEv = document.getElementById('aiChkEv').checked;
  const onBi = document.getElementById('aiChkBi').checked;
  const prov = providerName(document.getElementById('aiProv').value);
  const sub  = document.getElementById('hdrSub');
  if(!onEv && !onBi){
    sub.textContent = 'Локальная классификация · TF-IDF + словарь · Wikipedia';
  } else {
    const parts = [];
    if(onEv) parts.push('события');
    if(onBi) parts.push('рождения');
    sub.textContent = `🤖 AI (${prov}): ${parts.join(' + ')} · Wikipedia`;
  }
}

function providerName(id){
  const names={gemini:'Gemini 2.0 Flash',groq:'Groq Llama 3.1',openrouter:'OpenRouter (free)'};
  return names[id]||id;
}

function modeLabel(){
  const onEv = document.getElementById('aiChkEv').checked;
  const onBi = document.getElementById('aiChkBi').checked;
  const prov = providerName(document.getElementById('aiProv').value);
  if(!onEv && !onBi) return 'локальная TF-IDF';
  const parts = [];
  if(onEv) parts.push('события');
  if(onBi) parts.push('рождения');
  return `🤖 ${prov}: ${parts.join(' + ')}`;
}

// ── SAVE / UPLOAD / CACHE ─────────────────────────────────────────────────────

async function checkCacheForDate(dv){
  if(!dv){ hideSavedBadge(); setSaveUploadBtns(false,false); return; }
  const r = await fetch('/api/cache_info?date='+dv);
  const d = await r.json();
  if(d.exists){
    showSavedBadge(dv, d.saved_at);
    setSaveUploadBtns(!!data, true);
  } else {
    hideSavedBadge();
    setSaveUploadBtns(!!data, false);
  }
}

async function onDateChange(){
  const dv=document.getElementById('datePicker').value;
  await loadNotesForDate(dv);
  await checkCacheForDate(dv);
}

function showSavedBadge(dv, savedAt){
  const badge = document.getElementById('savedBadge');
  if(savedAt){
    // savedAt is like "2026-06-05 14:32:10" — show date and time separately
    const parts = savedAt.split(' ');
    const datePart = parts[0] || '';
    const timePart = parts[1] || '';
    // Format the save date in Russian
    let dateLabel = datePart;
    if(datePart){
      const d = new Date(datePart + 'T00:00:00');
      if(!isNaN(d)) dateLabel = d.toLocaleDateString('ru-RU',{day:'numeric',month:'short',year:'numeric'});
    }
    document.getElementById('sbDate').textContent = dateLabel;
    document.getElementById('sbTime').textContent = timePart ? timePart : '';
  } else {
    document.getElementById('sbDate').textContent = '';
    document.getElementById('sbTime').textContent = '';
  }
  badge.classList.add('visible');
}

function hideSavedBadge(){
  document.getElementById('savedBadge').classList.remove('visible');
}

function setSaveUploadBtns(canSave, canUpload){
  document.getElementById('saveBtn').disabled  = !canSave;
  document.getElementById('uploadBtn').disabled = !canUpload;
}

async function saveResult(){
  if(!data) return;
  const dv = document.getElementById('datePicker').value;
  const btn = document.getElementById('saveBtn');
  btn.disabled = true; btn.textContent = '💾 Сохранение…';
  try {
    const r = await fetch('/api/save', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({date: dv, result: data})
    });
    const resp = await r.json();
    if(resp.error) throw new Error(resp.error);
    showSavedBadge(dv, resp.saved_at);
    setSaveUploadBtns(true, true);
    btn.textContent = '💾 Сохранено ✓';
    setTimeout(() => { btn.textContent = '💾 Сохранить'; btn.disabled = false; }, 2000);
  } catch(e) {
    alert('Ошибка сохранения: ' + e.message);
    btn.textContent = '💾 Сохранить'; btn.disabled = false;
  }
}

async function uploadResult(){
  const dv = document.getElementById('datePicker').value;
  if(!dv) return;
  const btn = document.getElementById('uploadBtn');
  btn.disabled = true; btn.textContent = '📂 Открытие…';
  try {
    const r = await fetch('/api/load?date='+dv);
    const resp = await r.json();
    if(resp.error) throw new Error(resp.error);
    data = resp;
    await loadNotesForDate(dv);
    updCounts(data);
    renderContent(data, 'загружено из файла · ' + fmtDate(dv));
    showSavedBadge(dv, resp._saved_at);
    setSaveUploadBtns(true, true);
    btn.textContent = '📂 Открыть'; btn.disabled = false;
  } catch(e) {
    alert('Ошибка загрузки: ' + e.message);
    btn.textContent = '📂 Открыть'; btn.disabled = false;
  }
}
function renderFilters(){
  const ef=document.getElementById('EF'),bf=document.getElementById('BF');
  ef.innerHTML='';bf.innerHTML='';
  config.event_categories.forEach((c,i)=>{
    if(!(c.id in activeFilters.events))activeFilters.events[c.id]=true;
    ef.appendChild(mkCI(c,EC[i%EC.length],'events'));
  });
  const ru={id:'russians',label:'Русские / Советские'};
  if(!('russians' in activeFilters.births))activeFilters.births['russians']=true;
  bf.appendChild(mkCI(ru,'#8b2d2d','births'));
  config.birth_categories.forEach((c,i)=>{
    if(!(c.id in activeFilters.births))activeFilters.births[c.id]=true;
    bf.appendChild(mkCI(c,BC[i%BC.length],'births'));
  });
}

function mkCI(cat,color,type){
  const d=document.createElement('div');
  d.className='ci';
  const on=activeFilters[type][cat.id];
  d.innerHTML=`<div class="cdot" style="background:${color}"></div>
    <div class="clbl">${cat.label}</div>
    <span class="ccnt" id="cnt_${type}_${cat.id}">—</span>
    <div class="ctgl ${on?'on':''}" id="tgl_${type}_${cat.id}">${on?'✓':''}</div>`;
  d.onclick=()=>toggleF(type,cat.id);
  return d;
}

function toggleF(type,id){
  activeFilters[type][id]=!activeFilters[type][id];
  const t=document.getElementById('tgl_'+type+'_'+id);
  t.className='ctgl'+(activeFilters[type][id]?' on':'');
  t.textContent=activeFilters[type][id]?'✓':'';
  if(data)renderContent(data);
}

// ── LOAD ──────────────────────────────────────────────────────────────────────
async function loadData(){
  const dv=document.getElementById('datePicker').value;if(!dv)return;
  const useAiEv=document.getElementById('aiChkEv').checked;
  const useAiBi=document.getElementById('aiChkBi').checked;
  const aiProv=document.getElementById('aiProv').value;
  const btn=document.getElementById('loadBtn');
  btn.disabled=true;btn.textContent='Загрузка...';
  if(sseSource){sseSource.close();sseSource=null;}

  document.getElementById('content').innerHTML=
    `<div style="padding:10px 0;font-size:14px;color:var(--tx2);font-style:italic"><span class="sp"></span>Загружаем Wikipedia...</div>`;

  const r=await fetch('/api/start',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:dv,config,
      use_ai_events:useAiEv, use_ai_births:useAiBi,
      ai_provider:aiProv})
  });
  const resp=await r.json();
  if(resp.error){
    document.getElementById('content').innerHTML=`<div class="err">${resp.error}</div>`;
    btn.disabled=false;btn.textContent='Загрузить';return;
  }

  const {wiki_url,wiki_title,total_events,total_births}=resp;
  const mode=modeLabel();
  document.getElementById('content').innerHTML=`
    <div class="ptitle">${fmtDate(dv)}</div>
    <div class="pmeta"><a href="${wiki_url}" target="_blank" style="color:var(--ac)">Wikipedia — ${wiki_title}</a>
    &nbsp;·&nbsp; событий: ${total_events} · рождений: ${total_births}
    &nbsp;·&nbsp; <span style="color:var(--ai)">${mode}</span></div>
    <div class="pw">
      <div class="pl" id="pl">Инициализация...</div>
      <div class="pt"><div class="pf" id="pf" style="width:0%"></div></div>
      <div class="pd" id="pd">0% выполнено</div>
    </div>`;

  sseSource=new EventSource('/api/progress');
  sseSource.onmessage=(ev)=>{
    const m=JSON.parse(ev.data);
    if(m.type==='status'){
      document.getElementById('pl').textContent=m.text;
    } else if(m.type==='progress'){
      document.getElementById('pf').style.width=m.pct+'%';
      document.getElementById('pl').textContent=m.text;
      document.getElementById('pd').textContent=m.pct+'% выполнено';
    } else if(m.type==='done'){
      sseSource.close();sseSource=null;
      data={...m.result,wiki_url,wiki_title};
      const dv2=document.getElementById('datePicker').value;
      loadNotesForDate(dv2).then(()=>{
        updCounts(data);renderContent(data,modeLabel());
      });
      btn.disabled=false;btn.textContent='Загрузить';
      // refresh cache state
      checkCacheForDate(dv2);
      document.getElementById('saveBtn').disabled=false;
    } else if(m.type==='error'){
      sseSource.close();sseSource=null;
      document.getElementById('content').innerHTML=`<div class="err"><b>Ошибка:</b> ${m.text}</div>`;
      btn.disabled=false;btn.textContent='Загрузить';
    }
  };
  sseSource.onerror=()=>{
    if(sseSource){sseSource.close();sseSource=null;}
    btn.disabled=false;btn.textContent='Загрузить';
  };
}

function fmtDate(v){
  const d=new Date(v+'T00:00:00');
  const m=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  return d.getDate()+' '+m[d.getMonth()];
}

function updCounts(d){
  d.events.forEach(c=>{const e=document.getElementById('cnt_events_'+c.id);if(e)e.textContent=c.entries.length;});
  const re=document.getElementById('cnt_births_russians');if(re)re.textContent=(d.births_russian||[]).length;
  d.births.forEach(c=>{const e=document.getElementById('cnt_births_'+c.id);if(e)e.textContent=c.entries.length;});
}

function renderContent(d, modeLabel){
  const rSet=new Set(d.births_russian||[]);
  const mode=modeLabel||'локальная TF-IDF';
  let h=`<div class="ptitle">${fmtDate(document.getElementById('datePicker').value)}</div>
    <div class="pmeta"><a href="${d.wiki_url}" target="_blank" style="color:var(--ac)">Wikipedia — ${d.wiki_title}</a>
    &nbsp;·&nbsp; <span style="color:var(--ai)">${mode}</span></div>
    <div class="tabs">
      <div class="tab ${activeTab==='events'?'active':''}" onclick="swTab('events')">📅 События</div>
      <div class="tab ${activeTab==='births'?'active':''}" onclick="swTab('births')">👤 Рождения</div>
    </div>
    <div class="tc ${activeTab==='events'?'active':''}" id="tE">`;
  d.events.forEach((c,i)=>{if(!activeFilters.events[c.id])return;h+=sec(c,EC[i%EC.length],EI[i%EI.length],false,new Set());});
  h+=`</div><div class="tc ${activeTab==='births'?'active':''}" id="tB">`;
  if(activeFilters.births['russians']&&rSet.size){
    h+=`<div class="sb"><div class="sh" style="border-color:#8b2d2d">
      <div class="si" style="background:#8b2d2d">🏳️</div>
      <div class="st" style="color:#8b2d2d">Русские и Советские</div>
      <div class="ss">${rSet.size} чел.</div></div>`;
    rSet.forEach(e=>{
      const wk=entryWikiKey(e);
      h+=`<div class="entry" style="border-left-color:#8b2d2d">${e}${noteBtn(wk)}</div>${notePost(wk)}`;
    });
    h+=`</div>`;
  }
  d.births.forEach((c,i)=>{if(!activeFilters.births[c.id])return;h+=sec(c,BC[i%BC.length],BI[i%BI.length],true,rSet);});
  h+=`</div>`;
  document.getElementById('content').innerHTML=h;
}

// Extract the first Wikipedia href from an entry HTML string
function entryWikiKey(html){
  const m=html.match(/href="(https:\/\/en\.wikipedia\.org\/wiki\/[^"]+)"/);
  return m?m[1]:'';
}

function noteBtn(wikiKey){
  if(!wikiKey) return '';
  const n=notesData[wikiKey];
  const has=n&&(n.text||(n.images||[]).length||n.pub_status);
  const status=n?n.pub_status||'draft':'';
  const icons={draft:'✏️',ready:'📋',published:'✅'};
  const icon=icons[status]||'';
  return `<button class="note-btn${has?' has-note':''}" title="${has?'Редактировать':'Добавить'} заметку"
    onclick="openNoteModal(event,'${wikiKey.replace(/'/g,"\\'")}')">📝${has&&icon?' '+icon:''}</button>`;
}

function notePost(wikiKey){
  if(!wikiKey||!notesData[wikiKey]) return '';
  const n=notesData[wikiKey];
  const hasText=n.text&&n.text.trim();
  const imgs=(n.images||[]);
  const pub=n.published||{};
  const status=n.pub_status||'draft';
  if(!hasText&&!imgs.length&&status==='draft') return '';
  let h=`<div class="note-post">`;
  if(hasText) h+=`<div class="note-post-text">${n.text}</div>`;
  if(imgs.length){
    h+=`<div class="note-post-imgs">`;
    imgs.forEach(url=>{
      h+=`<img src="${url}" loading="lazy" onclick="openLightbox('${url}')" title="Открыть">`;
    });
    h+=`</div>`;
  }
  const labels={draft:'Черновик',ready:'Готова к публикации',published:'Опубликовано'};
  h+=`<div class="note-pub-badge"><span class="status-dot ${status}"></span><span>${labels[status]||status}</span>`;
  if(status==='published'){
    const dateStr=pub.date?new Date(pub.date+'T00:00:00').toLocaleDateString('ru-RU',
      {day:'numeric',month:'long',year:'numeric'}):'';
    if(dateStr) h+=`<span>${dateStr}</span>`;
    if(pub.url) h+=`<a href="${pub.url}" target="_blank">🔗 Ссылка на публикацию</a>`;
  }
  h+=`</div></div>`;
  return h;
}

function sec(cat,color,icon,markRu,rSet){
  if(!cat.entries||!cat.entries.length)return`<div class="sb"><div class="sh" style="border-color:${color}">
    <div class="si" style="background:${color}">${icon}</div>
    <div class="st" style="color:${color}">${cat.label}</div>
    <div class="ss">нет записей</div></div>
    <div class="empty">Записей не найдено</div></div>`;
  let h=`<div class="sb"><div class="sh" style="border-color:${color}">
    <div class="si" style="background:${color}">${icon}</div>
    <div class="st" style="color:${color}">${cat.label}</div>
    <div class="ss">${cat.entries.length} зап.</div></div>`;
  cat.entries.forEach(e=>{
    const ru=markRu&&rSet.has(e);
    const wk=entryWikiKey(e);
    h+=`<div class="entry" style="border-left-color:${color}">${e}${ru?'<span class="rb">🇷🇺 рус.</span>':''}${noteBtn(wk)}</div>${notePost(wk)}`;
  });
  return h+`</div>`;
}

function swTab(t){activeTab=t;if(data)renderContent(data);}

// ── NOTE MODAL ────────────────────────────────────────────────────────────────

function openLightbox(url){
  document.getElementById('lightboxImg').src=url;
  document.getElementById('lightbox').classList.add('open');
}

function openNoteModal(evt, wikiKey){
  evt.stopPropagation();
  _noteKey=wikiKey;
  _noteDate=document.getElementById('datePicker').value;
  _pendingImgDels=[];

  // Title: strip wiki URL to readable name
  const title=decodeURIComponent(wikiKey.split('/wiki/').pop().replace(/_/g,' '));
  document.getElementById('noteTitle').textContent='📝 '+title;
  document.getElementById('noteSource').innerHTML=
    `Запись: <a href="${wikiKey}" target="_blank">${wikiKey}</a>`;

  const existing=notesData[wikiKey];
  const editor=document.getElementById('noteEditor');
  if(existing){
    editor.innerHTML=existing.text||'';
    renderNoteImgs(existing.images||[]);
    const pub=existing.published||{};
    const status=existing.pub_status||( (pub.date||pub.url)?'published':'draft' );
    setPubStatus(status);
    document.getElementById('pubDate').value=pub.date||'';
    document.getElementById('pubUrl').value=pub.url||'';
  } else {
    const entryEl=[...document.querySelectorAll('.entry')].find(el=>el.innerHTML.includes(wikiKey));
    if(entryEl){
      // Clone node to safely strip the 📝 button without modifying the DOM
      const clone=entryEl.cloneNode(true);
      clone.querySelectorAll('button.note-btn').forEach(b=>b.remove());
      clone.querySelectorAll('span.rb').forEach(s=>s.remove());
      editor.innerHTML=clone.innerHTML.trim();
    } else {
      editor.innerHTML='';
    }
    renderNoteImgs([]);
    setPubStatus('draft');
    document.getElementById('pubDate').value='';
    document.getElementById('pubUrl').value='';
  }
  editor.focus();
  document.getElementById('noteModal').classList.add('open');
  document.getElementById('noteEditor').focus();
}

function renderNoteImgs(imgs){
  const wrap=document.getElementById('noteImgs');
  wrap.innerHTML='';
  imgs.forEach(url=>{
    const thumb=document.createElement('div');
    thumb.className='note-img-thumb';
    thumb.innerHTML=`<img src="${url}" onclick="openLightbox('${url}')">
      <button class="note-img-del" onclick="removeNoteImg('${url}',this.parentNode)" title="Удалить">✕</button>`;
    wrap.appendChild(thumb);
  });
}

function removeNoteImg(url, thumbEl){
  // Just remove from DOM — saveNote will diff and delete from server
  thumbEl.remove();
}

async function uploadImages(evt){
  const files=[...evt.target.files];
  if(!files.length) return;
  const dv=document.getElementById('datePicker').value;
  for(const file of files){
    const fd=new FormData();
    fd.append('date', dv);
    fd.append('wiki_key', _noteKey);
    fd.append('file', file, file.name);
    const r=await fetch('/api/upload_image',{method:'POST',body:fd});
    const res=await r.json();
    if(res.url){
      // Only update the DOM — notesData is updated on save
      const wrap=document.getElementById('noteImgs');
      const thumb=document.createElement('div');
      thumb.className='note-img-thumb';
      thumb.innerHTML=`<img src="${res.url}" onclick="openLightbox('${res.url}')">
        <button class="note-img-del" onclick="removeNoteImg('${res.url}',this.parentNode)" title="Удалить">✕</button>`;
      wrap.appendChild(thumb);
    } else {
      alert('Ошибка загрузки: '+(res.error||'?'));
    }
  }
  evt.target.value='';
}

let _savedRange = null;  // saved selection before popup opens

function insertLink(){
  const sel = window.getSelection();
  if(sel && sel.rangeCount > 0){
    _savedRange = sel.getRangeAt(0).cloneRange();
    const selected = sel.toString().trim();
    document.getElementById('linkText').value = selected;
  } else {
    _savedRange = null;
    document.getElementById('linkText').value = '';
  }
  document.getElementById('linkUrl').value = '';

  const popup = document.getElementById('linkPopup');
  // Show offscreen first to measure its height
  popup.style.visibility = 'hidden';
  popup.style.top = '0px';
  popup.style.left = '0px';
  popup.classList.add('open');
  const popH = popup.offsetHeight;
  const popW = popup.offsetWidth;
  popup.style.visibility = '';

  const box = document.querySelector('.note-toolbar').getBoundingClientRect();
  const vpH = window.innerHeight;
  const vpW = window.innerWidth;

  // Prefer below toolbar, flip above if not enough room
  let top = box.bottom + 4;
  if(box.bottom + popH + 8 > vpH){
    top = box.top - popH - 4;
  }
  if(top < 4) top = 4;

  // Align left with toolbar, but don't overflow right edge
  let left = box.left;
  if(left + popW + 8 > vpW){
    left = vpW - popW - 8;
  }
  if(left < 4) left = 4;

  popup.style.top  = top  + 'px';
  popup.style.left = left + 'px';
  document.getElementById('linkUrl').focus();
}

function closeLinkPopup(){
  document.getElementById('linkPopup').classList.remove('open');
  _savedRange = null;
}

function confirmInsertLink(){
  const text = document.getElementById('linkText').value.trim();
  const url  = document.getElementById('linkUrl').value.trim();
  if(!url){ closeLinkPopup(); return; }
  const label = text || url;
  const link  = `<a href="${url}" target="_blank">${label}</a>`;
  const editor = document.getElementById('noteEditor');
  editor.focus();
  if(_savedRange){
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(_savedRange);
    // Replace selection (or insert at cursor) with link HTML
    document.execCommand('insertHTML', false, link);
  } else {
    // Append at end
    editor.innerHTML += link;
  }
  closeLinkPopup();
}

function clearNoteFormat(){
  document.execCommand('removeFormat');
}


function setPubStatus(status){
  _pubStatus=status;
  ['draft','ready','published'].forEach(s=>{
    const btn=document.getElementById('s'+s.charAt(0).toUpperCase()+s.slice(1));
    if(btn) btn.className=(s===status?'active-'+s:'');
  });
  document.getElementById('pubDateUrl').style.display=(status==='published'?'flex':'none');
}

async function saveNote(){
  const text=document.getElementById('noteEditor').innerHTML.trim();

  // Collect current images from DOM thumbs (these are the ones the user wants to keep)
  const currentImgs=[...document.getElementById('noteImgs').querySelectorAll('img')]
    .map(img=>{
      // img.src is absolute (http://localhost:8765/images/...), convert to relative
      try { return new URL(img.src).pathname; }
      catch(e){ return img.src; }
    });

  // Delete images that were in the previously saved note but are no longer in DOM
  const previousImgs=(notesData[_noteKey]||{}).images||[];
  const toDelete=previousImgs.filter(url=>!currentImgs.includes(url));
  for(const url of toDelete){
    await fetch('/api/delete_image',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
  }
  // Also delete anything still pending from upload-then-remove cycles
  for(const url of _pendingImgDels){
    if(!currentImgs.includes(url)){
      await fetch('/api/delete_image',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url})});
    }
  }
  _pendingImgDels=[];

  // Publication status
  const pubDate=document.getElementById('pubDate').value.trim();
  const pubUrl=document.getElementById('pubUrl').value.trim();
  const published=(_pubStatus==='published'&&(pubDate||pubUrl))?{date:pubDate, url:pubUrl}:null;

  const note={text, images: currentImgs, pub_status: _pubStatus,
    ...(published?{published}:{})};

  const r=await fetch('/api/notes',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:_noteDate, key:_noteKey, note})});
  const res=await r.json();
  if(res.ok){
    notesData[_noteKey]=note;
    closeNoteModal();
    if(data) renderContent(data);
  } else {
    alert('Ошибка сохранения: '+(res.error||'?'));
  }
}

async function deleteNote(){
  if(!confirm('Удалить заметку и все прикреплённые картинки?')) return;
  // Delete all saved images (from notesData, not DOM — user may not have saved latest changes)
  const imgs=(notesData[_noteKey]||{}).images||[];
  for(const url of imgs){
    await fetch('/api/delete_image',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
  }
  // Also delete any freshly uploaded but unsaved images still in DOM
  const domImgs=[...document.getElementById('noteImgs').querySelectorAll('img')]
    .map(img=>{ try{return new URL(img.src).pathname;}catch(e){return img.src;} });
  for(const url of domImgs){
    if(!imgs.includes(url)){
      await fetch('/api/delete_image',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url})});
    }
  }
  await fetch('/api/notes',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:_noteDate, key:_noteKey, note:null})});
  delete notesData[_noteKey];
  closeNoteModal();
  if(data) renderContent(data);
}

function closeNoteModal(){
  document.getElementById('noteModal').classList.remove('open');
  closeLinkPopup();
  _pendingImgDels=[];
}

function noteModalOutClick(evt){
  if(evt.target===document.getElementById('noteModal')) closeNoteModal();
}

// ── SETTINGS MODAL ────────────────────────────────────────────────────────────
function openSettings(){renderME();document.getElementById('MO').classList.add('open');}
function closeSettings(){document.getElementById('MO').classList.remove('open');}
function outClick(e){if(e.target===document.getElementById('MO'))closeSettings();}

function smtab(t){
  const ns=['events','births','russian','tune','ai'];
  document.querySelectorAll('.mtab').forEach((el,i)=>el.classList.toggle('active',ns[i]===t));
  document.querySelectorAll('.ce').forEach((el,i)=>el.classList.toggle('active',ns[i]===t));
}

function renderME(){
  renderEd('evE',config.event_categories,'events');
  renderEd('biE',config.birth_categories,'births');
  document.getElementById('ruE').innerHTML=`
    <p class="hint">Слова/фразы для определения принадлежности к русской/советской культуре:</p>
    <textarea id="ruDesc" rows="4">${esc(config.russian_description||'')}</textarea>`;
  const thr=(config.tfidf_threshold||0.07).toFixed(2);
  const kw=config.keyword_min_hits||1;
  document.getElementById('tuE').innerHTML=`
    <p class="hint"><b>TF-IDF порог</b>: минимальное косинусное сходство (0.04 – мягко, 0.15 – строго).<br>
    <b>Мин. ключевых слов</b>: сколько слов из встроенного словаря должно совпасть.</p>
    <div class="srow"><label>Порог TF-IDF:</label>
      <input type="range" min="0.02" max="0.25" step="0.01" value="${thr}" id="thrS"
        oninput="document.getElementById('thrV').textContent=parseFloat(this.value).toFixed(2)">
      <span class="sval" id="thrV">${thr}</span></div>
    <div class="srow"><label>Мин. ключевых слов:</label>
      <input type="range" min="1" max="4" step="1" value="${kw}" id="kwS"
        oninput="document.getElementById('kwV').textContent=this.value">
      <span class="sval" id="kwV">${kw}</span></div>`;
  renderAiTab();
}

function renderAiTab(){
  const providers = keysData.providers || {};
  const keys = keysData.keys || {};
  let h = `<p class="hint">Все перечисленные провайдеры предлагают <b>бесплатный тариф</b> — регистрация без карты.
    Ключи сохраняются в файл <code>ai_keys.json</code> рядом со скриптом.</p>`;

  for(const [id, meta] of Object.entries(providers)){
    const hasKey = !!(keys[id] && keys[id] !== '');
    const isOR   = id === 'openrouter';
    const curModel = keys['openrouter_model'] || meta.model || '';

    h += `<div class="provider-card ${hasKey?'has-key':''}" id="pcard_${id}">
      <div class="prov-header">
        <span class="prov-name">${meta.name}</span>
        <span class="prov-free">Бесплатный</span>
        <span class="key-status ${hasKey?'set':'unset'}" id="kstat_${id}">${hasKey?'✓ ключ задан':'нет ключа'}</span>
      </div>
      <div class="prov-note">${meta.note}</div>
      <div class="prov-key-row">
        <input type="password" id="key_${id}" placeholder="Вставьте API ключ…"
          value="${keys[id]||''}" autocomplete="off"
          ${isOR ? 'oninput="onOrKeyInput()"' : ''}>
        <a href="${meta.signup}" target="_blank">Получить ключ →</a>
      </div>`;

    if(isOR){
      h += `<div style="margin-top:10px">
        <div style="font-size:12px;color:var(--tx2);margin-bottom:5px">
          Модель:
          <button class="bs" style="font-size:11px;padding:2px 9px;margin-left:6px"
            onclick="loadOrModels()" id="orLoadBtn">⟳ Загрузить список</button>
          <span id="orLoadStatus" style="font-size:11px;color:var(--tx2);margin-left:6px"></span>
        </div>
        <div style="display:flex;gap:7px;align-items:center">
          <select id="orModelSel" style="flex:1;font-size:12px"
            onchange="document.getElementById('orModelInput').value=this.value">
            <option value="${curModel}">${curModel || '— введите вручную —'}</option>
          </select>
          <input type="text" id="orModelInput" value="${esc(curModel)}"
            placeholder="или введите model ID вручную"
            style="flex:1;font-family:monospace;font-size:12px;padding:4px 7px;
                   border:1px solid var(--bd);border-radius:4px;background:var(--sf)">
        </div>
        <div style="font-size:11px;color:var(--tx2);margin-top:4px">
          Ищите бесплатные модели на
          <a href="https://openrouter.ai/models?q=:free" target="_blank"
            style="color:var(--ai)">openrouter.ai/models?q=:free</a>
        </div>
      </div>`;
    }

    h += `</div>`;
  }
  document.getElementById('aiE').innerHTML = h;
}

function onOrKeyInput(){
  // Enable load button only when key looks non-empty
  const btn = document.getElementById('orLoadBtn');
  const key = document.getElementById('key_openrouter');
  if(btn && key) btn.disabled = key.value.trim().length < 10;
}

async function loadOrModels(){
  const key = document.getElementById('key_openrouter')?.value.trim();
  if(!key){ alert('Сначала введите API ключ OpenRouter'); return; }
  const btn = document.getElementById('orLoadBtn');
  const status = document.getElementById('orLoadStatus');
  btn.disabled = true; btn.textContent = '…';
  status.textContent = 'загружаю…';
  try {
    const r = await fetch('/api/or_models?key=' + encodeURIComponent(key));
    const d = await r.json();
    if(d.error){ status.textContent = '⚠ ' + d.error; return; }
    const sel = document.getElementById('orModelSel');
    const cur = document.getElementById('orModelInput')?.value || '';
    sel.innerHTML = '';
    d.models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.id}  (${m.name})`;
      if(m.id === cur) opt.selected = true;
      sel.appendChild(opt);
    });
    if(!cur && d.models.length) {
      document.getElementById('orModelInput').value = d.models[0].id;
    }
    status.textContent = `✓ ${d.models.length} бесплатных моделей`;
  } catch(e) {
    status.textContent = '⚠ ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '⟳ Загрузить список';
  }
}

function renderEd(cid,cats,type){
  const c=document.getElementById(cid);
  c.innerHTML=`<p class="hint">Описание категории — слова/фразы для TF-IDF сравнения. Встроенный словарь работает независимо.</p>`;
  cats.forEach((cat,i)=>{
    c.innerHTML+=`<div class="cr">
      <input type="text" class="li" value="${esc(cat.label)}" placeholder="Название" id="${type}_l_${i}">
      <input type="text" class="di" value="${esc(cat.description||'')}" placeholder="Описание" id="${type}_d_${i}">
      <button class="bs dbtn" onclick="rmCat('${type}',${i})">✕</button>
    </div>`;
  });
  c.innerHTML+=`<button class="bs" style="font-size:12px;padding:4px 11px;margin-top:6px" onclick="addCat('${type}')">+ Добавить</button>`;
}

function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;');}
function rmCat(t,i){(t==='events'?config.event_categories:config.birth_categories).splice(i,1);renderME();}
function addCat(t){
  (t==='events'?config.event_categories:config.birth_categories).push(
    {id:t+'_'+Date.now(),label:'Категория',description:''});
  renderME();
}

async function saveSettings(){
  // Save category config
  ['events','births'].forEach(type=>{
    const cats=type==='events'?config.event_categories:config.birth_categories;
    cats.forEach((c,i)=>{
      const l=document.getElementById(type+'_l_'+i);
      const d=document.getElementById(type+'_d_'+i);
      if(l)c.label=l.value.trim();
      if(d)c.description=d.value.trim();
    });
  });
  const ru=document.getElementById('ruDesc');
  if(ru)config.russian_description=ru.value.trim();
  const thr=document.getElementById('thrS');
  if(thr)config.tfidf_threshold=parseFloat(thr.value);
  const kw=document.getElementById('kwS');
  if(kw)config.keyword_min_hits=parseInt(kw.value);
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)});

  // Save AI keys (only send non-empty / non-masked values)
  const newKeys = {};
  const providers = keysData.providers || {};
  for(const id of Object.keys(providers)){
    const inp = document.getElementById('key_'+id);
    if(inp) newKeys[id] = inp.value;
  }
  // Save OpenRouter model override
  const orModel = document.getElementById('orModelInput');
  if(orModel && orModel.value.trim()) newKeys['openrouter_model'] = orModel.value.trim();
  await fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(newKeys)});
  keysData = await (await fetch('/api/keys')).json();  // refresh

  renderFilters();closeSettings();
  if(data)loadData();
}

init();
</script>
</body>
</html>
"""


# ── HTTP HANDLER ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/':
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path.startswith('/images/'):
            name  = os.path.basename(path)
            fpath = os.path.join(IMAGES_DIR, name)
            if not os.path.exists(fpath):
                self.send_response(404); self.end_headers(); return
            ext   = os.path.splitext(name)[1].lower()
            ctype = {"jpg": "image/jpeg", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".png": "image/png",
                     ".gif": "image/gif",  ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/api/notes':
            params   = parse_qs(urlparse(self.path).query)
            date_str = params.get('date', [''])[0]
            self._json(notes_load(date_str) if date_str else {})

        elif path == '/api/cache_info':
            params   = parse_qs(urlparse(self.path).query)
            date_str = params.get('date', [''])[0]
            self._json(cache_info(date_str) if date_str else {"exists": False})

        elif path == '/api/load':
            params   = parse_qs(urlparse(self.path).query)
            date_str = params.get('date', [''])[0]
            result   = cache_load(date_str)
            if result is None:
                self._json({"error": "not found"})
            else:
                self._json(result)

        elif path == '/api/config':
            self._json(config_store)

        elif path == '/api/keys':
            # Return keys with values masked for display, plus provider metadata
            masked = {k: ("•" * 8 if v else "") for k, v in _keys_store.items()}
            self._json({
                "keys": masked,
                "providers": AI_PROVIDERS,
            })

        elif path == '/api/or_models':
            # Proxy OpenRouter /models with user key, return only free models
            params = parse_qs(urlparse(self.path).query)
            key = params.get('key', [''])[0].strip()
            if not key:
                self._json({"error": "no key"}); return
            try:
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {key}",
                             "User-Agent": _UA, "Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                free = [
                    {"id": m["id"], "name": m.get("name", m["id"])}
                    for m in data.get("data", [])
                    if float((m.get("pricing") or {}).get("prompt", "1") or "1") == 0
                ]
                free.sort(key=lambda m: m["id"])
                self._json({"models": free})
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                self._json({"error": f"HTTP {e.code}: {body[:200]}"})
            except Exception as e:
                self._json({"error": str(e)})

        elif path == '/api/progress':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                while True:
                    try:
                        msg = _job_queue.get(timeout=0.4)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                        if json.loads(msg)["type"] in ("done", "error"):
                            break
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except Exception:
                pass

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))

        if self.path == '/api/notes':
            try:
                payload  = json.loads(body)
                date_str = payload.get("date", "")
                key      = payload.get("key", "")
                note     = payload.get("note")          # {text, images[]} or null=delete
                if not date_str or not key:
                    self._json({"error": "missing date or key"}); return
                notes = notes_load(date_str)
                if note is None:
                    notes.pop(key, None)
                else:
                    notes[key] = note
                notes_save(date_str, notes)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/upload_image':
            try:
                ctype    = self.headers.get('Content-Type', '')
                boundary = ctype.split('boundary=')[-1].strip().encode()
                parts    = body.split(b'--' + boundary)
                fields   = {}
                file_data, file_name = None, 'upload.jpg'
                for part in parts:
                    if b'Content-Disposition' not in part:
                        continue
                    header, _, content = part.partition(b'\r\n\r\n')
                    content = content.rstrip(b'\r\n')
                    hstr    = header.decode(errors='replace')
                    name_m  = re.search(r'name="([^"]+)"', hstr)
                    fname_m = re.search(r'filename="([^"]+)"', hstr)
                    if not name_m:
                        continue
                    if fname_m:
                        file_data = content
                        file_name = fname_m.group(1)
                    else:
                        fields[name_m.group(1)] = content.decode(errors='replace')
                if file_data is None:
                    self._json({"error": "no file"}); return
                url = image_save(fields.get('date',''), fields.get('wiki_key',''),
                                 file_name, file_data)
                self._json({"ok": True, "url": url})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/delete_image':
            try:
                payload = json.loads(body)
                ok      = image_delete(payload.get("url", ""))
                self._json({"ok": ok})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/save':
            try:
                payload  = json.loads(body)
                date_str = payload.get("date", "")
                result   = payload.get("result", {})
                if not date_str or not result:
                    self._json({"error": "missing date or result"}); return
                ts = cache_save(date_str, result)
                self._json({"ok": True, "saved_at": ts})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/config':
            try:
                config_store.update(json.loads(body))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/keys':
            # Save keys: only overwrite non-empty / non-masked values
            try:
                incoming = json.loads(body)
                for k, value in incoming.items():
                    if value and not value.startswith("•"):
                        _keys_store[k] = value.strip()
                    elif not value:
                        _keys_store.pop(k, None)
                save_keys(_keys_store)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/start':
            try:
                payload    = json.loads(body)
                date_str   = payload["date"]
                cfg        = payload.get("config", config_store)
                use_ai_events  = bool(payload.get("use_ai_events",  False))
                use_ai_births  = bool(payload.get("use_ai_births",  False))
                ai_provider    = payload.get("ai_provider", "gemini")

                d = date.fromisoformat(date_str)
                months = [
                    'January','February','March','April','May','June',
                    'July','August','September','October','November','December'
                ]
                title    = f"{months[d.month - 1]}_{d.day}"
                wiki_url = f"https://en.wikipedia.org/wiki/{title}"

                html_content, err = fetch_wikipedia(title)
                if html_content is None:
                    self._json({"error": f"Не удалось загрузить Wikipedia: {err}"}); return

                events_raw, births_raw = parse_wikipedia_raw(html_content)
                run_job(events_raw, births_raw, cfg,
                        use_ai_events=use_ai_events,
                        use_ai_births=use_ai_births,
                        ai_provider=ai_provider)

                self._json({
                    "ok": True,
                    "wiki_url": wiki_url,
                    "wiki_title": title.replace("_", " "),
                    "total_events": len(events_raw),
                    "total_births": len(births_raw),
                })
            except Exception as e:
                self._json({"error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    global _keys_store
    _keys_store = load_keys()

    port = 8765
    server = HTTPServer(('localhost', port), Handler)
    url = f'http://localhost:{port}'
    print(f"\n{'='*58}")
    print(f"  📖 Читатель Дней Истории")
    print(f"{'='*58}")
    print(f"  Адрес:        {url}")
    print(f"  Классификация: локальная TF-IDF  или  внешний AI")
    print(f"  Ключи AI:     {KEYS_FILE}")
    print(f"  Зависимости:  нет (только стандартная библиотека)")
    print(f"  Остановка:    Ctrl+C")
    print(f"{'='*58}\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Сервер остановлен.")


if __name__ == '__main__':
    main()
