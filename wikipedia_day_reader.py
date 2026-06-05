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
        "name": "OpenRouter — Llama 3.1 8B (free)",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "free": True,
        "signup": "https://openrouter.ai/keys",
        "note": "Free models, no billing required",
    },
}

KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_keys.json")


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
        "You are a precise semantic classifier for historical Wikipedia entries.\n"
        "Return ONLY a JSON object mapping each entry index (string) to an array "
        "of matching category ids. Match by meaning. Empty array [] if none match.\n"
        'Example: {"0":["science"],"1":[],"2":["art","education"]}\n'
        "No markdown, no explanation — only the JSON object."
    )
    user = f"Categories:\n{cat_lines}\n\nEntries:\n{numbered}"
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
                         provider, key, model, progress_cb=None):
    fn = _AI_FNS.get(provider)
    if not fn:
        raise ValueError(f"Unknown provider: {provider}")
    result = {c["id"]: [] for c in categories}
    if include_russian:
        result["russians"] = []
    n = len(entries)
    batch_size = _AI_BATCH.get(provider, AI_BATCH)
    for start in range(0, n, batch_size):
        batch   = entries[start:start + batch_size]
        mapping = fn(batch, categories, russian_desc, include_russian, key, model)
        for idx_s, cats in mapping.items():
            idx = int(idx_s)
            if idx < len(batch):
                for cid in (cats or []):
                    if cid in result:
                        result[cid].append(batch[idx])
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


def run_job(events_raw, births_raw, cfg, use_ai=False, ai_provider="gemini"):
    _job_queue.queue.clear()

    def push(obj):
        _job_queue.put(json.dumps(obj, ensure_ascii=False))

    def run():
        try:
            if use_ai:
                # ── AI path ──────────────────────────────────────────────
                key   = _keys_store.get(ai_provider, "").strip()
                model = AI_PROVIDERS[ai_provider]["model"]
                if not key:
                    raise ValueError(
                        f"API key for '{AI_PROVIDERS[ai_provider]['name']}' not set. "
                        f"Add it in ⚙ Settings → AI."
                    )
                push({"type": "status",
                      "text": f"AI-классификация событий ({AI_PROVIDERS[ai_provider]['name']})…"})
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
                push({"type": "status",
                      "text": f"AI-классификация рождений…"})
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
                # ── Local TF-IDF path ─────────────────────────────────────
                thr   = float(cfg.get("tfidf_threshold",  DEFAULT_CONFIG["tfidf_threshold"]))
                kwmin = int(cfg.get("keyword_min_hits",    DEFAULT_CONFIG["keyword_min_hits"]))

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
  /* AI toggle strip */
  .ai-strip{display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:var(--r);background:var(--sf2);border:1.5px solid var(--bd);transition:all .2s;}
  .ai-strip.active{background:#e8f2fb;border-color:#8ab8d8;}
  .ai-strip label{font-size:13px;color:var(--tx2);cursor:pointer;user-select:none;}
  .ai-strip.active label{color:var(--ai);font-weight:600;}
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
  .entry a{color:var(--ac);text-decoration:none;}
  .entry a:hover{text-decoration:underline;}
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
<header>
  <div>
    <h1>📖 Читатель Дней Истории</h1>
    <div class="sub" id="hdrSub">Локальная классификация · TF-IDF + словарь · Wikipedia</div>
  </div>
</header>
<div class="ctrl">
  <input type="date" id="datePicker"/>
  <button class="bp" id="loadBtn" onclick="loadData()">Загрузить</button>
  <!-- AI toggle -->
  <div class="ai-strip" id="aiStrip">
    <input type="checkbox" id="aiChk" onchange="onAiToggle()">
    <label for="aiChk">🤖 Внешний AI</label>
    <select id="aiProv" style="display:none" onchange="onProvChange()">
      <option value="gemini">Gemini 2.0 Flash</option>
      <option value="groq">Groq Llama 3.1</option>
      <option value="openrouter">OpenRouter (free)</option>
    </select>
  </div>
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

<script>
let config=null, data=null, sseSource=null, keysData={};
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
}

// ── AI TOGGLE ────────────────────────────────────────────────────────────────
function onAiToggle(){
  const on=document.getElementById('aiChk').checked;
  const strip=document.getElementById('aiStrip');
  const sel=document.getElementById('aiProv');
  strip.classList.toggle('active',on);
  sel.style.display=on?'':'none';
  const sub=document.getElementById('hdrSub');
  sub.textContent=on
    ? `AI классификация · ${providerName(sel.value)} · Wikipedia`
    : 'Локальная классификация · TF-IDF + словарь · Wikipedia';
}

function onProvChange(){
  const sel=document.getElementById('aiProv');
  const sub=document.getElementById('hdrSub');
  if(document.getElementById('aiChk').checked)
    sub.textContent=`AI классификация · ${providerName(sel.value)} · Wikipedia`;
}

function providerName(id){
  const names={gemini:'Gemini 2.0 Flash',groq:'Groq Llama 3.1',openrouter:'OpenRouter (free)'};
  return names[id]||id;
}

// ── FILTERS ───────────────────────────────────────────────────────────────────
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
  const useAi=document.getElementById('aiChk').checked;
  const aiProv=document.getElementById('aiProv').value;
  const btn=document.getElementById('loadBtn');
  btn.disabled=true;btn.textContent='Загрузка...';
  if(sseSource){sseSource.close();sseSource=null;}

  document.getElementById('content').innerHTML=
    `<div style="padding:10px 0;font-size:14px;color:var(--tx2);font-style:italic"><span class="sp"></span>Загружаем Wikipedia...</div>`;

  const r=await fetch('/api/start',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:dv,config,use_ai:useAi,ai_provider:aiProv})
  });
  const resp=await r.json();
  if(resp.error){
    document.getElementById('content').innerHTML=`<div class="err">${resp.error}</div>`;
    btn.disabled=false;btn.textContent='Загрузить';return;
  }

  const {wiki_url,wiki_title,total_events,total_births}=resp;
  const mode=useAi?`🤖 ${providerName(aiProv)}`:'локальная TF-IDF';
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
      updCounts(data);renderContent(data,mode);
      btn.disabled=false;btn.textContent='Загрузить';
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
    rSet.forEach(e=>{h+=`<div class="entry" style="border-left-color:#8b2d2d">${e}</div>`;});
    h+=`</div>`;
  }
  d.births.forEach((c,i)=>{if(!activeFilters.births[c.id])return;h+=sec(c,BC[i%BC.length],BI[i%BI.length],true,rSet);});
  h+=`</div>`;
  document.getElementById('content').innerHTML=h;
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
    h+=`<div class="entry" style="border-left-color:${color}">${e}${ru?'<span class="rb">🇷🇺 рус.</span>':''}</div>`;
  });
  return h+`</div>`;
}

function swTab(t){activeTab=t;if(data)renderContent(data);}

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
    h += `<div class="provider-card ${hasKey?'has-key':''}">
      <div class="prov-header">
        <span class="prov-name">${meta.name}</span>
        <span class="prov-free">Бесплатный</span>
        <span class="key-status ${hasKey?'set':'unset'}">${hasKey?'✓ ключ задан':'нет ключа'}</span>
      </div>
      <div class="prov-note">${meta.note}</div>
      <div class="prov-key-row">
        <input type="password" id="key_${id}" placeholder="Вставьте API ключ…"
          value="${keys[id]||''}"
          autocomplete="off">
        <a href="${meta.signup}" target="_blank">Получить ключ →</a>
      </div>
    </div>`;
  }
  document.getElementById('aiE').innerHTML = h;
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

  // Save AI keys (only send non-masked values)
  const newKeys = {};
  const providers = keysData.providers || {};
  for(const id of Object.keys(providers)){
    const inp = document.getElementById('key_'+id);
    if(inp) newKeys[id] = inp.value;
  }
  const kr = await fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(newKeys)});
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

        elif path == '/api/config':
            self._json(config_store)

        elif path == '/api/keys':
            # Return keys with values masked for display, plus provider metadata
            masked = {k: ("•" * 8 if v else "") for k, v in _keys_store.items()}
            self._json({
                "keys": masked,
                "providers": AI_PROVIDERS,
            })

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

        if self.path == '/api/config':
            try:
                config_store.update(json.loads(body))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/keys':
            # Save keys: only overwrite non-empty values so masking doesn't erase real keys
            try:
                incoming = json.loads(body)  # {provider: key_or_masked}
                for provider, value in incoming.items():
                    if value and not value.startswith("•"):
                        _keys_store[provider] = value.strip()
                    elif not value:
                        _keys_store.pop(provider, None)
                save_keys(_keys_store)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})

        elif self.path == '/api/start':
            try:
                payload    = json.loads(body)
                date_str   = payload["date"]
                cfg        = payload.get("config", config_store)
                use_ai     = bool(payload.get("use_ai", False))
                ai_provider = payload.get("ai_provider", "gemini")

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
                run_job(events_raw, births_raw, cfg, use_ai=use_ai, ai_provider=ai_provider)

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
