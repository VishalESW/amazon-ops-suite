# Campaign Processor v2 — Build Plan (stateful, AdLabs-driven, role-gated)

Full redesign of the Campaign Processor into a step-by-step, persisted, approval-gated
workflow. Replaces the single-form v1. Other app features untouched.

## Roles & projects (DB)
- New SQLite tables:
  - `cp_users(email PK, role)` — role ∈ {operator, manager}. Managers seeded from
    `.env CAMPAIGN_MANAGER_EMAILS` (comma list); default = operator. Manager can
    promote/demote in a small admin panel.
  - `cp_projects(id, name, profile_id, profile_name, team_id, status, current_step,
    created_by, created_at, updated_at)`. status ∈ {draft, awaiting_approval,
    approved, completed}.
  - `cp_state(project_id, step_key, json_blob)` — per-step saved data (entered
    keywords/ASINs, uploaded-file refs, selections). Big blobs stored as JSON.
  - `cp_approvals(id, project_id, step_key, requested_by, requested_at,
    approved_by, approved_at, note)`.
- Multiple projects. Resumable by anyone with app access.
- **Save** = approve & continue (manager action on a gated step). **Save & Exit**
  = pause + persist (operator); project → `awaiting_approval`; appears in the
  Manager dashboard queue.

## Step flow (each step its own dashboard screen; only current step shows)
**Step 1 — Profile select.** Top-of-app profile picker. Fetch teams→profiles from
AdLabs (cached). Pick one → create/open a project.

**Step 2 — ASIN dashboard (auto).** Fetch AdLabs `entity_type="product"` for the
profile (DATE = last 90d, COMPARE_DATE prior 90d, IMPRESSIONS>0). Show table:
ASIN, SKU, Product Name, Impressions, Clicks, CTR, Spend, CPC, Sales, Orders,
ACOS, CVR, Price, ASP + **Rank**. Rank computed (see Rank formula) with a custom
editable override per ASIN. This feeds the ASIN List tab.

**Step 3 — Seed entry + approval gates (3 sub-gates):**
  - 3a POE customer needs: operator types customer-need keywords → Save & Exit →
    manager approves → Save (continue).
  - 3b H10 Reverse ASIN list: operator types ASINs → approval.
  - 3c SQP ASINs (≤3): operator types ASINs → approval.

**Step 4 — Upload files.** POE (per product), H10 Reverse, Search Term Report
(optional), multiple BA TST, Brand Analytics, SQP, Brand-H10. Header auto-detect +
name-canonicalisation already built (reused from v1).

**Step 5 — Keyword selection grids.** For each uploaded file, render a sheet view
(all columns) with a **dropdown beside the keyword column** per row: `Y / N /
Competitor / Brand`. Selections persisted per project. (Y = target.)

**Step 6 — Semantics.** Build from keywords tagged **Y only** (exclude N /
Competitor / Brand), carrying their data via the existing Semantics formulas.
Preview grid in dashboard.

**Step 7 — ASIN selection grids → PAT.** For each file's ASIN columns (order-wise),
dropdown per ASIN: `Main / Low Rated / High Priced / Bestselling / Non-relevant`.
Non-relevant excluded. Feeds PAT (rows + S:T placement table) + PT campaigns.

**Step 8 — Master Keyword List.** Keywords tagged **Competitor** → MKL competitor
columns (AP/AQ) with the **competitor name read from a brand column in the file**;
keywords tagged **Brand** → own-brand columns (AL/AM) with own brand name.

**Step 9 — Campaign Naming & Bidding** (the soul):
  - Root keyword generation from Y keywords (max 10–15). A keyword with multiple
    roots → assign the most-relevant root not already taken (greedy unique).
    Keyword with no root → catch-all "0-Gen". (AI-assisted, heuristic fallback.)
  - SV-digit → campaign (ONE per keyword, C="SPM"):
    6-digit→SKW Ex. · 5-digit→MKW Ex. · 4-digit→MKW Br.M · 3-digit→MKW Br. ·
    ≤2-digit→MKW Phrase (Ph.).
  - Campaign Name via the sheet formula (I = B|C|E|F|G|H), AN helper, ad group W.
  - Bidding: CVR from Search Term Report; if STR absent, use H10 Reverse
    "ABA Total Conv. Share" as CVR. Starting bid =
    ROUND((CVR*ASP*ACoS_target)/(1+placement),2). Formula swapped to the available
    source so it never breaks.

**Step 10 — Verify + preview + build.** Cross-verify every Semantics & Campaign
column, show full preview, then build the downloadable .xlsx (template-based engine,
reused + extended from v1).

## Rank formula (Step 2) — I decide, override allowed
Across the profile's ASINs, z-normalise sales, cvr, acos; score =
0.5·z(sales) + 0.3·z(cvr) − 0.2·z(acos); Rank 1 = best. Per-ASIN manual override.

## AdLabs (grounded by probing)
- teams → profiles → `read_resource(adlabs://profiles/<slug>)` → Profile ID.
- `entity_type="product"` returns asin, sku, title, brand, impressions, clicks,
  ctr, spend, cpc, sales, orders, cvr, acos, roas, price_to_pay, total_asp,
  best_seller_rank. Confirmed live on team 83095 / TVA Supply.
- Filters: JSON array, UPPERCASE keys, DATE + COMPARE_DATE required.

## UI (/hallmark)
Step-by-step dashboard; one step visible at a time with a progress rail. Sheet/grid
views fully formatted — no overlap, no truncation, horizontal scroll for wide
sheets, sticky header + sticky keyword/selection column. Manager queue view.

## Build phasing
1. DB models + RBAC + project lifecycle + manager queue.
2. AdLabs profile picker + product fetch + ASIN dashboard + Rank.
3. Seed-entry steps + approval gates (Save / Save & Exit).
4. Upload + selection grids (keyword & ASIN) with persistence.
5. Semantics / PAT / MKL builders from selections.
6. Root-KW + SV-rule campaign generation + bidding.
7. Verify + preview + build/download.
8. /hallmark polish pass on every screen.

## Assumptions to confirm (small)
1. Rank weights 0.5/0.3/0.2 (sales/cvr/acos) — OK? (override always available.)
2. Phrase match code in the workbook = **"Ph."** (sheet only shows Ex./Br.M/Br.).
3. Competitor/Brand NAME column: use the file's brand column — for BA TST that's
   "Top Clicked Brand 1" (first non-empty). If a file has no brand column, operator
   types the name. OK?
4. Managers seeded via `.env CAMPAIGN_MANAGER_EMAILS`. OK?
