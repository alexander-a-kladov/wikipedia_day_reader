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
from urllib.parse import parse_qs, urlparse, quote as url_quote
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
        "name": "Groq — Llama 3.3 70B",
        "model": "llama-3.3-70b-versatile",
        "free": True,
        "signup": "https://console.groq.com/keys",
        "note": "Free: 30 req/min · 1 000 req/day · also try llama-3.1-8b-instant for higher limits",
        "model_override_key": "groq_model",
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


def _get_utility_ai():
    """
    Return (key, model, provider) for utility AI calls (emoji, books, top picks, wiki images).
    Prefers OpenRouter → Groq → Gemini, whichever has a key set.
    """
    order = ["openrouter", "groq", "gemini"]
    for prov in order:
        key = _keys_store.get(prov, "").strip()
        if key:
            override_key = AI_PROVIDERS.get(prov, {}).get("model_override_key")
            model = (_keys_store.get(override_key, "").strip()
                     or AI_PROVIDERS[prov]["model"])
            return key, model, prov
    return "", "", ""


def _utility_chat(messages: list, key: str, model: str, provider: str,
                  max_tokens: int = 200, temperature: float = 0) -> dict:
    """Call chat completion on the given provider's endpoint, always return OpenAI-style dict."""
    try:
        if provider == "gemini":
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={key}")
            parts = []
            for msg in messages:
                if msg["role"] == "system":
                    parts.insert(0, {"text": msg["content"]})
                else:
                    parts.append({"text": msg["content"]})
            payload = {"contents": [{"parts": parts}],
                       "generationConfig": {"maxOutputTokens": max_tokens,
                                            "temperature": temperature}}
            resp = _http_post(url, payload, {"Content-Type": "application/json"})
            # Extract text robustly
            try:
                text = resp["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError):
                text = str(resp)
        else:
            url = ("https://openrouter.ai/api/v1/chat/completions"
                   if provider == "openrouter"
                   else "https://api.groq.com/openai/v1/chat/completions")
            payload = {"model": model, "temperature": temperature,
                       "max_tokens": max_tokens, "messages": messages}
            resp = _http_post(url, payload,
                              {"Content-Type": "application/json",
                               "Authorization": f"Bearer {key}"})
            # Extract text robustly — handle error responses
            try:
                text = resp["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                # May be an error response
                err = resp.get("error", {})
                if isinstance(err, dict):
                    raise ValueError(f"API error: {err.get('message', str(resp))}")
                raise ValueError(f"Unexpected response: {str(resp)[:200]}")

        # Ensure text is a string
        if not isinstance(text, str):
            text = str(text)
        return {"choices": [{"message": {"content": text}}]}

    except Exception:
        raise


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
    "cinema": {
        "filmmaker","film director","director","actor","actress","screenwriter",
        "cinematographer","producer","film producer","movie director",
        "animated film","documentary","stuntman","stuntwoman","casting director",
        "film editor","cinema","movie","film actor","film actress","tv actor",
        "tv actress","television actor","television actress","voice actor",
        "voice actress","film score composer","cinematography",
    },
    "writers": {
        "author","writer","novelist","poet","playwright","essayist","biographer",
        "journalist","columnist","scriptwriter","diarist","satirist","lyricist",
        "short story writer","fiction writer","nonfiction writer","children's writer",
        "screenwriter","memoirist","translator","editor",
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
        {
            "id": "cinema", "label": "Кино",
            "description": (
                "filmmaker film director actor actress screenwriter cinematographer "
                "producer film producer movie director animated film documentary "
                "stuntman casting director film editor"
            ),
        },
        {
            "id": "writers", "label": "Писатели",
            "description": (
                "author writer novelist poet playwright essayist biographer "
                "journalist columnist scriptwriter diarist satirist lyricist "
                "short story fiction nonfiction"
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
            self.in_ev=self.in_bi=self.in_ho=self.in_h2=self.in_li=False
            self.events=[]; self.births=[]; self.holidays=[]
            self.li_html=""; self.h2t=""
            # holiday-specific
            self.ul_depth=0
            self.parent_html=""
            self.child_html=""
            self.cur_top=None
            self.in_child_li=False
            self.skip={"sup","style","script"}; self.sd=0

        def _href(self,attrs):
            h=dict(attrs).get("href","")
            if h.startswith("/wiki/"):
                return "https://en.wikipedia.org"+h
            if h.startswith("//en.wikipedia.org/"):
                return "https:"+h
            return h

        def handle_starttag(self,tag,attrs):
            ad=dict(attrs)
            if tag in self.skip: self.sd+=1; return
            if self.sd: return
            if tag=="h2": self.in_h2=True; self.h2t=""
            # ── events / births ──
            elif tag=="li" and (self.in_ev or self.in_bi):
                self.in_li=True; self.li_html=""
            elif tag=="a" and self.in_li:
                href=self._href(attrs)
                self.li_html+=f'<a href="{href}" target="_blank">'
            # ── holidays (nested) ──
            elif tag=="ul" and self.in_ho:
                self.ul_depth+=1
            elif tag=="li" and self.in_ho:
                if self.ul_depth==1:
                    self.cur_top={"text":"","children":[]}
                    self.parent_html=""; self.in_child_li=False
                elif self.ul_depth==2:
                    self.in_child_li=True; self.child_html=""
            elif tag=="a" and self.in_ho:
                link=f'<a href="{self._href(attrs)}" target="_blank">'
                if self.in_child_li: self.child_html+=link
                else: self.parent_html+=link

        def handle_endtag(self,tag):
            if tag in self.skip: self.sd=max(0,self.sd-1); return
            if self.sd: return
            if tag=="h2":
                t=self.h2t.strip().lower()
                self.in_ev="event" in t and "observ" not in t
                self.in_bi="birth" in t
                self.in_ho="holiday" in t or "observ" in t
                if self.in_ho: self.ul_depth=0
                self.in_h2=False
            # ── events / births ──
            elif tag=="li" and self.in_li:
                e=self.li_html.strip()
                if e:
                    (self.events if self.in_ev else self.births).append(e)
                self.in_li=False
            elif tag=="a" and self.in_li:
                self.li_html+="</a>"
            # ── holidays (nested) ──
            elif tag=="ul" and self.in_ho:
                self.ul_depth=max(0,self.ul_depth-1)
            elif tag=="li" and self.in_ho:
                if self.ul_depth==2 and self.cur_top is not None:
                    t=self.child_html.strip()
                    if t: self.cur_top["children"].append(t)
                    self.in_child_li=False
                elif self.ul_depth==1 and self.cur_top is not None:
                    self.cur_top["text"]=self.parent_html.strip()
                    if self.cur_top["text"] or self.cur_top["children"]:
                        self.holidays.append(self.cur_top)
                    self.cur_top=None
            elif tag=="a" and self.in_ho:
                if self.in_child_li: self.child_html+="</a>"
                else: self.parent_html+="</a>"

        def handle_data(self,data):
            if self.sd: return
            if self.in_h2: self.h2t+=data
            elif self.in_li: self.li_html+=html_module.escape(data)
            elif self.in_ho:
                esc=html_module.escape(data)
                if self.in_child_li: self.child_html+=esc
                else: self.parent_html+=esc

    p=WP()
    try: p.feed(content)
    except Exception: pass
    return p.events, p.births, p.holidays


# ── JOB STATE ─────────────────────────────────────────────────────────────────

_job_queue: queue.Queue = queue.Queue()
config_store = dict(DEFAULT_CONFIG)
_keys_store:  dict = {}   # loaded at startup
_raw_store:   dict = {}   # last fetched events_raw / births_raw for /api/highlight


def run_job(events_raw, births_raw, holidays_raw, cfg,
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
                "holidays": holidays_raw,
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
  /* notes stats block */
  .note-stat-row{display:flex;align-items:center;gap:7px;padding:5px 5px;
    border-radius:4px;margin-bottom:2px;cursor:pointer;transition:background .1s;}
  .note-stat-row:hover{background:var(--sf2);}
  .note-stat-icon{font-size:13px;flex-shrink:0;width:16px;text-align:center;}
  .note-stat-label{font-size:13px;flex:1;color:var(--tx);}
  .note-stat-count{font-size:11px;color:var(--tx2);background:var(--sf2);
    padding:1px 6px;border-radius:9px;min-width:18px;text-align:center;}
  .note-stat-pub{font-size:10px;color:#1a5c2a;background:#edf7ed;
    border:1px solid #a3d4ad;padding:0 6px;border-radius:9px;margin-left:3px;}
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
  .note-source{font-size:12px;color:var(--tx2);margin-bottom:10px;word-break:break-all;}
  .note-source a{color:var(--ai);}
  .note-title-field{width:100%;font-family:'Source Serif 4',serif;font-size:15px;
    padding:7px 10px;border:1.5px solid var(--bd);border-radius:var(--r);
    background:var(--bg);color:var(--tx);margin-bottom:10px;}
  .note-title-field:focus{outline:none;border-color:var(--ac2);}
  .note-title-field::placeholder{color:#bbb;}
  /* emoji picker panel */
  .emoji-panel{margin-bottom:10px;}
  .emoji-panel-tabs{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:5px;}
  .emoji-ptab{font-size:12px;padding:2px 9px;border:1px solid var(--bd);border-radius:12px;
    background:var(--sf);cursor:pointer;color:var(--tx2);transition:all .12s;white-space:nowrap;}
  .emoji-ptab:hover{border-color:var(--ac2);}
  .emoji-ptab.active{background:var(--ac);border-color:var(--ac);color:#fff;}
  .emoji-grid{display:flex;flex-wrap:wrap;gap:2px;min-height:34px;}
  .emoji-btn{font-size:18px;width:32px;height:32px;border:1px solid transparent;
    border-radius:5px;background:none;cursor:pointer;position:relative;
    display:flex;align-items:center;justify-content:center;padding:0;transition:background .1s;}
  .emoji-btn:hover{background:var(--sf2);border-color:var(--bd);}
  .emoji-btn[title]:hover::after{content:attr(title);position:fixed;
    transform:translateX(-50%);margin-top:36px;
    font-family:'Source Serif 4',serif;font-size:11px;
    background:#222;color:#fff;padding:3px 7px;border-radius:4px;
    white-space:nowrap;pointer-events:none;z-index:500;}
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
  .note-img-thumb{position:relative;display:inline-flex;flex-direction:column;align-items:center;max-width:260px;
    cursor:grab;user-select:none;transition:opacity .15s,transform .15s;}
  .note-img-thumb.dragging{opacity:.4;transform:scale(.96);cursor:grabbing;}
  .note-img-thumb.drag-over{outline:2px dashed var(--ac2);outline-offset:3px;border-radius:6px;}
  .note-img-thumb img{height:240px;width:auto;max-width:100%;border-radius:6px;object-fit:cover;
    border:1.5px solid var(--bd);cursor:pointer;pointer-events:none;}
  .note-img-thumb img:hover{border-color:var(--ac2);}
  .drag-hint{font-size:11px;color:var(--tx2);font-style:italic;margin-bottom:4px;}
  .note-img-caption{font-size:10px;color:var(--tx2);text-align:center;padding:3px 4px 0;
    max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:text;}
  .note-img-del{position:absolute;top:-7px;right:-7px;width:22px;height:22px;
    border-radius:50%;background:#c0392b;color:#fff;border:none;cursor:pointer;
    font-size:12px;display:flex;align-items:center;justify-content:center;line-height:1;}
  .note-img-del:hover{background:#a02020;}
  /* caption under post images */
  .note-post-img-wrap{display:flex;flex-direction:column;align-items:center;}
  .note-post-img-caption{font-size:11px;color:var(--tx2);text-align:center;
    padding:3px 4px;max-width:200px;}
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
  /* tags */
  .tag-editor-wrap{margin-bottom:10px;}
  .tag-editor-label{font-size:12px;color:var(--tx2);margin-bottom:5px;display:block;}
  .tag-list{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px;min-height:22px;}
  .tag{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:12px;
    font-size:12px;background:var(--sf2);border:1px solid var(--bd);color:var(--tx2);}
  .tag.auto{background:#e8f2fb;border-color:#8ab8d8;color:#1a4060;}
  .tag-del{background:none;border:none;cursor:pointer;font-size:10px;color:inherit;
    opacity:.7;padding:0;line-height:1;}
  .tag-del:hover{opacity:1;}
  .tag-input-row{display:flex;gap:6px;}
  .tag-input-row input{flex:1;font-size:13px;padding:4px 8px;border:1px solid var(--bd);
    border-radius:4px;background:var(--sf);font-family:'Source Serif 4',serif;}
  .tag-input-row button{font-size:12px;padding:4px 10px;}
  /* tags in post */
  .note-tags{display:flex;flex-wrap:wrap;gap:4px;padding:5px 10px 8px;}
  .note-tag{font-size:11px;padding:1px 8px;border-radius:10px;
    background:var(--sf2);border:1px solid var(--bd);color:var(--tx2);}
  /* status badges on post */
  .note-pub-badge{display:flex;align-items:center;gap:6px;padding:5px 12px;
    font-size:12px;color:var(--tx2);border-top:1px solid var(--bd);}
  /* top-pick star badge on entries */
  .top-star{display:inline-block;font-size:13px;margin-right:4px;
    vertical-align:middle;line-height:1;}
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
  /* holiday spoilers */
  .spoiler{margin:2px 0 4px 0;}
  .spoiler-hdr{display:flex;align-items:center;gap:7px;padding:5px 10px;
    border-left:3px solid #8b5e2d;cursor:pointer;font-size:14px;
    line-height:1.5;border-radius:0 4px 4px 0;transition:background .1s;user-select:none;}
  .spoiler-hdr:hover{background:var(--sf2);}
  .spoiler-arrow{font-size:10px;color:var(--tx2);transition:transform .2s;flex-shrink:0;}
  .spoiler-arrow.open{transform:rotate(90deg);}
  .spoiler-body{display:none;padding-left:14px;border-left:2px solid #ddd0c0;margin-left:10px;margin-bottom:4px;}
  .spoiler-body.open{display:block;}
  .spoiler-body .entry{font-size:13px;}
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
  <div class="ai-strip" id="aiStripTop" title="Groq выбирает 1 важнейшее событие и 2 самых известных человека дня, создаёт для них заметки автоматически">
    <input type="checkbox" id="aiChkTop" onchange="onAiToggle()">
    <label for="aiChkTop">⭐ Топ дня</label>
  </div>
  <button class="bs" onclick="openSettings()">⚙ Настройки</button>
</div>
<div class="main">
  <div class="sidebar">
    <div class="ssec"><div class="stitle">Заметки</div><div id="NF"></div></div>
    <div class="ssec"><div class="stitle">События</div><div id="EF"></div></div>
    <div class="ssec"><div class="stitle">Праздники</div><div id="HF"></div></div>
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
    <div style="display:flex;gap:7px;align-items:center;margin-bottom:10px;">
      <input type="text" class="note-title-field" id="noteTitleField"
        placeholder="✏️ Заголовок заметки (с эмодзи)…" maxlength="120"
        style="margin-bottom:0;flex:1;">
      <button class="bs" id="autoEmojiBtn" onclick="autoEmojiTitle()"
        title="Добавить эмодзи автоматически через Groq AI"
        style="font-size:12px;padding:5px 11px;white-space:nowrap;flex-shrink:0;">
        ✨ AI эмодзи
      </button>
    </div>
    <!-- Emoji panel -->
    <div class="emoji-panel">
      <div class="emoji-panel-tabs" id="emojiTabs"></div>
      <div class="emoji-grid" id="emojiGrid"></div>
    </div>
    <!-- Tags -->
    <div class="tag-editor-wrap">
      <span class="tag-editor-label">🏷 Теги:</span>
      <div class="tag-list" id="tagList"></div>
      <div class="tag-input-row">
        <input type="text" id="tagInput" placeholder="Добавить тег…" maxlength="40"
          onkeydown="if(event.key==='Enter'){event.preventDefault();addTag();}">
        <button class="bs" onclick="addTag()">+ Добавить</button>
      </div>
      <div class="tag-list" id="tagSuggestions" style="margin-top:6px"></div>
    </div>
    <div class="drag-hint" id="dragHint" style="display:none">☰ Перетащите картинки для изменения порядка</div>
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
      <button id="fetchImgBtn" onclick="fetchWikiImages()"
        title="Загрузить подходящие изображения со страниц Википедии (Groq выберет лучшие)">
        🖼️ Фото из Wiki
      </button>
      <button id="findBookBtn" onclick="findBook()"
        title="Найти книгу на Archive.org или Gutenberg через Groq AI">
        📚 Найти книгу
      </button>
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
let _noteSection='';     // 'events' | 'holidays' | 'births' — which page this note belongs to
let _pendingImgDels=[];  // image URLs queued for deletion on save
let activeFilters={events:{},births:{},holidays:{all:true}}, activeTab='events';

const EC=['#2d5986','#8b3a62','#2d7a4a','#6b4c8b','#7a6b2d','#2d6b7a'];
const BC=['#4a6b36','#8b5e2d','#2d6b7a','#7a4a2d','#6b2d8b','#2d5e6b'];
const EI=['🔬','🎨','🏫','📚','⚙','🌍'];
const BI=['🔬','🎨','🎵','⚙','🎬','✍️'];

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
  if(!dv){notesData={};renderNoteStats();return;}
  const r=await fetch('/api/notes?date='+dv);
  notesData=await r.json();
  renderNoteStats();
}

function renderNoteStats(){
  const wrap=document.getElementById('NF');
  if(!wrap) return;
  const sections=[
    {key:'events',   icon:'📅', label:'События',  color:'#2d5986'},
    {key:'holidays', icon:'🎉', label:'Праздники', color:'#8b5e2d'},
    {key:'births',   icon:'👤', label:'Рождения',  color:'#4a6b36'},
  ];
  const counts={events:{total:0,pub:0}, holidays:{total:0,pub:0}, births:{total:0,pub:0}};
  Object.entries(notesData).forEach(([wikiKey,n])=>{
    if(!n) return;
    const hasContent = n.title || (n.text&&n.text.trim()) || (n.images||[]).length || n.pub_status;
    if(!hasContent) return;
    // Legacy notes saved before the "section" field existed have no section —
    // infer it from current page data so old notes still count correctly.
    const sec = n.section || inferSection(wikiKey);
    if(sec && counts[sec]){
      counts[sec].total++;
      if((n.pub_status||'draft')==='published') counts[sec].pub++;
    }
  });
  wrap.innerHTML='';
  sections.forEach(s=>{
    const c=counts[s.key];
    const row=document.createElement('div');
    row.className='note-stat-row';
    row.title='Перейти на вкладку «'+s.label+'»';
    row.innerHTML=`<span class="note-stat-icon">${s.icon}</span>
      <span class="note-stat-label">${s.label}</span>
      <span class="note-stat-count">${c.total}</span>
      ${c.pub>0?`<span class="note-stat-pub">✓ ${c.pub}</span>`:''}`;
    row.onclick=()=>swTab(s.key);
    wrap.appendChild(row);
  });
}

// Best-effort section lookup for notes that predate the "section" field.
function inferSection(wikiKey){
  if(!data || !wikiKey) return '';
  if((data.events||[]).some(cat=>(cat.entries||[]).some(e=>e.includes(wikiKey)))) return 'events';
  if((data.births||[]).some(cat=>(cat.entries||[]).some(e=>e.includes(wikiKey)))) return 'births';
  if((data.births_russian||[]).some(e=>e.includes(wikiKey))) return 'births';
  if((data.holidays||[]).some(item=>{
    const text=typeof item==='string'?item:(item.text||'');
    const children=typeof item==='object'?(item.children||[]):[];
    return text.includes(wikiKey)||children.some(c=>c.includes(wikiKey));
  })) return 'holidays';
  return '';
}

// ── AI TOGGLE ────────────────────────────────────────────────────────────────
function onAiToggle(){
  const onEv  = document.getElementById('aiChkEv').checked;
  const onBi  = document.getElementById('aiChkBi').checked;
  const onTop = document.getElementById('aiChkTop').checked;
  const anyOn = onEv || onBi || onTop;
  document.getElementById('aiStripEv').classList.toggle('active', onEv);
  document.getElementById('aiStripBi').classList.toggle('active', onBi);
  document.getElementById('aiStripTop').classList.toggle('active', onTop);
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
    await runTopPicks(dv);
    btn.textContent = '📂 Открыть'; btn.disabled = false;
  } catch(e) {
    alert('Ошибка загрузки: ' + e.message);
    btn.textContent = '📂 Открыть'; btn.disabled = false;
  }
}
function renderFilters(){
  const ef=document.getElementById('EF');
  const hf=document.getElementById('HF');
  const bf=document.getElementById('BF');
  ef.innerHTML=''; hf.innerHTML=''; bf.innerHTML='';
  config.event_categories.forEach((c,i)=>{
    if(!(c.id in activeFilters.events))activeFilters.events[c.id]=true;
    ef.appendChild(mkCI(c,EC[i%EC.length],'events'));
  });
  // Holidays — single toggle
  if(!('all' in (activeFilters.holidays||{}))){ activeFilters.holidays={all:true}; }
  const hci=document.createElement('div'); hci.className='ci';
  const hon=activeFilters.holidays.all;
  hci.innerHTML=`<div class="cdot" style="background:#8b5e2d"></div>
    <div class="clbl">Праздники и памятные даты</div>
    <span class="ccnt" id="cnt_holidays_all">—</span>
    <div class="ctgl ${hon?'on':''}" id="tgl_holidays_all">${hon?'✓':''}</div>`;
  hci.onclick=()=>{
    activeFilters.holidays.all=!activeFilters.holidays.all;
    const t=document.getElementById('tgl_holidays_all');
    t.className='ctgl'+(activeFilters.holidays.all?' on':'');
    t.textContent=activeFilters.holidays.all?'✓':'';
    if(data) renderContent(data);
  };
  hf.appendChild(hci);
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
      _topEventKeys.clear();
      _topBirthKeys.clear();
      const dv2=document.getElementById('datePicker').value;
      loadNotesForDate(dv2).then(async ()=>{
        updCounts(data);renderContent(data,modeLabel());
        await runTopPicks(dv2);
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
  const he=document.getElementById('cnt_holidays_all');
  if(he) he.textContent=(d.holidays||[]).length;
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
      <div class="tab ${activeTab==='holidays'?'active':''}" onclick="swTab('holidays')">🎉 Праздники</div>
      <div class="tab ${activeTab==='births'?'active':''}" onclick="swTab('births')">👤 Рождения</div>
    </div>
    <div class="tc ${activeTab==='events'?'active':''}" id="tE">`;
  d.events.forEach((c,i)=>{if(!activeFilters.events[c.id])return;h+=sec(c,EC[i%EC.length],EI[i%EI.length],false,new Set(),'events');});
  h+=`</div>

    <div class="tc ${activeTab==='holidays'?'active':''}" id="tH">`;
  const holidays=d.holidays||[];
  if(activeFilters.holidays&&activeFilters.holidays.all&&holidays.length){
    h+=`<div class="sb"><div class="sh" style="border-color:#8b5e2d">
      <div class="si" style="background:#8b5e2d">🎉</div>
      <div class="st" style="color:#8b5e2d">Праздники и памятные даты</div>
      <div class="ss">${holidays.length} зап.</div></div>`;
    holidays.forEach((item,idx)=>{
      // item is either a string (legacy) or {text, children:[]}
      const text=typeof item==='string'?item:(item.text||'');
      const children=typeof item==='object'?(item.children||[]):[];
      const wk=entryWikiKey(text);
      if(children.length>0){
        // Has sub-items — render as spoiler; no note button on header
        const sid='spl_'+idx;
        // Strip trailing colon from header text for display
        const headerText=text.replace(/:\s*$/,'');
        h+=`<div class="spoiler">
          <div class="spoiler-hdr" onclick="toggleSpoiler('${sid}')">
            <span class="spoiler-arrow" id="arr_${sid}">▶</span>
            <span>${headerText}</span>
          </div>
          <div class="spoiler-body" id="${sid}">`;
        children.forEach(child=>{
          const cwk=entryWikiKey(child);
          const star=isTopEntry(cwk)?'<span class="top-star" title="⭐ Топ дня">⭐</span>':'';
          h+=`<div class="entry" style="border-left-color:#8b5e2d">${star}${child}${noteBtn(cwk,'holidays')}</div>${notePost(cwk,'holidays')}`;
        });
        h+=`</div></div>`;
      } else {
        const star=isTopEntry(wk)?'<span class="top-star" title="⭐ Топ дня">⭐</span>':'';
        h+=`<div class="entry" style="border-left-color:#8b5e2d">${star}${text}${noteBtn(wk,'holidays')}</div>${notePost(wk,'holidays')}`;
      }
    });
    h+=`</div>`;
  } else if(!holidays.length){
    h+=`<div class="empty" style="padding:20px">Праздники и памятные даты не найдены для этого дня.</div>`;
  }
  h+=`</div>

    <div class="tc ${activeTab==='births'?'active':''}" id="tB">`;
  if(activeFilters.births['russians']&&rSet.size){
    h+=`<div class="sb"><div class="sh" style="border-color:#8b2d2d">
      <div class="si" style="background:#8b2d2d">🏳️</div>
      <div class="st" style="color:#8b2d2d">Русские и Советские</div>
      <div class="ss">${rSet.size} чел.</div></div>`;
    rSet.forEach(e=>{
      const wk=entryWikiKey(e);
      const star=isTopEntry(wk)?'<span class="top-star" title="⭐ Топ дня">⭐</span>':'';
      h+=`<div class="entry" style="border-left-color:#8b2d2d">${star}${e}${noteBtn(wk,'births')}</div>${notePost(wk,'births')}`;
    });
    h+=`</div>`;
  }
  d.births.forEach((c,i)=>{if(!activeFilters.births[c.id])return;h+=sec(c,BC[i%BC.length],BI[i%BI.length],true,rSet,'births');});
  h+=`</div>`;
  document.getElementById('content').innerHTML=h;
}

// Extract the first Wikipedia href from an entry HTML string
function entryWikiKey(html){
  const m=html.match(/href="((?:https?:)?\/\/en\.wikipedia\.org\/wiki\/[^"]+)"/);
  if(!m) return '';
  // Normalise protocol-relative URLs to https://
  return m[1].startsWith('//') ? 'https:'+m[1] : m[1];
}

function noteBtn(wikiKey, section){
  if(!wikiKey) return '';
  const n=notesData[wikiKey];
  const has=n&&(n.text||(n.images||[]).length||n.pub_status);
  const status=n?n.pub_status||'draft':'';
  const icons={draft:'✏️',ready:'📋',published:'✅'};
  const icon=icons[status]||'';
  const sec=section?section.replace(/'/g,"\\'"):'';
  return `<button class="note-btn${has?' has-note':''}" title="${has?'Редактировать':'Добавить'} заметку"
    onclick="openNoteModal(event,'${wikiKey.replace(/'/g,"\\'")}','${sec}')">📝${has&&icon?' '+icon:''}</button>`;
}

function notePost(wikiKey, section){
  if(!wikiKey||!notesData[wikiKey]) return '';
  const n=notesData[wikiKey];
  // Only show this note's post on the page matching its assigned section
  // (notes without a section, e.g. legacy data, are shown everywhere)
  if(n.section && section && n.section!==section) return '';
  const hasText=n.text&&n.text.trim();
  const imgs=(n.images||[]);
  const pub=n.published||{};
  const status=n.pub_status||'draft';
  if(!n.title&&!hasText&&!imgs.length&&status==='draft') return '';
  let h=`<div class="note-post">`;
  if(n.title) h+=`<div style="font-family:'Playfair Display',serif;font-size:15px;
    font-weight:600;padding:8px 12px 2px;color:var(--tx)">${n.title}</div>`;
  if(hasText) h+=`<div class="note-post-text">${n.text}</div>`;
  if(imgs.length){
    const caps = n.image_captions || {};
    h+=`<div class="note-post-imgs">`;
    imgs.forEach(url=>{
      const cap = caps[url] || '';
      h+=`<div class="note-post-img-wrap">
        <img src="${url}" loading="lazy" onclick="openLightbox('${url}')"
          title="${cap.replace(/"/g,'&quot;')}">
        ${cap ? `<div class="note-post-img-caption">${cap}</div>` : ''}
      </div>`;
    });
    h+=`</div>`;
  }
  const labels={draft:'Черновик',ready:'Готова к публикации',published:'Опубликовано'};
  // Tags
  const tags=n.tags||[];
  if(tags.length){
    h+=`<div class="note-tags">`;
    tags.forEach(t=>{ h+=`<span class="note-tag">${t}</span>`; });
    h+=`</div>`;
  }
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

function sec(cat,color,icon,markRu,rSet,section){
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
    const ru = markRu && rSet.has(e);
    const wk = entryWikiKey(e);
    const star = isTopEntry(wk) ? '<span class="top-star" title="⭐ Топ дня">⭐</span>' : '';
    h+=`<div class="entry" style="border-left-color:${color}">${star}${e}${ru?'<span class="rb">🇷🇺 рус.</span>':''}${noteBtn(wk,section)}</div>${notePost(wk,section)}`;
  });
  return h+`</div>`;
}

function swTab(t){activeTab=t;if(data)renderContent(data);}

function toggleSpoiler(id){
  const body=document.getElementById(id);
  const arr=document.getElementById('arr_'+id);
  if(!body) return;
  const open=body.classList.toggle('open');
  if(arr) arr.classList.toggle('open', open);
}

// ── NOTE MODAL ────────────────────────────────────────────────────────────────

function openLightbox(url){
  document.getElementById('lightboxImg').src=url;
  document.getElementById('lightbox').classList.add('open');
}

function openNoteModal(evt, wikiKey, section){
  evt.stopPropagation();
  _noteKey=wikiKey;
  _noteDate=document.getElementById('datePicker').value;
  _pendingImgDels=[];
  // Section this note belongs to: 'events' | 'holidays' | 'births'.
  // For existing notes, prefer the saved section (it shouldn't change
  // just because the same wiki link happens to appear elsewhere).
  const existingForSection=notesData[wikiKey];
  _noteSection = existingForSection ? (existingForSection.section || section || '') : (section || '');

  // Title: strip wiki URL to readable name
  const title=decodeURIComponent(wikiKey.split('/wiki/').pop().replace(/_/g,' '));
  document.getElementById('noteTitle').textContent='📝 '+title;
  const sectionLabels={events:'📅 Событие',holidays:'🎉 Праздник',births:'👤 Рождение'};
  const sectionLbl=sectionLabels[_noteSection]||'';
  document.getElementById('noteSource').innerHTML=
    `${sectionLbl?sectionLbl+' · ':''}Запись: <a href="${wikiKey}" target="_blank">${wikiKey}</a>`;

  const existing=notesData[wikiKey];
  const editor=document.getElementById('noteEditor');
  document.getElementById('noteTitleField').value = existing ? (existing.title||'') : '';
  // Tags: load saved or auto-detect for new note
  _tags = existing ? [...(existing.tags||[])] : detectAutoTags(wikiKey);
  renderTagList();
  initEmojiPanel();
  if(existing){
    editor.innerHTML=existing.text||'';
    renderNoteImgs(existing.images||[], existing.image_captions||{});
    const pub=existing.published||{};
    const status=existing.pub_status||( (pub.date||pub.url)?'published':'draft' );
    setPubStatus(status);
    document.getElementById('pubDate').value=pub.date||'';
    document.getElementById('pubUrl').value=pub.url||'';
  } else {
    // Search only within the correct section tab to avoid cross-tab matches
    const tabId = _noteSection==='events'   ? 'tE'
                : _noteSection==='holidays' ? 'tH'
                : _noteSection==='births'   ? 'tB' : null;
    const container = tabId ? document.getElementById(tabId) : document;
    const scope = container || document;
    const entryEl=[...scope.querySelectorAll('.entry')].find(el=>el.innerHTML.includes(wikiKey));
    if(entryEl){
      const clone=entryEl.cloneNode(true);
      clone.querySelectorAll('button.note-btn').forEach(b=>b.remove());
      clone.querySelectorAll('span.rb').forEach(s=>s.remove());
      clone.querySelectorAll('span.top-star').forEach(s=>s.remove());
      const cloneHtml = clone.innerHTML.trim();

      if(_noteSection === 'births'){
        // Insert "Birthday of " after the year dash: "1879 – Albert..." → "1879 – Birthday of Albert..."
        const withPrefix = cloneHtml.replace(
          /^(.*?[–—-]\s*)/,
          '$1Birthday of '
        );
        editor.innerHTML = withPrefix;

        // Extract person's name for the title (text before the first comma)
        // e.g. "1879 – Albert Einstein, German physicist" → "Albert Einstein"
        const plainText = clone.innerText || clone.textContent || '';
        const afterDash = plainText.replace(/^\d+\s*[–—-]\s*/, '').trim();
        const name = afterDash.split(',')[0].trim();
        if(name) document.getElementById('noteTitleField').value = name;
      } else {
        editor.innerHTML = cloneHtml;
      }
    } else {
      editor.innerHTML='';
    }
    renderNoteImgs([], {});
    setPubStatus('draft');
    document.getElementById('pubDate').value='';
    document.getElementById('pubUrl').value='';
  }
  editor.focus();
  document.getElementById('noteModal').classList.add('open');
  document.getElementById('noteEditor').focus();
}

function renderNoteImgs(imgs, captions){
  const caps = captions || (notesData[_noteKey]||{}).image_captions || {};
  const wrap = document.getElementById('noteImgs');
  [...wrap.querySelectorAll('.note-img-thumb')].forEach(el => el.remove());
  imgs.forEach(url => _addImgThumb(wrap, url, caps[url]||''));
  _updateDragHint();
}

function _updateDragHint(){
  const hint = document.getElementById('dragHint');
  if(!hint) return;
  const count = document.querySelectorAll('#noteImgs .note-img-thumb').length;
  hint.style.display = count >= 2 ? '' : 'none';
}

function _addImgThumb(wrap, url, caption){
  const safeUrl  = url.replace(/'/g, "\\'");
  const safeCap  = (caption||'').replace(/"/g, '&quot;');
  const thumb    = document.createElement('div');
  thumb.className = 'note-img-thumb';
  thumb.draggable = true;
  thumb.innerHTML = `
    <img src="${url}" data-caption="${safeCap}"
      onclick="openLightbox('${safeUrl}')"
      title="${safeCap}">
    <button class="note-img-del"
      onclick="event.stopPropagation();removeNoteImg('${safeUrl}',this.parentNode)"
      title="Удалить">✕</button>
    ${caption ? `<div class="note-img-caption" title="${safeCap}">${caption}</div>` : ''}`;

  // ── Drag-and-drop handlers ──────────────────────────────────────────────────
  thumb.addEventListener('dragstart', e => {
    e.dataTransfer.effectAllowed = 'move';
    // Store reference via a temp id
    thumb.dataset.dragging = '1';
    setTimeout(() => thumb.classList.add('dragging'), 0);
  });
  thumb.addEventListener('dragend', () => {
    thumb.classList.remove('dragging');
    delete thumb.dataset.dragging;
    document.querySelectorAll('.note-img-thumb').forEach(t => t.classList.remove('drag-over'));
  });
  thumb.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    // Highlight drop target
    document.querySelectorAll('.note-img-thumb').forEach(t => t.classList.remove('drag-over'));
    if(!thumb.dataset.dragging) thumb.classList.add('drag-over');
  });
  thumb.addEventListener('dragleave', () => thumb.classList.remove('drag-over'));
  thumb.addEventListener('drop', e => {
    e.preventDefault();
    thumb.classList.remove('drag-over');
    const dragging = document.querySelector('.note-img-thumb[data-dragging="1"]');
    if(!dragging || dragging === thumb) return;
    const w = document.getElementById('noteImgs');
    const thumbs = [...w.querySelectorAll('.note-img-thumb')];
    const fromIdx = thumbs.indexOf(dragging);
    const toIdx   = thumbs.indexOf(thumb);
    // Insert before or after based on position
    if(fromIdx < toIdx){
      w.insertBefore(dragging, thumb.nextSibling);
    } else {
      w.insertBefore(dragging, thumb);
    }
  });

  wrap.appendChild(thumb);
  _updateDragHint();
}

function removeNoteImg(url, thumbEl){
  thumbEl.remove();
  _updateDragHint();
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

// ── EMOJI PANEL ───────────────────────────────────────────────────────────────

// Each entry: [emoji, hint] or just emoji string (backward compat)
const EMOJI_CATS = [
  { id:'suggested', label:'✨ Подходящие', emojis:[] }, // filled dynamically
  { id:'science', label:'🔬 Наука', emojis:[
    ['🔬','Микроскоп'],['🧪','Пробирка'],['🧫','Чашка Петри'],['🧬','ДНК'],
    ['⚗️','Колба'],['🔭','Телескоп'],['🌡️','Термометр'],['💉','Шприц'],
    ['🩺','Стетоскоп'],['🧠','Мозг'],['⚛️','Атом'],['🧲','Магнит'],
    ['💊','Таблетка'],['🦠','Микроб'],['🏥','Больница'],['🔋','Батарея'],
    ['💡','Лампочка / идея'],['📡','Антенна'],['🌌','Галактика'],
    ['🌊','Океан'],['🌋','Вулкан'],['🌍','Земля'],['🧭','Компас'],
    ['🛰️','Спутник'],['☢️','Радиация'],['🔩','Болт'],['⚙️','Шестерня'],
    ['🧰','Инструменты'],['🖥️','Компьютер'],['💻','Ноутбук'],
    ['🤖','Робот'],['🧲','Магнит'],['🔌','Розетка'],['🌡️','Градусник'] ] },
  { id:'art', label:'🎨 Искусство', emojis:[
    ['🎨','Художник / палитра'],['🖼️','Картина'],['✏️','Карандаш'],
    ['🖌️','Кисть'],['🖍️','Мелок'],['📸','Фотография'],['🎭','Театр / маски'],
    ['🎬','Кино / хлопушка'],['🎞️','Киноплёнка'],['📽️','Кинопроектор'],
    ['🎤','Микрофон'],['🎙️','Студийный микрофон'],['🎼','Ноты'],
    ['🎹','Пианино'],['🎸','Гитара'],['🎺','Труба'],['🎻','Скрипка'],
    ['🥁','Барабан'],['🎷','Саксофон'],['🎶','Музыка'],['🎵','Нота'],
    ['🎧','Наушники'],['🎪','Цирк'],['🏺','Амфора'],['🗿','Скульптура'],
    ['🖊️','Ручка'],['✒️','Перо'],['📷','Фотоаппарат'],['🎥','Видеокамера'],
    ['🎦','Кинотеатр'],['📺','Телевизор'],['🎠','Карусель'],['🎡','Колесо обозрения'] ] },
  { id:'history', label:'🏛 История', emojis:[
    ['🏛️','Храм / античность'],['⚔️','Мечи / битва'],['🛡️','Щит'],
    ['👑','Корона'],['🗺️','Карта'],['📜','Свиток'],['🏰','Замок'],
    ['⚓','Якорь'],['🚂','Паровоз'],['🗽','Статуя Свободы'],
    ['🗼','Эйфелева башня'],['🌉','Мост'],['🕌','Мечеть'],['🛕','Храм'],
    ['⛪','Церковь'],['🏯','Самурайский замок'],['🗿','Моаи'],['⛵','Парусник'],
    ['🚀','Ракета'],['✈️','Самолёт'],['🚢','Корабль'],['🏹','Лук и стрела'],
    ['🪖','Каска'],['🎖️','Медаль'],['🏅','Медаль спортсмена'],
    ['⚖️','Весы правосудия'],['🏟️','Стадион / арена'],['🗡️','Кинжал'],
    ['🛻','Транспорт'],['🚁','Вертолёт'],['🪃','Бумеранг'],['🌐','Глобус'] ] },
  { id:'people', label:'👤 Люди', emojis:[
    ['👤','Силуэт'],['👑','Монарх'],['🧑‍🔬','Учёный'],['🧑‍🎨','Художник'],
    ['🧑‍💻','Программист'],['🧑‍🏫','Учитель'],['🧑‍⚕️','Врач'],['🧑‍🌾','Фермер'],
    ['👩','Женщина'],['👨','Мужчина'],['🧒','Ребёнок'],['👶','Младенец'],
    ['🧙','Волшебник'],['🤴','Принц'],['👸','Принцесса'],['🧑‍🚀','Астронавт'],
    ['🦸','Герой'],['🕵️','Детектив'],['🧑‍⚖️','Судья'],['🧑‍🎤','Певец'],
    ['🧑‍🏭','Рабочий'],['👨‍👩‍👧','Семья'],['🫂','Объятия'],['🧝','Эльф'],
    ['🧛','Вампир'],['🧟','Зомби'],['🧞','Джин'],['🧜','Русалка'],
    ['🪄','Волшебная палочка'],['💂','Гвардеец'],['🥷','Ниндзя'],['🤵','Джентльмен'] ] },
  { id:'nature', label:'🌿 Природа', emojis:[
    ['🌿','Листок'],['🌸','Сакура'],['🌺','Цветок гибискус'],['🌻','Подсолнух'],
    ['🍃','Листья на ветру'],['🌲','Ель'],['🌳','Дерево'],['🌴','Пальма'],
    ['🌵','Кактус'],['🌾','Колос пшеницы'],['🍀','Клевер'],['🌹','Роза'],
    ['🦋','Бабочка'],['🐦','Птица'],['🦅','Орёл'],['🦁','Лев'],
    ['🐘','Слон'],['🐬','Дельфин'],['🐅','Тигр'],['🐆','Леопард'],
    ['🦒','Жираф'],['🐊','Крокодил'],['🦈','Акула'],['🌊','Волна'],
    ['🏔️','Гора'],['🌄','Рассвет в горах'],['🌅','Закат'],
    ['❄️','Снежинка'],['🌈','Радуга'],['⛈️','Гроза'],['🌪️','Торнадо'],
    ['🍄','Гриб'],['🐾','Следы животного'],['🌙','Луна'] ] },
  { id:'symbols', label:'★ Символы', emojis:[
    ['⭐','Звезда'],['🌟','Яркая звезда'],['💫','Кружащаяся звезда'],
    ['✨','Искры'],['🔥','Огонь'],['💎','Алмаз'],['❤️','Красное сердце'],
    ['💙','Синее сердце'],['💛','Жёлтое сердце'],['💚','Зелёное сердце'],
    ['🖤','Чёрное сердце'],['🤍','Белое сердце'],['⚡','Молния'],
    ['🎯','Цель'],['🏆','Трофей'],['🥇','Золотая медаль'],['🎗️','Лента'],
    ['📌','Канцелярская кнопка'],['🔑','Ключ'],['💰','Деньги'],
    ['📖','Открытая книга'],['📚','Стопка книг'],['✉️','Конверт'],
    ['📰','Газета'],['🗞️','Свёрнутая газета'],['🔐','Замок с ключом'],
    ['⚜️','Лилия'],['☯️','Инь-ян'],['✡️','Звезда Давида'],['☪️','Полумесяц'],
    ['♾️','Бесконечность'],['🆕','Новый'],['❗','Восклицание'],['❓','Вопрос'] ] },
  { id:'flags', label:'🏳️ Флаги', emojis:[
    ['🏳️','Белый флаг'],['🏴','Чёрный флаг'],['🚩','Красный флаг'],
    ['🏁','Клетчатый флаг'],['🇺🇳','ООН'],
    ['🇦🇩','Андорра'],['🇦🇪','ОАЭ'],['🇦🇫','Афганистан'],['🇦🇱','Албания'],
    ['🇦🇲','Армения'],['🇦🇴','Ангола'],['🇦🇷','Аргентина'],['🇦🇹','Австрия'],
    ['🇦🇺','Австралия'],['🇦🇿','Азербайджан'],['🇧🇦','Босния и Герцеговина'],
    ['🇧🇩','Бангладеш'],['🇧🇪','Бельгия'],['🇧🇬','Болгария'],['🇧🇷','Бразилия'],
    ['🇧🇾','Беларусь'],['🇨🇦','Канада'],['🇨🇳','Китай'],['🇨🇴','Колумбия'],
    ['🇨🇺','Куба'],['🇨🇿','Чехия'],['🇩🇪','Германия'],['🇩🇰','Дания'],
    ['🇩🇿','Алжир'],['🇪🇨','Эквадор'],['🇪🇪','Эстония'],['🇪🇬','Египет'],
    ['🇪🇸','Испания'],['🇪🇹','Эфиопия'],['🇫🇮','Финляндия'],['🇫🇷','Франция'],
    ['🇬🇧','Великобритания'],['🇬🇪','Грузия'],['🇬🇭','Гана'],['🇬🇷','Греция'],
    ['🇭🇷','Хорватия'],['🇭🇺','Венгрия'],['🇮🇩','Индонезия'],['🇮🇪','Ирландия'],
    ['🇮🇱','Израиль'],['🇮🇳','Индия'],['🇮🇶','Ирак'],['🇮🇷','Иран'],
    ['🇮🇸','Исландия'],['🇮🇹','Италия'],['🇯🇵','Япония'],['🇯🇴','Иордания'],
    ['🇰🇪','Кения'],['🇰🇵','Северная Корея'],['🇰🇷','Южная Корея'],['🇰🇿','Казахстан'],
    ['🇱🇧','Ливан'],['🇱🇹','Литва'],['🇱🇻','Латвия'],['🇱🇾','Ливия'],
    ['🇲🇦','Марокко'],['🇲🇩','Молдова'],['🇲🇳','Монголия'],['🇲🇽','Мексика'],
    ['🇲🇾','Малайзия'],['🇳🇬','Нигерия'],['🇳🇱','Нидерланды'],['🇳🇴','Норвегия'],
    ['🇳🇵','Непал'],['🇳🇿','Новая Зеландия'],['🇵🇦','Панама'],['🇵🇪','Перу'],
    ['🇵🇭','Филиппины'],['🇵🇰','Пакистан'],['🇵🇱','Польша'],['🇵🇸','Палестина'],
    ['🇵🇹','Португалия'],['🇶🇦','Катар'],['🇷🇴','Румыния'],['🇷🇸','Сербия'],
    ['🇷🇺','Россия'],['🇸🇦','Саудовская Аравия'],['🇸🇩','Судан'],['🇸🇪','Швеция'],
    ['🇸🇬','Сингапур'],['🇸🇮','Словения'],['🇸🇰','Словакия'],['🇸🇾','Сирия'],
    ['🇹🇭','Таиланд'],['🇹🇷','Турция'],['🇹🇳','Тунис'],['🇹🇿','Танзания'],
    ['🇺🇦','Украина'],['🇺🇬','Уганда'],['🇺🇸','США'],['🇺🇿','Узбекистан'],
    ['🇻🇦','Ватикан'],['🇻🇳','Вьетнам'],['🇾🇪','Йемен'],
    ['🇿🇦','ЮАР'],['🇿🇲','Замбия'],['🇿🇼','Зимбабве'] ] },
  { id:'time', label:'📅 Время', emojis:[
    ['📅','Календарь'],['📆','Отрывной календарь'],['🗓️','Спиральный календарь'],
    ['⏳','Песочные часы (идут)'],['⌛','Песочные часы (кончились)'],
    ['🕰️','Старинные часы'],['⏰','Будильник'],['🔔','Колокол'],
    ['📣','Мегафон'],['🎊','Конфетти'],['🎉','Праздник'],['🎁','Подарок'],
    ['🎈','Воздушный шар'],['🕯️','Свеча'],['🪔','Масляная лампа'],
    ['🌙','Луна'],['☀️','Солнце'],['🌤️','Переменная облачность'],
    ['🌧️','Дождь'],['⛄','Снеговик'],['🌺','Весна'],['🍂','Осень'],
    ['🌸','Сакура / весна'],['❄️','Зима'],['🍁','Осенний лист'],
    ['🌞','Солнце с лицом'],['🌛','Месяц'],['⭐','Звезда'],['🎆','Фейерверк'],
    ['🎇','Бенгальский огонь'],['🧨','Петарда'],['🪅','Пиньята'],['🥳','Вечеринка'] ] },
];

// Tag → emoji category mapping for smart suggestions
const TAG_EMOJI_MAP = {
  Science:    ['🔬','⚗️','🔭','🧪','💡','⚛️','🧠','🌌','🧬','🌡️','📡','💊'],
  Art:        ['🎨','🖼️','✏️','🖌️','🎭','📸','🎬','🎼','🎵','🎤','🎸','🎺'],
  Music:      ['🎵','🎼','🎹','🎸','🎺','🥁','🎷','🎻','🎧','🎤','🎙️','🎶'],
  Literature: ['📖','📚','✏️','📜','📰','🗞️','🖊️','📝','✍️','📕','📗','📘'],
  Education:  ['🎓','🏫','📚','📝','✏️','🖊️','📐','📏','🔬','💡','🏛️','📖'],
  Inventions: ['💡','⚙️','🔧','🔩','🛠️','🚀','⚗️','🔋','💻','📡','🤖','🛰️'],
  Space:      ['🚀','🌌','⭐','🌙','🛸','🔭','🌍','🪐','☄️','🌠','🛰️','👨‍🚀'],
  Exploration:['🗺️','⚓','🧭','🌏','🏔️','⛵','✈️','🚢','🌋','🌊','🏕️','🗺️'],
  Cinema:     ['🎬','🎞️','📽️','🎥','🎭','🎤','🌟','🏆','🎬','📺','🎦','🎪'],
  Holiday:    ['🎊','🎉','🎁','🎈','🕯️','🌟','🏆','🎗️','🥂','🎀','🎠','🎆'],
  Writers:    ['✍️','📖','📚','📜','🖊️','📝','📕','📰','🗞️','📃','✒️','📄'],
};

let _emojiActiveTab = 'suggested';

function initEmojiPanel(){
  // Build suggested list from current tags — extract emoji char from [em,hint] pairs
  const suggestedSet = new Set();
  _tags.forEach(tag => {
    (TAG_EMOJI_MAP[tag]||[]).forEach(e => suggestedSet.add(e));
  });
  // Store as [emoji, hint] pairs for suggested tab using TAG_EMOJI_MAP values directly
  // (those are already plain emoji strings; wrap them)
  EMOJI_CATS[0].emojis = [...suggestedSet].map(e=>[e,'']);

  _emojiActiveTab = suggestedSet.size > 0 ? 'suggested' : 'art';
  renderEmojiTabs();
  renderEmojiGrid();
}

function renderEmojiTabs(){
  const wrap = document.getElementById('emojiTabs');
  if(!wrap) return;
  wrap.innerHTML = '';
  EMOJI_CATS.forEach(cat => {
    if(cat.id === 'suggested' && cat.emojis.length === 0) return;
    const btn = document.createElement('button');
    btn.className = 'emoji-ptab' + (cat.id === _emojiActiveTab ? ' active' : '');
    btn.textContent = cat.label;
    btn.onclick = () => { _emojiActiveTab = cat.id; renderEmojiTabs(); renderEmojiGrid(); };
    wrap.appendChild(btn);
  });
}

function renderEmojiGrid(){
  const wrap = document.getElementById('emojiGrid');
  if(!wrap) return;
  const cat = EMOJI_CATS.find(c => c.id === _emojiActiveTab);
  if(!cat){ wrap.innerHTML=''; return; }
  wrap.innerHTML = '';
  cat.emojis.forEach(item => {
    const em   = Array.isArray(item) ? item[0] : item;
    const hint = Array.isArray(item) ? item[1] : '';
    const btn = document.createElement('button');
    btn.className = 'emoji-btn';
    btn.textContent = em;
    if(hint) btn.title = hint;
    btn.onclick = () => insertEmoji(em);
    wrap.appendChild(btn);
  });
}

function insertEmoji(emoji){
  // Default target: title field. Only insert into editor if editor is explicitly focused.
  const titleField = document.getElementById('noteTitleField');
  const editor     = document.getElementById('noteEditor');
  const active     = document.activeElement;

  if(active === editor){
    // User has explicitly clicked into the editor — insert there
    editor.focus();
    const sel = window.getSelection();
    if(sel && sel.rangeCount > 0){
      document.execCommand('insertText', false, emoji);
    } else {
      editor.innerHTML += emoji;
    }
  } else {
    // Default: insert into title field at cursor (or at end)
    const s = titleField.selectionStart ?? titleField.value.length;
    const e = titleField.selectionEnd   ?? titleField.value.length;
    titleField.value = titleField.value.slice(0,s) + emoji + titleField.value.slice(e);
    titleField.selectionStart = titleField.selectionEnd = s + emoji.length;
    titleField.focus();
  }
}

// ── TOP PICKS (Groq auto-select + auto-note) ──────────────────────────────────

// ── ⭐ TOP PICKS ──────────────────────────────────────────────────────────────

function stripHtmlForApi(html){
  return html.replace(/<[^>]+>/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<')
    .replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'")
    .replace(/\s+/g,' ').trim();
}
let _topEventKeys  = new Set();   // wiki keys of top events
let _topBirthKeys  = new Set();   // wiki keys of top births (all cats)

function isTopEntry(wikiKey){
  return _topEventKeys.has(wikiKey) || _topBirthKeys.has(wikiKey);
}

async function runTopPicks(dateVal){
  if(!document.getElementById('aiChkTop').checked) return;
  if(!data) return;

  // Show spinner in notes sidebar
  const nf = document.getElementById('NF');
  const origNF = nf ? nf.innerHTML : '';
  if(nf) nf.innerHTML += '<div style="font-size:11px;color:var(--tx2);padding:4px 5px">⭐ Выбираю топ…</div>';

  try {
    // ── Build event list (flat, with global index into all events) ──────────
    const eventsList = [];
    let evGlobalIdx  = 0;
    (data.events||[]).forEach(cat=>{
      (cat.entries||[]).forEach(e=>{
        eventsList.push({idx: evGlobalIdx++, html: e, text: stripHtmlForApi(e)});
      });
    });

    // ── Build per-category birth lists ──────────────────────────────────────
    const BIRTH_CATS = ['scientists','artists','composers','inventors','cinema','writers'];
    const birthsByCat = {};
    (data.births||[]).forEach(cat=>{
      if(!BIRTH_CATS.includes(cat.id)) return;
      birthsByCat[cat.id] = (cat.entries||[]).map((e, i)=>({
        idx: i, html: e, text: stripHtmlForApi(e)
      }));
    });

    if(!eventsList.length && !Object.keys(birthsByCat).length) return;

    const r   = await fetch('/api/top_picks', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        events:        eventsList.map(e=>({idx: e.idx, text: e.text})),
        births_by_cat: Object.fromEntries(
          Object.entries(birthsByCat).map(([k,v])=>[k, v.map(e=>({idx:e.idx, text:e.text}))])
        ),
      })
    });
    const res = await r.json();
    // ── Debug: show result visually ──────────────────────────────────────────
    const dbg = [];
    dbg.push('API response: ' + JSON.stringify(res).slice(0,400));

    if(res.error){ console.warn('[TopPicks] error:', res.error); alert('Топ дня ошибка: ' + res.error); return; }

    // ── Apply stars ─────────────────────────────────────────────────────────
    _topEventKeys.clear();
    _topBirthKeys.clear();

    (res.event_indices||[]).forEach(globalIdx=>{
      const entry = eventsList.find(e=>e.idx===globalIdx);
      dbg.push(`event[${globalIdx}]: ${entry ? entry.text.slice(0,50) : 'NOT FOUND'}`);
      if(entry){ const wk=entryWikiKey(entry.html); dbg.push(`  wk: ${wk||'EMPTY'}`); if(wk) _topEventKeys.add(wk); }
    });

    const byCatResult = res.birth_indices_by_cat || {};
    Object.entries(byCatResult).forEach(([catId, indices])=>{
      const catEntries = birthsByCat[catId] || [];
      dbg.push(`${catId} indices=${JSON.stringify(indices)} entries=${catEntries.length}`);
      (indices||[]).forEach(localIdx=>{
        const entry = catEntries[localIdx] || catEntries.find(e=>e.idx===localIdx);
        dbg.push(`  [${localIdx}]: ${entry ? entry.text.slice(0,50) : 'NOT FOUND'}`);
        if(entry){ const wk=entryWikiKey(entry.html); dbg.push(`    wk: ${wk||'EMPTY'}`); if(wk) _topBirthKeys.add(wk); }
      });
    });

    dbg.push(`RESULT: ${_topEventKeys.size} events, ${_topBirthKeys.size} births starred`);
    alert('⭐ Топ дня debug:\n\n' + dbg.join('\n'));

    // Re-render to show stars
    if(data) renderContent(data);

  } catch(e){
    console.warn('runTopPicks error:', e);
  } finally {
    if(nf) nf.innerHTML = origNF;
    renderNoteStats();
  }
}

async function findBook(){
  const btn = document.getElementById('findBookBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Ищу книгу…';

  try {
    const rawTitle   = document.getElementById('noteTitleField').value.trim();
    const noteEditor = document.getElementById('noteEditor');
    const noteText   = noteEditor.innerText.trim();

    if(!rawTitle){
      alert('Сначала введите заголовок заметки.');
      return;
    }

    const r   = await fetch('/api/find_book', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({title: rawTitle, note_text: noteText})
    });
    const res = await r.json();

    if(res.error){
      alert('Не удалось найти книгу:\n' + res.error);
      return;
    }

    // Build the link HTML: 📖 Title — Author (Source)
    const sourceLabel = res.source === 'gutenberg' ? 'Project Gutenberg' : 'Archive.org';
    const authorPart  = res.author ? ` — ${res.author}` : '';
    const linkHtml    = `📖 <a href="${res.url}" target="_blank">${res.title}${authorPart} [${sourceLabel}]</a>`;

    // Append to end of editor content with a line break
    noteEditor.focus();
    const br = noteEditor.innerHTML.trim().endsWith('<br>') ? '' : '<br>';
    noteEditor.innerHTML += br + linkHtml;

    btn.textContent = '✓ Книга добавлена';
    setTimeout(()=>{ btn.textContent = '📚 Найти книгу'; }, 3000);

  } catch(e){
    alert('Ошибка запроса: ' + e.message);
    btn.textContent = '📚 Найти книгу';
  } finally {
    btn.disabled = false;
  }
}

async function autoEmojiTitle(){
  const field = document.getElementById('noteTitleField');
  const btn   = document.getElementById('autoEmojiBtn');
  const title = field.value.trim();
  if(!title){ field.focus(); field.placeholder='Сначала введите заголовок…'; return; }

  btn.disabled = true;
  btn.textContent = '⏳…';

  try {
    const r = await fetch('/api/emoji_title', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({title})
    });
    const res = await r.json();
    if(res.error){
      alert('Ошибка: ' + res.error);
    } else {
      // Strip existing leading/trailing emojis to avoid doubling
      const stripped = title.replace(/^[\p{Emoji}\s]+|[\p{Emoji}\s]+$/gu, '').trim();
      field.value = (res.start||'') + ' ' + stripped + ' ' + (res.end||'');
      field.value = field.value.trim();
    }
  } catch(e) {
    alert('Ошибка запроса: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ AI эмодзи';
  }
}

async function fetchWikiImages(){
  const btn = document.getElementById('fetchImgBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Ищу…';

  try {
    // Collect Wikipedia URLs from the note editor HTML
    const editor = document.getElementById('noteEditor');
    const editorHtml = editor.innerHTML || '';
    // Also include the entry's own wiki URL (_noteKey)
    const urlSet = new Set();
    if(_noteKey) urlSet.add(_noteKey);
    // Extract all Wikipedia links from editor content
    const linkRe = /href="(https?:\/\/en\.wikipedia\.org\/wiki\/[^"#]+)"/g;
    let m;
    while((m = linkRe.exec(editorHtml)) !== null){
      urlSet.add(m[1]);
    }
    const wiki_urls = [...urlSet].slice(0, 5);

    // Strip emojis from title for semantic matching
    const rawTitle = document.getElementById('noteTitleField').value.trim();
    const cleanTitle = rawTitle.replace(
      /[\u{1F300}-\u{1FFFF}\u{2700}-\u{27BF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA9F}\u2600-\u26FF\u2702-\u27B0]+/gu,
      '').trim();
    const dv = document.getElementById('datePicker').value;

    btn.textContent = '⏳ Groq выбирает…';
    const r = await fetch('/api/fetch_wiki_images', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        wiki_urls,
        title: cleanTitle || rawTitle,
        date: dv,
        wiki_key: _noteKey
      })
    });
    const res = await r.json();
    if(res.error){
      alert('Ошибка: ' + res.error);
      return;
    }
    if(!res.images || !res.images.length){
      alert('Не удалось загрузить изображения. Попробуйте позже.');
      return;
    }

    // Append downloaded images to the note's current image list in DOM
    const wrap = document.getElementById('noteImgs');
    const captions = res.captions || {};
    res.images.forEach(url => {
      const caption = captions[url] || '';
      _addImgThumb(wrap, url, caption);
    });

    btn.textContent = `✓ ${res.images.length} фото добавлено`;
    setTimeout(()=>{ btn.textContent='🖼️ Фото из Wiki'; }, 3000);

  } catch(e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function clearNoteFormat(){
  document.execCommand('removeFormat');
}

// ── TAGS ──────────────────────────────────────────────────────────────────────

let _tags = [];       // current tag list while modal is open
let _pubStatus = 'draft';  // 'draft' | 'ready' | 'published'

function detectAutoTags(wikiKey){
  const tags = [];
  if(!data || !wikiKey) return tags;

  // Unified tag names by stable category id — same vocabulary across
  // events, births and holidays.
  const idMap = {
    science:     'Science',
    art:         'Art',
    education:   'Education',
    literature:  'Literature',
    scientists:  'Science',
    artists:     'Art',
    composers:   'Music',
    inventors:   'Inventions',
    cinema:      'Art',
    writers:     'Literature',
  };

  // Event categories
  (data.events||[]).forEach(cat=>{
    if(cat.entries && cat.entries.some(e=>e.includes(wikiKey)))
      tags.push(idMap[cat.id] || cat.label);
  });
  // Birth categories
  (data.births||[]).forEach(cat=>{
    if(cat.entries && cat.entries.some(e=>e.includes(wikiKey)))
      tags.push(idMap[cat.id] || cat.label);
  });
  // Holidays
  if((data.holidays||[]).some(item=>{
    const text=typeof item==='string'?item:(item.text||'');
    const children=typeof item==='object'?(item.children||[]):[];
    return text.includes(wikiKey)||children.some(c=>c.includes(wikiKey));
  })) tags.push('Holiday');

  return [...new Set(tags)];
}

// Quick-pick suggestions shown in the tag editor — same vocabulary
// used everywhere, plus a few extra topics not tied to a category.
const TAG_SUGGESTIONS = [
  'Science','Art','Music','Education','Literature',
  'Exploration','Inventions','Space','Holiday'
];

function renderTagList(){
  const wrap = document.getElementById('tagList');
  if(!wrap) return;
  wrap.innerHTML = '';
  _tags.forEach((tag, i) => {
    const el = document.createElement('span');
    el.className = 'tag';
    el.innerHTML = `${tag} <button class="tag-del" onclick="removeTag(${i})" title="Удалить">✕</button>`;
    wrap.appendChild(el);
  });
  renderTagSuggestions();
  // Refresh suggested emoji whenever tags change
  if(typeof EMOJI_CATS !== 'undefined'){
    const sugSet = new Set();
    _tags.forEach(t => (TAG_EMOJI_MAP[t]||[]).forEach(e => sugSet.add(e)));
    EMOJI_CATS[0].emojis = [...sugSet].map(e=>[e,'']);
    if(typeof _emojiActiveTab !== 'undefined'){
      if(_emojiActiveTab === 'suggested' || !EMOJI_CATS[0].emojis.length) renderEmojiGrid();
      renderEmojiTabs();
    }
  }
}

function renderTagSuggestions(){
  const wrap = document.getElementById('tagSuggestions');
  if(!wrap) return;
  wrap.innerHTML = '';
  TAG_SUGGESTIONS.filter(t=>!_tags.includes(t)).forEach(tag=>{
    const el = document.createElement('span');
    el.className = 'tag';
    el.style.cursor = 'pointer';
    el.style.opacity = '0.65';
    el.title = 'Добавить тег';
    el.textContent = '+ ' + tag;
    el.onclick = () => { _tags.push(tag); renderTagList(); };
    wrap.appendChild(el);
  });
}

function addTag(){
  const inp = document.getElementById('tagInput');
  const val = inp.value.trim();
  if(val && !_tags.includes(val)){
    _tags.push(val);
    renderTagList();
  }
  inp.value = '';
  inp.focus();
}

function removeTag(idx){
  _tags.splice(idx, 1);
  renderTagList();
}

function setPubStatus(status){
  _pubStatus = status;
  ['draft','ready','published'].forEach(s=>{
    const btn=document.getElementById('s'+s.charAt(0).toUpperCase()+s.slice(1));
    if(btn) btn.className=(s===status?'active-'+s:'');
  });
  document.getElementById('pubDateUrl').style.display=(status==='published'?'flex':'none');
}

async function saveNote(){
  const text=document.getElementById('noteEditor').innerHTML.trim();
  const title=document.getElementById('noteTitleField').value.trim();

  // Collect current images and captions from DOM thumbs
  const currentImgs=[];
  const currentCaptions={};
  document.getElementById('noteImgs').querySelectorAll('.note-img-thumb img').forEach(img=>{
    try {
      const relUrl = new URL(img.src).pathname;
      currentImgs.push(relUrl);
      const cap = img.getAttribute('data-caption')||'';
      if(cap) currentCaptions[relUrl] = cap;
    } catch(e){
      currentImgs.push(img.src);
    }
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

  const note={title, text, section: _noteSection, tags: [..._tags],
    images: currentImgs,
    image_captions: currentCaptions,
    pub_status: _pubStatus,
    ...(published?{published}:{})};;

  const r=await fetch('/api/notes',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:_noteDate, key:_noteKey, note})});
  const res=await r.json();
  if(res.ok){
    notesData[_noteKey]=note;
    renderNoteStats();
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
  renderNoteStats();
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
    const isGroq = id === 'groq';
    const overrideKey = meta.model_override_key || null;
    const curModel = overrideKey ? (keys[overrideKey] || meta.model || '') : '';

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

    if(isGroq && overrideKey){
      h += `<div style="margin-top:10px">
        <div style="font-size:12px;color:var(--tx2);margin-bottom:5px">Модель (оставьте пустым для дефолтной):</div>
        <input type="text" id="groqModelInput" value="${esc(curModel)}"
          placeholder="${meta.model}"
          style="width:100%;font-family:monospace;font-size:12px;padding:5px 8px;
                 border:1px solid var(--bd);border-radius:4px;background:var(--sf)">
        <div style="font-size:11px;color:var(--tx2);margin-top:4px">
          Доступные модели: <a href="https://console.groq.com/docs/models" target="_blank"
            style="color:var(--ai)">console.groq.com/docs/models</a>
          &nbsp;·&nbsp; Попробуйте: <code>llama-3.1-8b-instant</code> · <code>llama-3.3-70b-versatile</code> · <code>llama-4-scout-17b-16e-instruct</code>
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

  // Save AI keys and model overrides
  const newKeys = {};
  const providers = keysData.providers || {};
  for(const id of Object.keys(providers)){
    const inp = document.getElementById('key_'+id);
    if(inp) newKeys[id] = inp.value;
  }
  // OpenRouter model override
  const orModel = document.getElementById('orModelInput');
  if(orModel && orModel.value.trim()) newKeys['openrouter_model'] = orModel.value.trim();
  else if(orModel) newKeys['openrouter_model'] = '';
  // Groq model override
  const groqModel = document.getElementById('groqModelInput');
  if(groqModel) newKeys['groq_model'] = groqModel.value.trim();
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

        elif self.path == '/api/top_picks':
            try:
                payload = json.loads(body)
                events_list   = payload.get("events", [])
                births_by_cat = payload.get("births_by_cat", {})
                key, model, _uprov = _get_utility_ai()
                if not key:
                    self._json({"error": "No AI key set. Add Groq or OpenRouter key in ⚙ Settings → AI."}); return

                def pick(prompt_user, sys_content, max_tok=30):
                    """Single AI pick call, returns raw text."""
                    r = _utility_chat(
                        [{"role": "system", "content": sys_content},
                         {"role": "user",   "content": prompt_user}],
                        key, model, _uprov, max_tokens=max_tok
                    )
                    return re.sub(r"```[a-z]*\n?|\n?```", "",
                                  str(r["choices"][0]["message"]["content"] or "")).strip()

                def parse_int_array(text, max_val):
                    if not text or text == 'None':
                        return []
                    # First try proper JSON array
                    m = re.search(r"\[[\s\S]*?\]", text)
                    if m:
                        try:
                            result = [x for x in json.loads(m.group())
                                      if isinstance(x, int) and 0 <= x < max_val]
                            if result:
                                return result
                        except Exception:
                            pass
                    # Fallback: extract all standalone integers from text
                    nums = [int(x) for x in re.findall(r'\b(\d+)\b', text)
                            if 0 <= int(x) < max_val]
                    return list(dict.fromkeys(nums))  # deduplicate preserving order

                result = {}

                # ── Top event ────────────────────────────────────────────────
                if events_list:
                    numbered = "\n".join(f"{e['idx']}: {e['text'][:120]}" for e in events_list[:60])
                    text = pick(
                        f"Pick the index of the single most globally significant historical event:\n{numbered}\n\nReturn JSON array with 1 integer, e.g. [4]",
                        "You are a historian. Return ONLY a JSON array with exactly 1 integer index. Nothing else.",
                        max_tok=15
                    )
                    idxs = parse_int_array(text, len(events_list))
                    result["event_indices"] = [events_list[i]["idx"] for i in idxs[:1]]

                # ── Top 2 per birth category: parallel threads ───────────────
                cat_labels = {
                    "scientists": "scientists and academics",
                    "artists":    "visual artists and painters",
                    "composers":  "composers and musicians",
                    "inventors":  "inventors and engineers",
                    "cinema":     "film directors and actors",
                    "writers":    "authors and writers",
                }
                cat_results = {}

                import time as _time

                def pick_cat_sequential(cat_id, entries):
                    if not entries:
                        return cat_id, []
                    n = min(2, len(entries))
                    label = cat_labels.get(cat_id, cat_id)
                    numbered = "\n".join(
                        f"{e['idx']}: {e['text'][:100]}" for e in entries[:30]
                    )
                    user_msg = (
                        f"From these {label}, which {n} are most globally famous worldwide?\n\n"
                        f"{numbered}\n\n"
                        f"Answer with the {n} index numbers only, like: {n} {n+1}"
                    )
                    for attempt in range(3):
                        try:
                            if attempt > 0:
                                _time.sleep(1.5)
                            r = _utility_chat(
                                [{"role": "user", "content": user_msg}],
                                key, model, _uprov,
                                max_tokens=60, temperature=0
                            )
                            raw_text = str(r["choices"][0]["message"]["content"] or "").strip()
                            text = re.sub(r"```[a-z]*\n?|\n?```", "", raw_text).strip()
                            idxs = parse_int_array(text, len(entries))
                            idxs = list(dict.fromkeys(idxs))[:n]
                            print(f"  [top_picks] {cat_id} attempt {attempt+1}: {text!r} -> {idxs}",
                                  flush=True)
                            if idxs:
                                return cat_id, idxs
                        except Exception as ex:
                            print(f"  [top_picks] {cat_id} attempt {attempt+1} error: {ex}", flush=True)
                    return cat_id, []

                for cat_id, ents in births_by_cat.items():
                    if ents:
                        _time.sleep(0.3)  # small gap between calls
                        cid, idxs = pick_cat_sequential(cat_id, ents)
                        cat_results[cid] = idxs
                        print(f"[top_picks] {cid} -> {idxs}", flush=True)

                result["birth_indices_by_cat"] = cat_results
                self._json({"ok": True, **result})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/find_book':
            try:
                payload    = json.loads(body)
                title      = payload.get("title", "").strip()
                note_text  = payload.get("note_text", "").strip()
                key, model, _uprov = _get_utility_ai()
                if not key:
                    self._json({"error": "No AI key set. Add Groq or OpenRouter key in ⚙ Settings → AI."}); return
                if not title:
                    self._json({"error": "Note title is empty."}); return

                # ── Step 1: Ask Groq to suggest search queries ────────────────
                clean_title = re.sub(
                    r'[\U0001F300-\U0001FFFF\U00002700-\U000027BF'
                    r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA9F'
                    r'\u2600-\u26FF\u2702-\u27B0]+', '', title).strip()

                groq_resp = _utility_chat(
                    [{"role": "system", "content":
                      "You suggest book search queries for Archive.org and Project Gutenberg. "
                      "Return ONLY a JSON object with keys: "
                      "\"author\" (author name if person, else empty string), "
                      "\"queries\" (list of 3 search strings, from specific to broad), "
                      "\"prefer_gutenberg\" (true if author died before 1928). "
                      "No explanation, no markdown."},
                     {"role": "user", "content":
                      f"Note title: {clean_title}\n"
                      f"Context: {note_text[:300] if note_text else 'none'}"}],
                    key, model, _uprov, max_tokens=300, temperature=0.3
                )
                resp_text = str(groq_resp["choices"][0]["message"]["content"] or "")
                resp_text = re.sub(r"```[a-z]*\n?|\n?```", "", resp_text).strip()
                m = re.search(r"\{[\s\S]*\}", resp_text)
                if not m:
                    # Fallback: use title directly as search query
                    suggestions = {}
                else:
                    try:
                        suggestions = json.loads(m.group())
                    except Exception:
                        suggestions = {}

                queries          = suggestions.get("queries", [clean_title, clean_title.split()[0] if clean_title else ""])[:3]
                queries          = [q for q in queries if q] or [clean_title]
                prefer_gutenberg = bool(suggestions.get("prefer_gutenberg", False))
                author           = suggestions.get("author", "").strip()

                # ── Step 2: Search Gutenberg (for old texts) ──────────────────
                def search_gutenberg(q):
                    enc_q = url_quote(q)
                    api   = f"https://gutendex.com/books/?search={enc_q}&languages=en"
                    req   = urllib.request.Request(api, headers={"User-Agent": _UA})
                    with urllib.request.urlopen(req, timeout=12) as r:
                        data = json.loads(r.read().decode())
                    results = []
                    for book in data.get("results", [])[:6]:
                        authors = ", ".join(a.get("name","") for a in book.get("authors",[]))
                        formats = book.get("formats", {})
                        url = (formats.get("text/html","") or
                               formats.get("application/epub+zip","") or
                               f"https://www.gutenberg.org/ebooks/{book['id']}")
                        results.append({
                            "title":  book.get("title",""),
                            "author": authors,
                            "url":    url,
                            "source": "gutenberg",
                        })
                    return results

                # ── Step 3: Search Archive.org ────────────────────────────────
                def search_archive(q, author_q=""):
                    # Build a simple query — don't over-filter, Archive.org
                    # search is sensitive to syntax
                    parts = [f'"{q}"', "mediatype:texts", "language:English"]
                    if author_q:
                        parts.append(f'creator:("{author_q}")')
                    query_str = " AND ".join(parts)
                    params = urllib.parse.urlencode({
                        "q":        query_str,
                        "fl[]":     "identifier,title,creator,lending_status,subject",
                        "sort[]":   "downloads desc",
                        "rows":     "8",
                        "output":   "json",
                    }, doseq=True)
                    api = f"https://archive.org/advancedsearch.php?{params}"
                    req = urllib.request.Request(api, headers={"User-Agent": _UA})
                    with urllib.request.urlopen(req, timeout=14) as r:
                        data = json.loads(r.read().decode())
                    results = []
                    for doc in data.get("response", {}).get("docs", []):
                        ident = doc.get("identifier","")
                        if not ident:
                            continue
                        ls = doc.get("lending_status","")
                        # Accept any result — open, borrowable, or unspecified
                        results.append({
                            "title":  doc.get("title", ident),
                            "author": doc.get("creator", ""),
                            "url":    f"https://archive.org/details/{ident}",
                            "source": "archive",
                            "lending": ls,
                        })
                    return results

                def search_archive_simple(q):
                    """Fallback: broader search without author filter."""
                    params = urllib.parse.urlencode({
                        "q":      f"{q} mediatype:texts language:English",
                        "fl[]":   "identifier,title,creator,lending_status",
                        "sort[]": "downloads desc",
                        "rows":   "8",
                        "output": "json",
                    }, doseq=True)
                    api = f"https://archive.org/advancedsearch.php?{params}"
                    req = urllib.request.Request(api, headers={"User-Agent": _UA})
                    with urllib.request.urlopen(req, timeout=14) as r:
                        data = json.loads(r.read().decode())
                    results = []
                    for doc in data.get("response", {}).get("docs", []):
                        ident = doc.get("identifier","")
                        if ident:
                            results.append({
                                "title":  doc.get("title", ident),
                                "author": doc.get("creator", ""),
                                "url":    f"https://archive.org/details/{ident}",
                                "source": "archive",
                            })
                    return results

                # ── Step 4: Collect candidates ────────────────────────────────
                candidates = []
                for q in queries:
                    if prefer_gutenberg or not candidates:
                        try:
                            candidates += search_gutenberg(q)
                        except Exception:
                            pass
                    try:
                        candidates += search_archive(q, author)
                    except Exception:
                        pass
                    if len(candidates) >= 6:
                        break

                # Fallback: try simpler Archive.org queries without author
                if len(candidates) < 3:
                    for q in queries:
                        try:
                            candidates += search_archive_simple(q)
                        except Exception:
                            pass
                        if len(candidates) >= 6:
                            break

                # Deduplicate by URL
                seen_urls = set()
                unique = []
                for c in candidates:
                    if c["url"] not in seen_urls:
                        seen_urls.add(c["url"])
                        unique.append(c)
                candidates = unique

                if not candidates:
                    self._json({"error":
                        f"No books found for «{clean_title}» on Archive.org or Project Gutenberg. "
                        f"You can search manually at https://archive.org/search?query={url_quote(clean_title)}"
                        f" or https://gutenberg.org/ebooks/search/?query={url_quote(clean_title)}"}); return

                # ── Step 5: Ask Groq to pick the best book ────────────────────
                book_list = "\n".join(
                    f"{i}: [{c['source'].upper()}] {c['title'][:80]}"
                    f" — {c['author'][:50]}"
                    for i, c in enumerate(candidates[:12])
                )
                pick_resp = _utility_chat(
                    [{"role": "system",
                      "content": "Return ONLY a JSON object: {\"index\": N} — the best book index. No other text."},
                     {"role": "user",
                      "content":
                      f"Note topic: {clean_title}\n\n"
                      f"Books:\n{book_list}\n\n"
                      "Pick the most relevant and reputable book. Prefer the author's own works or works about them. "
                      "Prefer full texts over partial. Return JSON."}],
                    key, model, _uprov, max_tokens=30
                )
                pick_text = str(pick_resp["choices"][0]["message"]["content"] or "")
                pick_text = re.sub(r"```[a-z]*\n?|\n?```", "", pick_text).strip()
                pm = re.search(r"\{[\s\S]*?\}", pick_text)
                chosen_idx = 0
                if pm:
                    try:
                        chosen_idx = int(json.loads(pm.group()).get("index", 0))
                    except Exception:
                        pass
                chosen_idx = max(0, min(chosen_idx, len(candidates)-1))
                book = candidates[chosen_idx]

                self._json({"ok": True,
                            "url":    book["url"],
                            "title":  book["title"],
                            "author": book["author"],
                            "source": book["source"]})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/fetch_wiki_images':
            try:
                payload    = json.loads(body)
                wiki_urls  = payload.get("wiki_urls", [])   # list of wikipedia article URLs
                title      = payload.get("title", "")       # note title (no emojis)
                date_str   = payload.get("date", "")
                wiki_key   = payload.get("wiki_key", "")
                key, model, _uprov = _get_utility_ai()
                if not key:
                    self._json({"error": "No AI key set. Add Groq or OpenRouter key in ⚙ Settings → AI."}); return
                if not wiki_urls:
                    self._json({"error": "No Wikipedia URLs found in note text."}); return

                # ── Step 1: Fetch images from each Wikipedia page ──────────────
                import html as html_mod
                from html.parser import HTMLParser as HP

                class ImgParser(HP):
                    def __init__(self):
                        super().__init__()
                        self.imgs = []   # list of {src, alt}
                        self.in_figure = False
                        self.caption = ""
                        self.last_alt = ""

                    def handle_starttag(self, tag, attrs):
                        ad = dict(attrs)
                        if tag == "figure":
                            self.in_figure = True
                        elif tag == "img":
                            src = ad.get("src", "")
                            alt = ad.get("alt", "") or ad.get("data-alt", "")
                            # Only full-size Wikipedia images (skip thumbs <100px)
                            if src and "wikimedia.org" in src:
                                # Prefer /wiki/Special:FilePath or //upload.wikimedia
                                if src.startswith("//"):
                                    src = "https:" + src
                                self.imgs.append({"src": src, "alt": alt})
                                self.last_alt = alt

                    def handle_endtag(self, tag):
                        if tag == "figure":
                            self.in_figure = False

                    def handle_data(self, data):
                        pass

                all_candidates = []   # {src, alt, page}
                seen_srcs = set()

                for url in wiki_urls[:4]:   # limit pages checked
                    try:
                        page_title = url.split("/wiki/")[-1]
                        # Use Wikipedia REST API for images — faster and structured
                        api_url = (f"https://en.wikipedia.org/w/api.php"
                                   f"?action=query&titles={page_title}"
                                   f"&prop=images&imlimit=30&format=json")
                        req = urllib.request.Request(
                            api_url, headers={"User-Agent": _UA})
                        with urllib.request.urlopen(req, timeout=12) as resp:
                            wiki_data = json.loads(resp.read().decode())
                        pages = wiki_data.get("query", {}).get("pages", {})
                        for page in pages.values():
                            for img in page.get("images", []):
                                fname = img.get("title", "").replace("File:", "")
                                if not fname: continue
                                # Skip icons/flags/small decoratives
                                low = fname.lower()
                                if any(x in low for x in [
                                    "icon", "flag_of", "commons-logo", "wiki",
                                    "edit-", "disambig", "cscr", "featured",
                                    "padlock", "question", "star", "speak",
                                    ".svg", "lock", "button", "arrow"
                                ]): continue
                                # Build Wikimedia Commons URL
                                import hashlib as hl
                                fname_enc = fname.replace(" ", "_")
                                md5 = hl.md5(fname_enc.encode()).hexdigest()
                                src = (f"https://upload.wikimedia.org/wikipedia/commons/"
                                       f"{md5[0]}/{md5[:2]}/{url_quote(fname_enc)}")
                                if src not in seen_srcs:
                                    seen_srcs.add(src)
                                    all_candidates.append({
                                        "src": src,
                                        "alt": fname.replace("_", " ").rsplit(".", 1)[0],
                                        "page": page_title
                                    })
                    except Exception:
                        continue

                if not all_candidates:
                    self._json({"error": "No suitable images found on Wikipedia pages."}); return

                # ── Step 2: Ask Groq to pick 3-7 most relevant images ──────────
                # Clean title: remove emojis
                clean_title = re.sub(
                    r'[\U0001F300-\U0001FFFF\U00002700-\U000027BF'
                    r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA9F'
                    r'\u2600-\u26FF\u2702-\u27B0]+', '', title).strip()

                img_list = "\n".join(
                    f"{i}: {c['alt']} (from {c['page']})"
                    for i, c in enumerate(all_candidates[:40])
                )
                groq_resp = _utility_chat(
                    [{"role": "system",
                      "content": (
                          "You select images for a historical note. "
                          "Return ONLY a JSON array of indices (integers, 3-7 items). "
                          "Choose images that best illustrate the topic, "
                          "are diverse (not duplicates of the same thing), "
                          "and are visually informative. No markdown, no explanation."
                      )},
                     {"role": "user",
                      "content": (
                          f"Note topic: {clean_title}\n\n"
                          f"Available images:\n{img_list}\n\n"
                          f"Return JSON array of 3-7 best indices."
                      )}],
                    key, model, _uprov, max_tokens=150
                )
                resp_text = str(groq_resp["choices"][0]["message"]["content"] or "")
                resp_text = re.sub(r"```[a-z]*\n?|\n?```", "", resp_text).strip()
                m = re.search(r"\[[\s\S]*?\]", resp_text)
                chosen_indices = []
                if m:
                    try:
                        chosen_indices = [
                            x for x in json.loads(m.group())
                            if isinstance(x, int) and x < len(all_candidates)
                        ][:7]
                    except Exception:
                        pass
                if not chosen_indices:
                    chosen_indices = list(range(min(3, len(all_candidates))))

                # ── Step 3: Download chosen images and save ────────────────────
                saved_urls    = []
                saved_captions = {}   # {url: alt_text}
                for idx in chosen_indices:
                    cand = all_candidates[idx]
                    try:
                        img_req = urllib.request.Request(
                            cand["src"],
                            headers={"User-Agent": _UA,
                                     "Referer": "https://en.wikipedia.org"})
                        with urllib.request.urlopen(img_req, timeout=15) as resp:
                            img_bytes = resp.read()
                        if len(img_bytes) < 5000:
                            continue
                        ext = "." + cand["src"].rsplit(".", 1)[-1].split("?")[0].lower()
                        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            ext = ".jpg"
                        saved_url = image_save(date_str, wiki_key,
                                               f"wiki_{idx}{ext}", img_bytes)
                        saved_urls.append(saved_url)
                        if cand.get("alt"):
                            saved_captions[saved_url] = cand["alt"]
                    except Exception:
                        continue

                self._json({"ok": True, "images": saved_urls,
                            "captions": saved_captions,
                            "count": len(saved_urls)})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/emoji_title':
            try:
                payload = json.loads(body)
                title   = payload.get("title", "").strip()
                if not title:
                    self._json({"error": "empty title"}); return
                key, model, _uprov = _get_utility_ai()
                if not key:
                    self._json({"error": "No AI key set. Add Groq or OpenRouter key in ⚙ Settings → AI."}); return

                def try_get_emojis(prompt_text, sys_text):
                    r = _utility_chat(
                        [{"role": "system", "content": sys_text},
                         {"role": "user",   "content": prompt_text}],
                        key, model, _uprov, max_tokens=80, temperature=0.7
                    )
                    text = str(r["choices"][0]["message"]["content"] or "").strip()
                    text = re.sub(r"```[a-z]*\n?|\n?```", "", text).strip()
                    return text

                # Try primary prompt
                prompt = (
                    f"Title: \"{title}\"\n"
                    "Pick 4 emojis matching the meaning: 2 for start, 2 for end.\n"
                    'Return JSON only: {"start":"🔬⚗️","end":"🌟💡"}'
                )
                text = try_get_emojis(prompt, "Respond only with a JSON object.")

                m = re.search(r"\{[\s\S]*?\}", text)
                if m:
                    try:
                        data = json.loads(m.group())
                        start = str(data.get("start") or "")
                        end   = str(data.get("end")   or "")
                        if start or end:
                            self._json({"ok": True, "start": start, "end": end}); return
                    except Exception:
                        pass

                # Fallback: ask for a single emoji string and split it
                text2 = try_get_emojis(
                    f"Give exactly 4 emojis for this topic: {title[:80]}\n"
                    "Reply with ONLY 4 emojis, nothing else.",
                    "You output only emojis."
                )
                # Extract all emoji-like characters
                import unicodedata
                emojis = [c for c in text2
                          if unicodedata.category(c) in ('So','Sm','Sk','Cs')
                          or ord(c) > 0x1F300][:4]
                if len(emojis) >= 2:
                    self._json({"ok": True,
                                "start": "".join(emojis[:2]),
                                "end":   "".join(emojis[2:4])})
                else:
                    self._json({"error": f"Could not get emojis from model. Response: {text[:80]}"})
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

                events_raw, births_raw, holidays_raw = parse_wikipedia_raw(html_content)
                _raw_store["events_raw"] = events_raw
                _raw_store["births_raw"] = births_raw
                run_job(events_raw, births_raw, holidays_raw, cfg,
                        use_ai_events=use_ai_events,
                        use_ai_births=use_ai_births,
                        ai_provider=ai_provider)

                self._json({
                    "ok": True,
                    "wiki_url": wiki_url,
                    "wiki_title": title.replace("_", " "),
                    "total_events": len(events_raw),
                    "total_births": len(births_raw),
                    "total_holidays": len(holidays_raw),
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
