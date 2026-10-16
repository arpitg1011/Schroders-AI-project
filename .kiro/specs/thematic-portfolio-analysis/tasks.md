# Implementation Plan: AI-Powered Thematic Portfolio Analysis

## Overview

This plan builds the prototype bottom-up in Python 3.12, following the design's two-phase structure. Phase 1 (Tasks 1–11) delivers the full local analysis platform — `engine.py` pure logic, property-based and fixture tests, the single Groq taxonomy call, and the Streamlit UI — running with no login (Requirements 1–10). Phase 2 (Tasks 12–14, marked optional/later) adds Supabase authentication and saved-portfolio persistence in an isolated `supabase_client.py` without disturbing the working Phase 1 path (Requirements 11–12).

The engine is built incrementally with tests at each step, so determinism (Req 6.9) is validated as the logic lands. Property-based tests (Hypothesis) implement the 13 Correctness Properties from the design, one test per property, tagged as the design specifies. The build ends with end-to-end wiring and a local smoke run on `data/portfolio_80.xlsx`.

## Tasks

- [x] 1. Project scaffolding and dependency setup
  - Create `requirements.txt` with the exact dependency list from the design (streamlit, groq, pandas, numpy, openpyxl, python-dotenv, rapidfuzz, supabase, hypothesis, pytest) with the pinned minimum versions
  - Create the project file layout: empty `engine.py`, `app.py`, `supabase_client.py`, and a `tests/` package (with `__init__.py` or pytest config as needed)
  - Create `.env.example` documenting `GROQ_API_KEY` (and Phase 2 `SUPABASE_URL`/`SUPABASE_ANON_KEY`) with no secrets, and add `.env` / `.streamlit/secrets.toml` to `.gitignore`
  - Add a `config` helper (small function or module section) that reads a key from `st.secrets` first then the environment via `python-dotenv`, so the same code runs locally and on Streamlit Cloud
  - Confirm `data/portfolio_80.xlsx` and `data/spglobal_segments.xlsx` are present under `data/`
  - _Requirements: 1.1, 5.5, 11.7_
  - _Design: Technology Stack, Dependencies, Project File Layout, Configuration_

- [x] 2. Error-handling convention and shared engine helpers
  - [x] 2.1 Implement the shared `{"ok": bool, ...}` result convention in `engine.py`
    - Add helper constructors for handled errors (`error_code`, `message`, `detail`) and the canonical `error_code` constants (`MISSING_API_KEY`, `INVALID_API_KEY`, `LLM_BAD_JSON`, `LLM_UNREACHABLE`, `FILE_NOT_FOUND`, `MISSING_COLUMN`, `MISSING_SHEET`)
    - Keep `engine.py` free of any Streamlit import
    - _Requirements: 10.1, 10.2, 10.3_
    - _Design: Error-handling convention, Error Handling_

- [x] 3. Data loading: portfolio
  - [x] 3.1 Implement `load_portfolio(path_or_buffer)`
    - Read XLSX/CSV from a path or uploaded buffer, header row index 0, required columns Company, Bloomberg ticker, Country, Sector, SEDOL, Portfolio wt, Reference wt, Active wt
    - Keep Portfolio wt and Reference wt as decimal fractions (0.132 == 13.2%); normalize column names
    - Return `FILE_NOT_FOUND` when the path is missing and `MISSING_COLUMN` (naming the column) when a required column is absent
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
    - _Design: Function signatures (load_portfolio), Error Handling_

  - [x]* 3.2 Write unit tests for `load_portfolio` error paths
    - Crafted portfolio missing a required column → `MISSING_COLUMN` naming it; missing file path → `FILE_NOT_FOUND`
    - _Requirements: 1.6, 1.7_
    - _Design: Fixture / parsing tests_

- [x] 4. Data loading: S&P segment positional parse
  - [x] 4.1 Implement `load_segments(path)` positional parse
    - Read sheet "Sheet1" (ignore "Screening Criteria"); open raw with `header=None`; header at row index 4; skip rows 5 and 6; data from row index 7
    - Read named columns (SP_ENTITY_NAME, SP_ENTITY_ID, SP_COMPANY_NAME, SP_EXCHANGE, SP_LONG_BUSINESS_DESCRIPTION, IQ_* classification columns)
    - Locate the two repeated blocks by position: 5× IQ_BUS_SEG_ABS (names) and 5× IQ_BUS_SEG_REV_ABS (revenues); pair name[i] with revenue[i]
    - Extract exchange/ticker from the SP_ENTITY_NAME parenthetical via the `(EXCHANGE:TICKER)` regex; leave both unset when absent/non-matching
    - Coerce revenues: numeric → float (thousands); "NA"/blank/non-numeric → `revenue=None, revenue_present=False`; retain whichever side (name or revenue) exists per position
    - Build the `CompanySegments` structure per company keyed by normalized `entity_key`
    - Return `MISSING_SHEET` when no "Sheet1" and `MISSING_COLUMN` (naming it) when a required named column is absent
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12_
    - _Design: Function signatures (load_segments), Low-Level Algorithm A, Data Models (CompanySegments)_

  - [x]* 4.2 Write property test for ticker extraction
    - **Property 10: Ticker extraction matches the parenthetical pattern**
    - **Validates: Requirements 2.7, 2.8**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 10: ...`

  - [x]* 4.3 Write fixture parsing tests against `data/spglobal_segments.xlsx`
    - Reads "Sheet1", ignores "Screening Criteria"; header at row index 4, data from row index 7; exactly 5 IQ_BUS_SEG_ABS and 5 IQ_BUS_SEG_REV_ABS positional columns found; names pair with revenues positionally; ~20 companies carry data
    - Crafted segment file without "Sheet1" → `MISSING_SHEET`; missing file → `FILE_NOT_FOUND`
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.11_
    - _Design: Fixture / parsing tests_

- [x] 5. Company matching
  - [x] 5.1 Implement `normalize(name)` and `match_companies(portfolio_df, segments)`
    - `normalize`: uppercase, strip, remove parentheticals, strip corporate suffixes, remove punctuation, collapse spaces
    - Match order: exact normalized name match, then ticker match, then `rapidfuzz.fuzz.token_sort_ratio` fuzzy fallback with module constant `FUZZY_THRESHOLD = 88`
    - Unmatched companies retained with `matched=False`, `match_method="none"`; return `MatchResult` per company
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
    - _Design: Function signatures (match_companies), Low-Level Algorithm B, Data Models (MatchResult)_

  - [x]* 5.2 Write property test for name normalization idempotence
    - **Property 11: Name normalization is idempotent**
    - **Validates: Requirements 3.2, 3.3**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 11: ...`

- [x] 6. Revenue cleaning and segment weighting
  - [x] 6.1 Implement revenue cleaning and `Segment_Rev_Pct` weighting inside `score_company`
    - Contribute revenue only when `revenue_present`; clamp negatives to 0; force write-off/elimination-named segments to 0 and flag `cleaned_to_zero`
    - Compute `Segment_Rev_Pct_i = 100 * clean_rev_i / total`; when total is 0, set `has_positive_revenue=False` and exposure 0; flag segments excluded for missing revenue
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.10_
    - _Design: Low-Level Algorithm C, Data Models (ScoreResult.segment_breakdown)_

  - [x]* 6.2 Write property test for valid negative-safe distribution
    - **Property 1: Segment weighting is a valid, negative-safe distribution**
    - **Validates: Requirements 4.1, 4.2, 4.4, 4.5**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 1: ...`

  - [x]* 6.3 Write property test for write-off invariance
    - **Property 2: Write-off and elimination segments do not affect the distribution**
    - **Validates: Requirements 4.2**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 2: ...`

  - [x]* 6.4 Write property test for no-positive-revenue zero exposure
    - **Property 3: No positive revenue yields zero exposure**
    - **Validates: Requirements 4.6, 6.8**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 3: ...`

- [x] 7. Alignment scoring, exposure, and evidence
  - [x] 7.1 Implement tokenization and per-activity/segment alignment scoring in `score_company`
    - Shared normalization: lowercase, non-alphanumeric → space, collapse, split, drop stopword set
    - Per-activity tiers: strong phrase/all-token match → 1.0; partial overlap → `overlap/|A|` strictly in (0,1); no overlap → 0.0
    - Segment `alignment_score` = max over activities, rounded 4 dp, constrained [0.0, 1.0]; collect `matched_activities` and `matched_sub_industries`
    - _Requirements: 6.1, 6.2, 6.3, 6.6_
    - _Design: Low-Level Algorithm D, Data Models (ScoreResult)_

  - [x] 7.2 Implement company exposure and evidence snippet in `score_company`
    - `Company_Exposure = round(Σ(Segment_Rev_Pct_i × Alignment_Score_i), 2)` clamped [0, 100]
    - Evidence: pick highest non-zero alignment segment, extract verbatim 1–500 char window around first matched phrase; empty snippet + 0 exposure when no segment scores > 0
    - Return the full `ScoreResult` including `has_segment_data`, `has_positive_revenue`, and `segment_breakdown`; never invoke the LLM
    - _Requirements: 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 3.4, 9.5, 10.4_
    - _Design: Low-Level Algorithms E and F, Data Models (ScoreResult)_

  - [x]* 7.3 Write unit tests for alignment edge cases
    - "Kitchen Hardware", "Real Estate", "Consumer DX" against an unrelated taxonomy → 0.0
    - _Requirements: 6.3_
    - _Design: Example-based unit tests_

  - [x]* 7.4 Write property test for alignment scoring tiers
    - **Property 4: Alignment scoring tiers**
    - **Validates: Requirements 6.1, 6.3**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 4: ...`

  - [x]* 7.5 Write property test for alignment score range
    - **Property 5: Alignment score range**
    - **Validates: Requirements 6.2**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 5: ...`

  - [x]* 7.6 Write property test for bounded revenue-weighted exposure
    - **Property 6: Company exposure is a bounded revenue-weighted sum**
    - **Validates: Requirements 6.4, 6.5**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 6: ...`

  - [x]* 7.7 Write property test for determinism
    - **Property 7: Scoring is deterministic and reproducible**
    - **Validates: Requirements 6.9**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 7: ...`

  - [x]* 7.8 Write property test for evidence snippet bound and empty-iff-no-match
    - **Property 8: Evidence snippet bound and empty-iff-no-match**
    - **Validates: Requirements 6.7, 6.8**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 8: ...`

  - [x]* 7.9 Write property test for unmatched company flagging
    - **Property 9: Unmatched company is flagged with zero exposure**
    - **Validates: Requirements 3.4, 6.6, 9.5, 10.4**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 9: ...`

- [x] 8. Portfolio-level metrics
  - [x] 8.1 Implement `calculate_portfolio_metrics(df_scores, df_portfolio)`
    - `portfolio_thematic_exposure = Σ(company_exposure × portfolio_wt)`; `benchmark_exposure = Σ(company_exposure × reference_wt)`; `active_tilt = portfolio − benchmark`
    - Per-company `contribution = company_exposure × portfolio_wt`; unmatched companies contribute 0 but remain in the contributions list, flagged
    - Return the `PortfolioMetrics` dict
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 10.4_
    - _Design: Function signatures (calculate_portfolio_metrics), Low-Level Algorithm G, Data Models (PortfolioMetrics)_

  - [x]* 8.2 Write property test for portfolio metrics identities
    - **Property 12: Portfolio metrics identities**
    - **Validates: Requirements 7.1, 7.2, 7.3**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 12: ...`

  - [x]* 8.3 Write property test for contribution identity and additivity
    - **Property 13: Contribution identity and additivity**
    - **Validates: Requirements 7.4, 7.1**
    - Tag: `# Feature: thematic-portfolio-analysis, Property 13: ...`

- [x] 9. Checkpoint - deterministic engine complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. LLM taxonomy generation
  - [x] 10.1 Implement `generate_taxonomy(theme)` with the single Groq call
    - Load `GROQ_API_KEY` via the config helper; return `MISSING_API_KEY` before any network call when empty/missing
    - One Groq call, model `openai/gpt-oss-120b`, `response_format={"type":"json_object"}`, `temperature=0`, a generous `max_tokens` (at least ~1200) because `openai/gpt-oss-120b` is a reasoning model that emits internal reasoning tokens before the JSON, so a small budget truncates the answer and causes a `json_validate_failed` error; fallback extraction of first balanced `{...}` block
    - Parse and validate the two-layer shape (3–4 sub-industries, each 2–3 activities, all strings); lenient count clamping, strict structure/types
    - One repair/retry on bad JSON; map auth/network/timeout exceptions to `INVALID_API_KEY`/`LLM_UNREACHABLE`; return `LLM_BAD_JSON` after repair fails
    - Return `{"ok": True, "taxonomy": Taxonomy}` on success
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 10.1, 10.2_
    - _Design: Function signatures (generate_taxonomy), LLM Taxonomy Design, Data Models (Taxonomy)_

  - [x]* 10.2 Write mocked-Groq integration tests for `generate_taxonomy`
    - Valid JSON first try → success; malformed first then valid on repair → success with exactly one repair; malformed twice → `LLM_BAD_JSON`; empty key → `MISSING_API_KEY` with no network call
    - Pure-shape validator tests: accepts well-formed object; rejects wrong types, missing keys, out-of-range counts
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 10.1, 10.2_
    - _Design: Integration tests (mocked LLM), Example-based unit tests_

- [x] 11. Streamlit UI and end-to-end wiring (Phase 1)
  - [x] 11.1 Build the sidebar: portfolio selection and Groq status
    - Portfolio selector defaulting to `data/portfolio_80.xlsx`, with CSV/XLSX upload; keep the last good portfolio in `st.session_state` so a failed upload shows a message and does not overwrite it
    - Groq API status indicator reflecting whether `GROQ_API_KEY` is present/reachable
    - Render handled-error results from `load_portfolio`/`load_segments` via the `{"ok": bool}` convention without crashing
    - _Requirements: 8.1, 8.2, 1.5, 1.6, 1.7, 10.3_
    - _Design: Architecture (Sidebar), Error Handling (upload resilience)_

  - [x] 11.2 Build the theme input, sample chips, and executive KPI row
    - Theme input box in the top bar; three sample chips ("Critical Power Grid Modernization", "AI Server Hardware & Datacenter Supply Chain", "Autonomous Driving & Smart Mobility") that populate the box
    - On Analyze: call `generate_taxonomy`, then `score_company` per matched company, then `calculate_portfolio_metrics`; render the KPI row (Portfolio_Thematic_Exposure, Benchmark_Exposure, Active_Tilt) in institutional metric cards; dark/light mode support
    - Render taxonomy error banners via `{"ok": bool}` without crashing
    - _Requirements: 8.3, 8.4, 8.5, 8.6, 9.1, 10.1, 10.2_
    - _Design: Architecture (Top bar, KPI row), Error Handling, Sequence of a Full Analysis Run_

  - [x] 11.3 Build taxonomy display, Top Contributors table, and company drill-down
    - Taxonomy as a tree or expandable badges (Layer 1 sub-industries, Layer 2 activities)
    - Top Contributors table with Company, Sector, Portfolio Wt, Thematic Exposure %, Contribution to Portfolio Exposure
    - Company drill-down: segment-breakdown bar chart, segment revenue weights, and evidence snippet; companies with no segment data flagged with 0% exposure rather than failing
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 10.4_
    - _Design: Architecture (Taxonomy badges, Top Contributors, Drill-down), Data Models (ScoreResult, PortfolioMetrics)_

- [x] 12. Checkpoint - local smoke run (Phase 1 complete)
  - Run an end-to-end Phase 1 analysis on `data/portfolio_80.xlsx` with a sample theme; confirm `calculate_portfolio_metrics` returns a `PortfolioMetrics` dict with no exceptions and the KPI row renders
  - Ensure all tests pass, ask the user if questions arise.

- [ ]* 13. Phase 2 (later): Supabase authentication
  - [ ]* 13.1 Implement auth in `supabase_client.py`
    - Email/password `sign_up`, `sign_in_with_password`, `sign_out` via the `supabase` client; map each User_Profile 1:1 to a Supabase auth user id
    - Read `SUPABASE_URL`/`SUPABASE_ANON_KEY` through the config helper (`.env` locally, `st.secrets` on Cloud); keep this module fully isolated so `engine.py` and the Phase 1 path never import it
    - Return handled errors so unreachable/failed auth does not crash
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.6, 11.7_
    - _Design: Phase 2 Design — Authentication, Configuration_

  - [ ]* 13.2 Create the Supabase schema and RLS policies
    - `profiles` and `saved_portfolios` tables with the index, and RLS policies restricting each row to its owning `auth.uid()`
    - _Requirements: 11.4, 12.5_
    - _Design: Phase 2 Design — Schema, Row-Level Security_

- [ ]* 14. Phase 2 (later): saved-portfolio persistence and session wiring
  - [ ]* 14.1 Implement saved_portfolios CRUD storing parsed JSON rows
    - Upsert the parsed/validated portfolio as `jsonb` keyed by (user_id, name); restrict access to the owning profile
    - _Requirements: 12.1, 12.4, 12.5_
    - _Design: Phase 2 Design — Storage recommendation, Persistence behavior_

  - [ ]* 14.2 Wire Streamlit session handling and auto-load, keeping anonymous Phase 1 working
    - Sidebar login/logout; store session in `st.session_state["auth"]`; auto-load a saved portfolio on login, with a selector when there is more than one
    - On saved-portfolio load failure, fall back to the default portfolio with a message; unauthenticated users still get the full Phase 1 experience
    - _Requirements: 11.5, 11.6, 12.2, 12.3, 12.6_
    - _Design: Phase 2 Design — Authentication (session handling), Persistence behavior_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. All of Phase 2 (Tasks 13–14) is marked optional/later so the core Phase 1 platform can be built and run first.
- Each task references specific granular requirements and the design sections that specify it, for traceability.
- Checkpoints (Tasks 9 and 12) ensure incremental validation of the engine and the end-to-end Phase 1 path.
- Property-based tests (Hypothesis, ≥100 examples each) implement the 13 Correctness Properties, one test per property, each carrying the `# Feature: thematic-portfolio-analysis, Property N: ...` tag.
- Unit, fixture, and mocked-LLM integration tests cover the I/O boundaries (loaders, real fixtures, Groq) that are not property-tested per the design's Testing Strategy.
- `engine.py` has no Streamlit import; only `app.py` touches Streamlit and `supabase_client.py` isolates all Phase 2 concerns.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["3.1", "4.1"] },
    { "id": 3, "tasks": ["3.2", "4.2", "4.3", "5.1", "10.1"] },
    { "id": 4, "tasks": ["5.2", "6.1", "10.2"] },
    { "id": 5, "tasks": ["6.2", "6.3", "6.4", "7.1"] },
    { "id": 6, "tasks": ["7.2"] },
    { "id": 7, "tasks": ["7.3", "7.4", "7.5", "7.6", "7.7", "7.8", "7.9", "8.1"] },
    { "id": 8, "tasks": ["8.2", "8.3", "11.1"] },
    { "id": 9, "tasks": ["11.2"] },
    { "id": 10, "tasks": ["11.3"] },
    { "id": 11, "tasks": ["13.1"] },
    { "id": 12, "tasks": ["13.2", "14.1"] },
    { "id": 13, "tasks": ["14.2"] }
  ]
}
```
