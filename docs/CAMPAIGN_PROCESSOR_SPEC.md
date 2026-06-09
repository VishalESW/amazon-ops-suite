# Campaign Processor — Workbook Spec

Reverse-engineered from the client master workbook (Be Right Golf example).
Source analyzed: `_ngram_source.xlsx` (Google Sheets export). 20 tabs.
Goal: a web processor that takes user uploads + manual inputs and produces the
**same workbook with live formulas**, downloadable as `.xlsx`.

The processor is **template-based**: it loads `assets/campaign_template.xlsx`
(an exact copy of the master), clears example data, and writes the user's data.
This guarantees formulas, formatting, dropdowns, and layout match exactly.

## Pipeline (4 steps)

### Step 1 — Seed inputs
- **ASIN List** (1.1): user enters seller SKU / ASIN / item-name (cols A,B,C).
  Formulas: `D=B&"/"&A`, `E=SUMIFS(Q:Q,L:L,B)`, `F=SUMIFS(R:R,L:L,B)`,
  `G=SUMIFS(S:S,L:L,B)`. CTR-data block (cols L..AA) is a paste from Ads (optional).
- **Product Opportunity Explorer** (1.2): paste POE keyword export. Feeds Semantics.
- **Helium10 Reverse ASIN Search** (1.3): user enters ASINs (A4 down) + pastes H10
  reverse-ASIN keyword export (cols B..AF).
- **Search Term Report** (1.4, OPTIONAL): Adlabs export. Header row 6, data row 7+.
  Feeds PAT + Semantics metrics via XLOOKUP. If absent → IFERROR guards return 0.

### Step 2 — Brand Analytics block
- **BA TST - <word>** (2.1): one tab per filter word. User uploads POE top-search-term
  file per product (file name carries product/word). 6-block side-by-side layout
  (cols A/B, G/H/I, J/K/L, M/N/O, P/Q/R, S/T) keyed by "Search Term Filter Word".
- **Brand Analytics - ASIN Filter** (2.2): upload Brand Analytics (w/ reverse ASIN).
- **SQP Report <ASIN>** (2.3): up to 3 own ASINs; one tab each. Feeds Semantics search vol.
- **Brand** (2.4): H10 reverse ASIN for own brand. Header row 6, data row 7+.

### Step 3 — Consolidation
- **Master Keyword List** (3.1): wide staging. Manual user input for:
  Own Branded KWs (AL), Own Branded Searches (AM), Competitor KWs (AP),
  Competitor Searches (AQ), Own Brand ASINs (BD). Other source columns optional.
- **PAT** (3.2): product-attribute targeting. Header row 2, data row 3+.
  Formulas (per row r): `D=www.amazon.com/dp/&B`, `E/F/G/H=IFERROR(XLOOKUP(B,'Search Term Report'!A:A, S/U/Q/R),0)`,
  `L=IFERROR(XLOOKUP(K&"-PT-Ex.-"&J,'Campaign Naming...'!AN, ...I),"n/a")`,
  `M=IFERROR(XLOOKUP(J,S:S,T:T),"")`, `P=H`, `Q=ROUND((N*O*P)/(1+M),2)`.
- **Campaign Root KW** (3.3): category list (0-Gen,1-Gift,2-Accessories,3-Training Aids,4-Putter). col A.
- **Semantics** (3.4) — THE BRAIN. Header row 3, data row 4+. Rows 1-2 hold template
  formulas + threshold constants (keep them).
  - A=Keyword, B=Source. Keywords are the **best picks** from POE + H10 Reverse ASIN +
    Brand Analytics (AI/manual selection).
  - `C=Search Volume = IFERROR(XLOOKUP(A,POE!C,POE!P), IFERROR(XLOOKUP(A,SQP!A,SQP!D), IFERROR(XLOOKUP(A,H10!A,H10!H),0)))`
    (original also chained a broken external ref `'[1]SQP Report B0DQV56XC5'` → MUST be removed).
  - `D=IFERROR(XLOOKUP(A,STR!A,STR!U),0)` orders
  - `E=IFERROR(XLOOKUP(A,STR!A,STR!S),0)` CPC
  - `F=IFERROR(XLOOKUP(A,STR!A,STR!Q),0)` ACoS
  - `G=IFERROR(XLOOKUP(A,STR!A,STR!R), IFERROR(XLOOKUP(A,H10!A,H10!C),0))` CVR
    (original had stray trailing `%` modulo → MUST be removed).
  - K=Category (root match), L=SKW/MKW, M=Match (Ex./Br. etc), N=Broad KW list (MKW grouping),
    O=Product name.
  - `P=IFERROR(XLOOKUP(O&"-"&L&"-"&M&"-"&A,'Campaign Naming...'!AN,'Campaign Naming...'!I),"n/a")` campaign name
  - Q=Placement Modifier, R=ASP, S=ACoS target, `T=ROUND((G*R*S)/(1+Q),2)` starting bid.

### Step 4 — Output
- **Campaign Naming, Bids & Targets**: final campaign rows. Header row 1, data row 2+.
  Generated from selected Semantics rows (+ PAT). Input cols: A=Action, B=Product,
  C=Campaign Type, E=KW/PT, F=Match, G=Root KW, H=Goal, J..AK various settings.
  Formula cols: `I=B&" | "&C&" | "&E&" | "&F&" | "&G&" | "&H"` (campaign name),
  `T="Be Right Golf | "&B&" > "&H` (campaign tag — brand prefix is dynamic),
  `W=B&" | "&E&" | "&G&" | "&F"` (ad group name),
  `AN=B&"-"&E&"-"&F&"-"&G"` (helper key joined back from Semantics P / PAT L),
  `AL=ROUND((AI*AJ*AK)/(1+AH),2)` (starting bid). AB="Refer Semantics".

## The "optional sheet breaks formulas" bug (must fix)
1. `Semantics!C` chains `XLOOKUP(... '[1]SQP Report B0DQV56XC5' ...)` — a broken
   **external-workbook** link (`[1]`). It throws `#REF!`/`#NAME?` that the outer
   IFERROR may not fully absorb in Excel. FIX: drop that term; keep only the in-book
   SQP tab(s) that actually exist.
2. `Semantics!G` ends with `...IFERROR(...0)%` — a stray percent/modulo operator.
   FIX: remove trailing `%`.
3. When optional tabs (Search Term Report, SQP) are not provided, the XLOOKUP target
   range is empty → IFERROR returns 0/"". Ensure every cross-sheet XLOOKUP stays
   wrapped in IFERROR with a sane default so missing optional data never errors.
4. Build formulas referencing only the SQP tabs that exist this run (1-3 of them),
   nested in IFERROR.

## Passthrough vs computed
- **Passthrough** (paste upload rows as-is, keep control-panel headers):
  POE, H10 Reverse ASIN, Brand, Brand Analytics - ASIN Filter, SQP Report*, BA TST*,
  Search Term Report, ASIN List CTR block.
- **Computed** (app writes data + formulas): ASIN List (A,B,C + formulas),
  Semantics, PAT, Campaign Naming, Master Keyword List (manual fields).

## AI use (NVIDIA GLM, OpenAI-compatible)
- base_url `https://integrate.api.nvidia.com/v1`, model `z-ai/glm-5.1`.
- Tasks: (a) pick best keywords for Semantics from POE+H10+Brand Analytics;
  (b) assign each keyword to a Campaign Root KW category; (c) suggest SKW vs MKW and
  match type; (d) group MKW broad lists. All AI steps degrade to deterministic
  heuristics if endpoint unreachable.
