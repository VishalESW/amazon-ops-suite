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


# Deterministic-fallback lexicons for the PPC root rule. The AI is the primary
# path; these keep root assignment working when the endpoint is unreachable.
_BRAND_DEVICE = {
    "magsafe", "iphone", "samsung", "tesla", "jeep", "android", "motorola",
    "pixel", "apple", "google", "galaxy", "oneplus", "huawei", "xiaomi", "nokia",
    "sony", "lg", "honda", "toyota", "ford", "bmw", "audi", "kia", "nissan",
    "chevy", "chevrolet", "ipad", "airpods", "garmin",
}
_FEATURE = {
    "magnetic", "mirror", "vent", "wireless", "dashboard", "dash", "suction",
    "windshield", "cup", "ring", "stand", "clip", "cradle", "grip", "adhesive",
    "telescopic", "retractable", "foldable", "rearview", "armrest", "console",
}

# --------------------------------------------------------------------------- #
# 6-step PPC root-keyword priority chain (stop at the first step that matches). #
# Step 1 Brand > 2 Medical/Ingredient > 3 Form > 4 Location/Surface >          #
# 5 Problem/Condition > 6 Generic type; else Rule 7 "gen".                      #
# --------------------------------------------------------------------------- #
_STEP2_MEDICAL = {
    "antifungal", "antibacterial", "antimicrobial", "antiseptic", "antibiotic",
    "enzymatic", "medicated", "medical", "probiotic", "nontoxic", "toxic",
    "hypoallergenic", "ketoconazole", "miconazole", "chlorhexidine", "clotrimazole",
    "boric", "salicylic", "benzethonium", "aloe", "witchhazel", "alcohol", "peroxide",
}
_STEP3_FORM = {
    "wipes", "wipe", "pads", "pad", "solution", "kit", "spray", "finger", "foam",
    "foaming", "squeegee", "microfiber", "brush", "cloth", "gel", "drops", "liquid",
    "powder", "roll", "stick", "rinse", "mousse", "towelette", "towelettes",
}
_STEP4_LOCATION = {
    "interior", "inside", "inner", "indoor", "exterior", "outdoor", "outside",
    "outer", "mirror", "rearview", "vent", "dashboard", "dash", "window",
    "windshield", "windscreen", "seat", "console", "cabin",
}
_STEP5_PROBLEM = {
    "infection", "yeast", "mites", "mite", "bacteria", "bacterial", "fungal",
    "foggy", "fog", "antifog", "streak", "odor", "odour", "smelly", "smell",
    "waterproof", "itch", "itchy", "itching", "wax", "waxy", "allergy", "allergic",
    "grime", "dirt", "residue", "buildup",
}
_STEP6_GENERIC = {
    "holder", "mount", "stand", "tool", "detailing", "cleaner", "cleanser",
    "cleaning", "wash", "care", "dispenser", "applicator", "remover", "cleaningkit",
}

# Rule 4 / Rule 5 — fold synonyms and near-duplicates onto ONE canonical root so
# the same concept never appears under two labels.
_ROOT_CANON = {
    "cleanser": "cleaner", "cleaning": "cleaner", "clean": "cleaner",
    "inside": "interior", "inner": "interior", "indoor": "interior",
    "outdoor": "exterior", "outside": "exterior", "outer": "exterior",
    "yeast": "infection", "mites": "infection", "mite": "infection",
    "bacteria": "infection", "bacterial": "infection", "fungal": "infection",
    "wipe": "wipes", "pads": "wipes", "pad": "wipes", "towelette": "wipes",
    "towelettes": "wipes", "odour": "odor", "smelly": "odor", "smell": "odor",
    "fog": "foggy", "itchy": "itch", "itching": "itch", "windscreen": "windshield",
    "rearview": "mirror", "dash": "dashboard", "foaming": "foam", "toxic": "nontoxic",
}
# Priority chain the heuristic walks (step 1 = brand handled separately). NOTE:
# Problem (step 5) is placed before Location (step 4): when a keyword carries both
# a condition word and a surface word, the rule's own examples make the condition
# the root ("streak free window cleaner" -> streak, not window; "foggy windshield
# cleaner" -> foggy). No location example contains a problem word, so this ordering
# reproduces every example while keeping the earlier steps strictly dominant.
_ROOT_STEPS = [_STEP2_MEDICAL, _STEP3_FORM, _STEP5_PROBLEM, _STEP4_LOCATION, _STEP6_GENERIC]


def _canon_root(root):
    """One lowercase word, folded onto its canonical group (Rules 1, 4, 5)."""
    r = re.sub(r"[^a-z0-9]", "", str(root or "").strip().lower().split(" ")[0])
    return _ROOT_CANON.get(r, r)


def _cap_roots(mapping, max_roots):
    """Fold the least-used roots into the single most-used one so the result never
    exceeds `max_roots` categories (rule: <=15 root categories total)."""
    from collections import Counter
    cnt = Counter(mapping.values())
    if len(cnt) <= max_roots:
        return mapping
    keep = {r for r, _ in cnt.most_common(max_roots - 1)}
    fallback = cnt.most_common(1)[0][0]
    return {k: (v if v in keep else fallback) for k, v in mapping.items()}


def _roots_heuristic(keywords, custom):
    """Deterministic root per keyword following the 6-step priority chain:
    custom roots > brand/device > medical/ingredient > form > location/surface >
    problem/condition > generic type; else 'gen' (Rule 7). One lowercase word,
    canonicalised (Rules 1/4/5). The root always exists in the keyword (Rule 2)."""
    custom = [_canon_root(c) for c in (custom or []) if c]
    out = {}
    for kw in keywords:
        toks = [w for w in re.split(r"[^a-z0-9]+", kw.lower()) if w]
        root = ""
        # Custom roots first, then Step 1 (brand/device). Match the keyword token
        # OR its canonical group (so a custom root "cleaner" catches "cleaning").
        for w in toks:
            if _canon_root(w) in custom:
                root = _canon_root(w)
                break
        if not root:
            root = next((w for w in toks if w in _BRAND_DEVICE), "")
        # Steps 2-6, absolute order — first step with a matching token wins.
        if not root:
            for step in _ROOT_STEPS:
                hit = next((w for w in toks if w in step), "")
                if hit:
                    root = hit
                    break
        if not root:
            # Rule 7 outlier: most descriptive (longest) non-stopword token.
            cands = [w for w in toks if len(w) > 2 and not w.isdigit() and w not in _STOP]
            root = max(cands, key=len) if cands else (toks[0] if toks else "gen")
        out[kw] = _canon_root(root) or "gen"
    return out


def _summary(mapping):
    from collections import Counter
    return [[r, n] for r, n in Counter(mapping.values()).most_common()]


def root_for(keyword, custom_roots=None):
    """Best single root for ONE keyword via the deterministic rule — used as a
    per-keyword fallback when an AI map has no entry for that keyword."""
    return _roots_heuristic([str(keyword)], custom_roots).get(str(keyword), "")


def assign_roots_ruled(keywords, custom_roots=None, max_roots=15):
    """Assign ONE lowercase root word to each keyword following the PPC root rule:
    brand/device name > feature/type > foreign-language group > generic noun.
    `custom_roots` are user-defined roots the assigner reuses first whenever a
    keyword fits one. Caps at `max_roots` categories. Returns
    {"map": {keyword: root}, "summary": [[root, count], ...]} (count desc).
    AI-first with a deterministic lexicon fallback so build never breaks."""
    kws = [k for k in dict.fromkeys(str(k).strip() for k in keywords) if k]
    custom = [str(c).strip().lower() for c in (custom_roots or []) if str(c).strip()]
    if not kws:
        return {"map": {}, "summary": []}

    if available():
        custom_line = (
            f"REUSE these existing root categories first whenever a keyword fits one: "
            f"{json.dumps(custom)}.\n" if custom else ""
        )
        prompt = (
            "You are a PPC keyword taxonomy specialist. Assign exactly ONE root "
            "keyword to each keyword. The root is the single most specific, "
            "distinctive word that already exists in the keyword — not a category "
            "or theme.\n\n"
            "THE 6-STEP PRIORITY CHAIN — work in order, STOP at the first step that "
            "matches (an earlier step always wins):\n"
            "1. Brand / Product name / proper noun (magsafe, iphone, samsung, tesla, "
            "jeep, cybertruck, epiotic) -> that word.\n"
            "2. Medical / clinical / ingredient / active word (ketoconazole, "
            "antifungal, enzymatic, medicated, nontoxic) -> that word.\n"
            "3. Form / format / delivery method (wipes, solution, kit, spray, finger, "
            "foam, squeegee, microfiber, brush) -> that word.\n"
            "4. Location / surface / application - WHERE it is used or what surface "
            "(interior, mirror, vent, dashboard, exterior, window) -> that word.\n"
            "5. Problem / condition / use case (infection, foggy, streak, antifog, "
            "odor, waterproof) -> that word.\n"
            "6. Generic product type - the most descriptive noun (holder, mount, "
            "stand, tool, detailing, cleaner) -> that word.\n"
            "7. Only if NONE of steps 1-6 fit, use 'gen' (should be <5% of the list).\n\n"
            "CRITICAL RULES:\n"
            "- ONE lowercase word only. Never two words, never a phrase.\n"
            "- The word must appear in (or be directly implied by) the keyword. "
            "Never invent a word that isn't there.\n"
            "- Group synonyms onto ONE root: cleaner/cleanser/cleaning->cleaner; "
            "inside/inner/indoor->interior; outdoor/outside->exterior; "
            "yeast/mites/bacteria/infection->infection; wipe/wipes/pads->wipes.\n"
            "- Foreign-language keywords: group under ONE root word from that "
            "language (Spanish car->carro, cleaner->vidrios, phone->celular).\n"
            "- Be consistent: the same distinctive word always gets the same root.\n"
            f"- Use NO MORE than {max_roots} distinct roots total.\n"
            f"{custom_line}"
            f"Keywords: {json.dumps(kws[:400])}\n\n"
            'Reply ONLY a JSON array of objects: [{"kw":"...","root":"..."}]'
        )
        try:
            data = _json_array(chat([
                {"role": "system", "content": "You output only a valid JSON array."},
                {"role": "user", "content": prompt},
            ], max_tokens=4000))
            mapping = {}
            valid = {_norm(k): k for k in kws}
            for d in data:
                if not isinstance(d, dict):
                    continue
                kw = valid.get(_norm(d.get("kw")))
                root = _canon_root(d.get("root"))
                if not kw or not root:
                    continue
                # Rule 2: the root must actually occur in the keyword (allowing its
                # canonical group). If the model invented a word, re-derive it from
                # the chain instead. Foreign-language 'gen'-style roots are kept.
                kw_toks = {_canon_root(w) for w in re.split(r"[^a-z0-9]+", kw.lower()) if w}
                if root in kw_toks or root == "gen":
                    mapping[kw] = root
                else:
                    mapping[kw] = root_for(kw, custom) or root
            # Fill any keyword the model skipped, then enforce the category cap.
            missing = [k for k in kws if k not in mapping]
            if missing:
                mapping.update(_roots_heuristic(missing, custom))
            if mapping:
                mapping = _cap_roots(mapping, max_roots)
                return {"map": mapping, "summary": _summary(mapping)}
        except CampaignAIError:
            pass

    mapping = _cap_roots(_roots_heuristic(kws, custom), max_roots)
    return {"map": mapping, "summary": _summary(mapping)}


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


# Whole names (once punctuation/spacing is stripped) that are never a brand.
_BRAND_STOP = {
    "buy", "cat", "cats", "dog", "dogs", "pet", "pets", "thepet", "petsbest",
    "best", "top", "otic", "oticsolution", "solution", "wipes", "wipe", "wipers",
    "cleaner", "cleanser", "cleaning", "ear", "ears", "compostable", "poopbags",
    "poop", "bags", "isleof", "for", "and", "with", "the", "kit", "refill",
    "natural", "organic", "advanced", "care", "health", "vet", "vets", "plus",
    "free", "new", "original", "pack", "size", "large", "small", "medium",
    "puppy", "adult", "treats", "shampoo", "spray", "drops", "wash", "dental",
}


def _brand_key(s):
    """Canonical identity of a brand name: letters+digits only, lowercased.
    Collapses 'Epi-Otic' / 'epi otic' / 'epiotic' and "Vet's Best" / 'vets best'
    onto one key so spacing and punctuation variants dedupe."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _finalise_brands(cands, terms, own_brand, generic, known_keys, limit,
                     supplemental=None):
    """Canonicalise, verify and rank raw brand candidates.

    cands are trusted (column-derived + model). `supplemental` are heuristic
    guesses, admitted only when they don't overlap a trusted brand — so a bare
    head like "paw" is dropped next to "paw science", while a brand the model
    missed entirely ("Zymox") is still recovered.
    """
    from collections import Counter
    term_keys = [_brand_key(t) for t in terms]
    own_k = _brand_key(own_brand)

    groups = {}
    for name in cands:
        n = " ".join(str(name or "").split())
        k = _brand_key(n)
        if not k or len(k) < 3 or k == own_k or k in _BRAND_STOP:
            continue
        toks = _norm(n).split()
        if toks and all(t in generic for t in toks):   # purely category words
            continue
        # Must really occur in the search terms — drops model hallucinations.
        occ = sum(1 for tk in term_keys if k in tk)
        if not occ:
            continue
        # Preferred spelling: the column-derived form when we have one,
        # otherwise the shortest candidate (drops "Curaseb antiseptic").
        display = known_keys.get(k) or n
        g = groups.get(k)
        if g is None:
            groups[k] = {"display": display, "occ": occ}
        elif not known_keys.get(k) and len(n) < len(g["display"]):
            groups[k] = {"display": n, "occ": occ}
    # A key that is another key plus a trailing variant ("douxos3" vs "douxo")
    # is the same brand — keep the base.
    keys = set(groups)
    out, kept = [], set()
    for k, g in sorted(groups.items(),
                       key=lambda kv: (kv[0] not in known_keys, -kv[1]["occ"])):
        base = re.sub(r"[a-z]?\d+$", "", k)
        if base != k and len(base) >= 3 and base in keys:
            continue
        # If both "Curaseb" and "Curaseb antiseptic" were proposed, the bare
        # brand is the right one — drop the longer form carrying a product word.
        if any(other != k and len(other) >= 4 and k.startswith(other)
               for other in keys):
            continue
        out.append(g["display"])
        kept.add(k)

    # Heuristic extras: only what the trusted set doesn't already cover.
    for name in (supplemental or []):
        if len(out) >= limit:
            break
        n = " ".join(str(name or "").split())
        k = _brand_key(n)
        if not k or len(k) < 3 or k == own_k or k in _BRAND_STOP or k in kept:
            continue
        toks = _norm(n).split()
        if toks and all(t in generic for t in toks):
            continue
        if not any(k in tk for tk in term_keys):
            continue
        # Overlaps a brand we already have, in either direction -> it's a
        # fragment ("paw" vs "paw science") or a padded form, not a new brand.
        if any(other.startswith(k) or k.startswith(other) for other in kept):
            continue
        # Heuristic extras come off lowercased search terms — title-case them so
        # they sit alongside the model/column spellings.
        out.append(n.title() if n.islower() else n)
        kept.add(k)
    return out[:limit]


def extract_brands(search_terms, known=None, own_brand="", limit=60):
    """Brand names mentioned inside competitor search terms.

    Files without a brand column (e.g. a Search Term Report) still carry the
    brand in the term itself — "zymox ear wipes", "virbac epi-otic". Pull those
    out so Master Keywords lists real competitors instead of only the handful of
    rows that happened to have a brand column.

    known: brand names already found via a brand column — kept and ranked first.
    Every candidate, model- or heuristic-derived, is canonicalised (so 'Epi-Otic'
    and 'epi otic' collapse), checked against the stop list, and verified to
    actually occur in the terms before it is returned.
    """
    from collections import Counter
    terms = [str(t).strip() for t in (search_terms or []) if str(t or "").strip()]
    known = [k for k in (known or []) if str(k or "").strip()]
    if not terms:
        return list(dict.fromkeys(known))[:limit]

    tokenised = [_norm(t).split() for t in terms]
    df = Counter()
    for w in tokenised:
        df.update(set(w))
    # Category words repeat across many terms — but so does a dominant
    # competitor ("zymox" led 70 of 200 searches), and a frequency-only rule
    # classed it as generic and silently dropped it. The distinguishing signal
    # is position: category words FOLLOW ("... ear wipes"), brands LEAD
    # ("zymox ..."). So a frequent token is only generic when it rarely leads.
    lead_df = Counter(w[0] for w in tokenised if w)
    freq_cut = max(5, 0.15 * len(tokenised))
    generic = {tok for tok, n in df.items()
               if n > freq_cut and lead_df.get(tok, 0) < 0.25 * n}
    known_keys = {}
    for b in known:
        known_keys.setdefault(_brand_key(b), " ".join(str(b).split()))

    cands = list(known)
    if available():
        pool = terms[:400]
        prompt = (
            "Below are Amazon customer search terms for competitor products.\n"
            "List every BRAND name that appears in them (e.g. 'zymox ear wipes' "
            "-> 'Zymox'; 'virbac epi-otic advanced' -> 'Virbac' and 'Epi-Otic').\n"
            "Rules:\n"
            "- Real product/company brands only. Never generic words ('ear wipes',"
            " 'dog', 'cleaner', 'buy', 'cat', 'poop bags') and never ASINs.\n"
            "- One entry per brand in its correct, conventional spelling. Do NOT "
            "return spacing/punctuation variants of the same brand (pick either "
            "'Epi-Otic' or 'epi otic', not both).\n"
            "- Do not append product words to the brand ('Curaseb', not 'Curaseb "
            "antiseptic'; 'iHeartDogs', not 'iHeartDogs beef').\n"
            f"- Exclude the seller's own brand: {own_brand or '(none)'}.\n"
            f"- At most {limit}.\n\n"
            f"Search terms: {json.dumps(pool)}\n\n"
            'Reply ONLY a JSON array of brand strings: ["Zymox","Virbac",...]'
        )
        try:
            text = chat([
                {"role": "system", "content": "You output only a valid JSON array of strings."},
                {"role": "user", "content": prompt},
            ], max_tokens=2000)
            cands += [b for b in _json_array(text) if isinstance(b, str)]
        except CampaignAIError:
            pass

    # The heuristic always runs, as a supplement: models reliably miss a few
    # brands (Zymox, Earth Rated), and _finalise_brands only admits an extra
    # that doesn't overlap a trusted one, so fragments stay out.
    uni, bi = Counter(), Counter()
    for w in tokenised:
        lead = []
        for tok in w[:2]:                      # brands are 1-2 leading words
            if tok in generic or len(tok) < 2 or tok.isdigit():
                break
            lead.append(tok)
        if not lead:
            continue
        uni[lead[0]] += 1                      # count the head separately so a
        if len(lead) == 2:                     # varied 2nd word ("douxo dewaxing"
            bi[" ".join(lead)] += 1            # / "douxo micellar") still totals

    extras = []
    for first, n in uni.most_common():
        # Use the two-word form only when the head is *usually* followed by the
        # same word ("earth rated", "pet md") — otherwise the head is the brand.
        pair = [(b, c) for b, c in bi.items() if b.split()[0] == first]
        best = max(pair, key=lambda x: x[1]) if pair else None
        extras.append(best[0] if best and best[1] >= n * 0.6 else first)

    return _finalise_brands(cands, terms, own_brand, generic, known_keys, limit,
                            supplemental=extras)


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
