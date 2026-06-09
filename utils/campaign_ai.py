"""Campaign Processor AI helpers (NVIDIA-hosted GLM, OpenAI-compatible).

Used to (a) pick the best keywords to target from the pooled POE / Helium10
Reverse ASIN / Brand Analytics candidates, (b) map each keyword to a Campaign
Root KW category, and (c) suggest SKW vs MKW and a match type.

Every function degrades to a deterministic heuristic if the endpoint is
unreachable, so the processor always produces a workbook.
"""

from __future__ import annotations

import json
import re

import requests

from config import cfg

_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


class CampaignAIError(RuntimeError):
    pass


def available() -> bool:
    return bool(cfg.CAMPAIGN_AI_KEY and cfg.CAMPAIGN_AI_URL)


def chat(messages, temperature=0.2, max_tokens=2000):
    if not available():
        raise CampaignAIError("Campaign AI not configured")
    url = cfg.CAMPAIGN_AI_URL + "/chat/completions"
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {cfg.CAMPAIGN_AI_KEY}"
    payload = {
        "model": cfg.CAMPAIGN_AI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        # Bounded so a slow/overloaded model degrades to the heuristic quickly
        # instead of stalling the whole build for minutes.
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=35)
    except requests.RequestException as e:
        raise CampaignAIError(f"request failed: {e}") from e
    if r.status_code >= 400:
        raise CampaignAIError(f"{r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise CampaignAIError(f"bad response: {json.dumps(data)[:300]}") from e


def _json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return []


# --------------------------------------------------------------------------- #
def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def categorize(keywords, categories):
    """Map each keyword to one Campaign Root KW category.

    categories: list like ["0-Gen","1-Gift","2-Accessories","3-Training Aids","4-Putter"].
    Returns {keyword: category}. Heuristic: match a category's descriptive token
    inside the keyword; fall back to the first non "0-Gen" category, else "0-Gen".
    """
    keywords = [k for k in dict.fromkeys(keywords) if k]
    if not keywords or not categories:
        return {k: (categories[0] if categories else "") for k in keywords}

    out = _categorize_heuristic(keywords, categories)
    if not available():
        return out
    prompt = (
        "Assign each Amazon keyword to exactly one category. Categories (use the "
        f"label verbatim): {json.dumps(categories)}.\n"
        '"0-Gen" = generic catch-all when nothing fits better.\n\n'
        f"Keywords: {json.dumps(keywords)}\n\n"
        'Reply ONLY a JSON array: [{"kw":"...","category":"<one label>"}]'
    )
    try:
        data = _json_array(chat([
            {"role": "system", "content": "You output only valid JSON arrays."},
            {"role": "user", "content": prompt},
        ]))
        valid = set(categories)
        for d in data:
            kw, cat = d.get("kw"), d.get("category")
            if kw in out and cat in valid:
                out[kw] = cat
    except CampaignAIError:
        pass
    return out


def _category_tokens(category):
    # "3-Training Aids" -> ["training","aids"]; "2-Accessories" -> ["accessories"]
    label = re.sub(r"^\d+\s*-\s*", "", category)
    return [t for t in re.split(r"[^a-z]+", label.lower()) if len(t) > 2]


def _categorize_heuristic(keywords, categories):
    cats = [(c, _category_tokens(c)) for c in categories]
    default = next((c for c in categories if not c.lower().endswith("gen")), categories[0])
    out = {}
    for kw in keywords:
        k = _norm(kw)
        best = None
        for cat, toks in cats:
            if toks and any(t in k for t in toks):
                best = cat
                break
        out[kw] = best or default
    return out


def derive_categories(keywords, product_context, max_cats=8):
    """Derive client-specific Campaign Root KW categories from the keyword pool.

    Returns a list like ["0-Gen","1-...","2-..."] — "0-Gen" is always the generic
    catch-all at index 0. Falls back to ["0-Gen"] if AI is unavailable.
    """
    keywords = [k for k in dict.fromkeys(keywords) if k]
    if not keywords or not available():
        return ["0-Gen"]
    sample = keywords[:200]
    prompt = (
        f"Product context: {product_context}\n\n"
        "These are Amazon keywords for this product line. Group them into a small set "
        f"of 3-{max_cats} ROOT CATEGORIES that describe the product's sub-themes "
        "(e.g. a gift angle, an accessory angle, a specific product variant). Keep them "
        "specific to THIS product line — do not invent unrelated categories.\n\n"
        f"Keywords: {json.dumps(sample)}\n\n"
        'Reply ONLY a JSON array of category label strings, generic catch-all first: '
        '["0-Gen","1-<theme>","2-<theme>",...]'
    )
    try:
        labels = [str(x).strip() for x in _json_array(chat([
            {"role": "system", "content": "You output only a valid JSON array of strings."},
            {"role": "user", "content": prompt},
        ])) if str(x).strip()]
    except CampaignAIError:
        return ["0-Gen"]
    if not labels:
        return ["0-Gen"]
    # Guarantee a generic catch-all at index 0.
    if not any(l.lower().endswith("gen") for l in labels):
        labels = ["0-Gen"] + labels
    return labels[:max_cats]


_STOP = {"for", "the", "and", "with", "your", "you", "men", "women", "best", "new",
         "set", "pack", "kit", "pro", "plus", "inch", "size", "pcs", "pack"}


def generate_roots(keywords, product_context, max_roots=12):
    """Generate up to `max_roots` ROOT keywords — the essential head term each
    keyword is built around (e.g. 'holder' for 'car phone holder'). Returns a list
    of lowercase root tokens. AI-first, frequency-heuristic fallback."""
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    if not kws:
        return []
    if available():
        prompt = (
            f"Product context: {product_context}\n\n"
            "Below are Amazon keywords. Identify up to "
            f"{max_roots} ROOT keywords — the single essential head word each phrase "
            "is built around and would be meaningless without (e.g. 'holder' in 'car "
            "phone holder', 'mirror' in 'golf putting mirror'). Prefer specific product "
            "nouns, not generic modifiers.\n\n"
            f"Keywords: {json.dumps(kws[:300])}\n\n"
            'Reply ONLY a JSON array of lowercase root words: ["holder","mount",...]'
        )
        try:
            roots = [str(x).strip().lower() for x in _json_array(chat([
                {"role": "system", "content": "You output only a JSON array of strings."},
                {"role": "user", "content": prompt},
            ])) if str(x).strip()]
            roots = [r for r in dict.fromkeys(roots) if r]
            if roots:
                return roots[:max_roots]
        except CampaignAIError:
            pass
    # Heuristic: most frequent meaningful token across keywords.
    from collections import Counter
    cnt = Counter()
    for k in kws:
        for w in dict.fromkeys(re.split(r"[^a-z0-9]+", k.lower())):
            if len(w) > 2 and not w.isdigit() and w not in _STOP:
                cnt[w] += 1
    return [w for w, n in cnt.most_common(max_roots) if n >= 2][:max_roots] or \
           [cnt.most_common(1)[0][0]] if cnt else []


def assign_root(keyword, roots, usage):
    """Pick the most relevant root contained in `keyword`, preferring a root not yet
    used (greedy-unique). `usage` is a dict root->count, mutated. Returns root or ''."""
    k = " " + re.sub(r"[^a-z0-9 ]", " ", keyword.lower()) + " "
    present = [r for r in roots if f" {r} " in k or k.strip().endswith(r)]
    if not present:
        present = [r for r in roots if r in k]
    if not present:
        return ""
    present.sort(key=lambda r: (usage.get(r, 0), len(r)))  # least-used, then shorter
    chosen = present[0]
    usage[chosen] = usage.get(chosen, 0) + 1
    return chosen


def classify_targets(keywords):
    """Suggest SKW vs MKW + match type for each keyword.

    Heuristic: 1-2 word phrases -> SKW Exact ("Ex."); 3+ words -> MKW Broad ("Br.").
    Returns {keyword: {"kw_type": "SKW"|"MKW", "match": "Ex."|"Br."}}.
    """
    out = {}
    for kw in keywords:
        words = _norm(kw).split()
        if len(words) <= 2:
            out[kw] = {"kw_type": "SKW", "match": "Ex."}
        else:
            out[kw] = {"kw_type": "MKW", "match": "Br."}
    return out


def select_keywords(candidates, product_context, limit=120):
    """Pick the best keywords to target from pooled candidates.

    candidates: list of {"keyword","source","search_volume"} dicts.
    Returns the selected subset (list of the same dicts), AI-ranked when possible,
    otherwise the highest-search-volume unique keywords.
    """
    # dedupe by normalised keyword, keep richest record
    by_kw = {}
    for c in candidates:
        k = _norm(c.get("keyword"))
        if not k:
            continue
        cur = by_kw.get(k)
        if cur is None or (c.get("search_volume") or 0) > (cur.get("search_volume") or 0):
            by_kw[k] = c
    uniq = list(by_kw.values())
    uniq.sort(key=lambda c: (c.get("search_volume") or 0), reverse=True)

    if not available() or len(uniq) <= limit:
        return uniq[:limit]

    # Ask AI to keep the most relevant/high-intent of the top pool.
    pool = uniq[: min(len(uniq), 300)]
    listing = [{"kw": c["keyword"], "sv": c.get("search_volume") or 0} for c in pool]
    prompt = (
        f"Product: {product_context}\n\n"
        "From the candidate keywords below, select the BEST ones to target in Amazon "
        "Sponsored Products campaigns: relevant to the product, real buyer intent, no "
        f"off-topic/competitor-brand terms. Return at most {limit}.\n\n"
        f"Candidates (kw, monthly search volume): {json.dumps(listing)}\n\n"
        'Reply ONLY a JSON array of the chosen keyword strings: ["kw1","kw2",...]'
    )
    try:
        text = chat([
            {"role": "system", "content": "You output only a valid JSON array of strings."},
            {"role": "user", "content": prompt},
        ], max_tokens=4000)
        chosen = {_norm(x) for x in _json_array(text) if isinstance(x, str)}
        picked = [c for c in pool if _norm(c["keyword"]) in chosen]
        return picked[:limit] if picked else uniq[:limit]
    except CampaignAIError:
        return uniq[:limit]
