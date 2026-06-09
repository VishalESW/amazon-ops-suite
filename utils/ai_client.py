"""AI summary client.

Targets a configurable, OpenAI-compatible chat endpoint (cfg.AI_API_URL +
cfg.AI_CHAT_PATH) with a Bearer key and the ngrok skip-warning header. The
request/response shape is easy to adjust if the real endpoint differs.

summarize_keywords() turns a list of keyword-optimisation rows into a short,
plain-language brief for the dashboard. If the endpoint is unreachable it
degrades to a deterministic local summary so the UI still works.
"""

import json

import requests

from config import cfg

_HEADERS_BASE = {
    "ngrok-skip-browser-warning": "true",
    "Content-Type": "application/json",
}


class AIClientError(RuntimeError):
    pass


def _headers():
    h = dict(_HEADERS_BASE)
    if cfg.AI_API_KEY:
        h["Authorization"] = f"Bearer {cfg.AI_API_KEY}"
    return h


def chat(messages, temperature=0.3, max_tokens=700):
    """Call the chat endpoint. Returns assistant text. Raises AIClientError on failure."""
    if not cfg.AI_API_URL:
        raise AIClientError("AI_API_URL is not configured")
    url = cfg.AI_API_URL + cfg.AI_CHAT_PATH
    payload = {
        "model": cfg.AI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(url, headers=_headers(), data=json.dumps(payload), timeout=60)
    except requests.RequestException as e:
        raise AIClientError(f"AI request failed: {e}") from e
    if resp.status_code >= 400:
        raise AIClientError(f"AI endpoint {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    # OpenAI-compatible shape; fall back to a couple of common alternatives.
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        for key in ("content", "text", "response", "output"):
            if isinstance(data.get(key), str):
                return data[key].strip()
        raise AIClientError(f"Unexpected AI response shape: {json.dumps(data)[:300]}")


def summarize_keywords(rows, target_acos, target_cpa):
    """Build a plain-language brief of the proposed bid changes.

    rows: list of dicts from bid_optimizer.optimize (keyword, campaign, rule,
          current_bid, new_bid, acos, spend, sales, clicks, orders...).
    Returns a string (AI-generated, or a local fallback).
    """
    changed = [r for r in rows if r.get("new_bid") is not None and r.get("rule")]
    table = _compact_table(changed[:60])
    prompt = (
        "You are an Amazon PPC strategist. A bid optimizer proposed the keyword bid "
        f"changes below. Target ACOS={target_acos:.0%}, Target CPA=${target_cpa:.2f}. "
        "Write a concise dashboard brief (5-8 sentences) for the seller: summarize how "
        "many keywords change and why, total spend at risk, which campaigns are most "
        "affected, the dominant rule triggered, and one clear recommendation. Plain "
        "language, no markdown tables.\n\nData (JSON):\n" + table
    )
    try:
        return chat([
            {"role": "system", "content": "You summarize Amazon Ads bid changes clearly and briefly."},
            {"role": "user", "content": prompt},
        ])
    except AIClientError:
        return _local_summary(changed, target_acos, target_cpa)


def keyword_relevancy(terms, product_context):
    """Judge each search term's relevancy to the product. Returns
    {term: {"relevant": bool, "reason": str}}. Degrades to 'unknown' (relevant=True)
    if the AI endpoint is unreachable."""
    terms = [t for t in dict.fromkeys(terms) if t][:60]
    if not terms:
        return {}
    prompt = (
        "You are an Amazon PPC relevancy checker. Given the product context and a list "
        "of customer search terms, decide if each term is RELEVANT to advertising this "
        "product. Irrelevant = different product, competitor brand, or off-topic.\n\n"
        f"Product context: {product_context}\n\n"
        f"Search terms: {json.dumps(terms)}\n\n"
        'Reply with ONLY a JSON array: '
        '[{"term":"...","relevant":true,"reason":"short"}]'
    )
    try:
        text = chat([
            {"role": "system", "content": "You output only valid JSON arrays."},
            {"role": "user", "content": prompt},
        ], max_tokens=1500)
        start, end = text.find("["), text.rfind("]")
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else []
        return {d.get("term"): {"relevant": bool(d.get("relevant", True)),
                                "reason": d.get("reason", "")} for d in data if d.get("term")}
    except (AIClientError, ValueError, KeyError, TypeError):
        return {t: {"relevant": True, "reason": "AI unavailable"} for t in terms}


def keyword_brand_flags(terms, brand):
    """Classify each search term as branded (contains/targets the seller's brand)
    vs generic. Returns {term: bool}. Falls back to a brand-token heuristic if AI
    is unavailable."""
    terms = [t for t in dict.fromkeys(terms) if t][:90]
    if not terms:
        return {}

    def _heuristic():
        import re as _re
        toks = [_re.sub(r"[^a-z0-9]", "", w) for w in (brand or "").lower().split()]
        toks = [w for w in toks if len(w) > 2]
        return {t: any(w in t.lower() for w in toks) for t in terms} if toks else {t: False for t in terms}

    if not brand:
        return _heuristic()
    prompt = (
        f"The seller's brand is \"{brand}\". For each Amazon search term below, say if it is "
        "BRANDED (mentions this brand or a clear misspelling of it) or GENERIC (no brand / a "
        f"competitor brand).\n\nTerms: {json.dumps(terms)}\n\n"
        'Reply with ONLY a JSON array: [{"term":"...","branded":true}]'
    )
    try:
        text = chat([{"role": "system", "content": "You output only valid JSON arrays."},
                     {"role": "user", "content": prompt}], max_tokens=1500)
        start, end = text.find("["), text.rfind("]")
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else []
        out = {d.get("term"): bool(d.get("branded", False)) for d in data if d.get("term")}
        # Branded if the AI says so OR the brand name literally appears (never miss it).
        h = _heuristic()
        return {t: bool(out.get(t, False) or h.get(t, False)) for t in terms}
    except (AIClientError, ValueError, KeyError, TypeError):
        return _heuristic()


def harvest_summary(harvest, negate, sqp, target_acos):
    """Plain-language brief of the harvesting opportunity."""
    h_sales = sum(float(x.get("sales") or 0) for x in harvest)
    n_spend = sum(float(x.get("spend") or 0) for x in negate)
    prompt = (
        f"You are an Amazon PPC strategist. Target ACOS {target_acos:.0%}. From search-term "
        f"and SQP analysis: {len(harvest)} keywords to harvest (converting; ${h_sales:,.0f} "
        f"sales), {len(negate)} terms to negate (wasted ${n_spend:,.0f}), {len(sqp)} SQP "
        "opportunities. Write a concise 5-7 sentence dashboard brief: the size of the "
        "opportunity, which actions matter most, and one clear recommendation. Plain language."
    )
    try:
        return chat([
            {"role": "system", "content": "You summarize Amazon keyword harvesting clearly."},
            {"role": "user", "content": prompt},
        ])
    except AIClientError:
        return (f"{len(harvest)} keywords ready to harvest (${h_sales:,.0f} converting sales), "
                f"{len(negate)} terms wasting ${n_spend:,.0f} to negate, and {len(sqp)} SQP "
                "opportunities. (Offline summary — AI endpoint unreachable.)")


def _compact_table(rows):
    keys = ["campaign", "keyword", "match", "rule", "current_bid", "new_bid",
            "acos", "spend", "sales", "clicks", "orders"]
    return json.dumps([{k: r.get(k) for k in keys} for r in rows], default=str)


def _local_summary(changed, target_acos, target_cpa):
    if not changed:
        return ("No keyword bid changes were triggered by the current rules at "
                f"target ACOS {target_acos:.0%} / target CPA ${target_cpa:.2f}.")
    from collections import Counter
    rules = Counter(r.get("rule") for r in changed)
    spend = sum(float(r.get("spend") or 0) for r in changed)
    camps = Counter(r.get("campaign") for r in changed)
    top = ", ".join(f"{c} ({n})" for c, n in camps.most_common(3))
    dominant = rules.most_common(1)[0][0] if rules else "—"
    return (
        f"{len(changed)} keywords have proposed bid changes (offline summary — AI "
        f"endpoint unreachable). The dominant rule is '{dominant}'. Total spend across "
        f"affected keywords is ${spend:,.2f}. Most affected campaigns: {top}. Review the "
        "table and apply when ready."
    )
