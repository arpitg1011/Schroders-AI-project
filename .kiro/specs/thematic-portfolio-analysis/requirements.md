# Requirements Document

## Introduction

The AI-Powered Thematic Portfolio Analysis prototype helps Schroders (APAC asset management) measure how exposed an equity portfolio is to a free-form investment theme (for example, a macro or technology theme). The user enters a theme in natural language. An LLM decomposes the theme into a two-layer taxonomy of sub-industries and business activities. The system then audits an APAC portfolio against real S&P Capital IQ segment-revenue disclosures and business descriptions, and computes revenue-weighted thematic exposure at the company and portfolio level, with company-level drill-down.

The LLM is used only for taxonomy expansion. All exposure percentages are computed deterministically from reported segment revenue; the LLM never invents portfolio weights or exposure numbers.

This is a prototype. It currently operates on the ~20 portfolio companies that have S&P Capital IQ segment data and is designed to scale to all 80 portfolio companies without redesign.

The platform is built in two phases. Phase 1 delivers the core thematic analysis running locally (engine plus Streamlit UI operating on the default or an uploaded portfolio) with no login. Phase 2 adds user accounts and persistence so an analyst can log in and have a previously uploaded portfolio load automatically. User accounts and persistence are provided by Supabase (managed Postgres with built-in authentication, free tier), chosen so the application can later be deployed to Streamlit Community Cloud without changing the storage approach. The analysis functionality in Requirements 1 through 10 must work in Phase 1 independently of whether a user is logged in.

## Glossary

- **System**: The AI-Powered Thematic Portfolio Analysis application, comprising the analysis engine (engine.py) and the Streamlit user interface (app.py).
- **Taxonomy_Generator**: The engine component that calls the LLM to decompose a theme into a two-layer taxonomy.
- **Company_Scorer**: The engine component that computes a single company's thematic exposure from its S&P segment data and the taxonomy.
- **Portfolio_Calculator**: The engine component that aggregates company exposures into portfolio-level metrics.
- **Data_Loader**: The engine component that reads and cleans the portfolio file and the S&P segment file.
- **UI**: The Streamlit front end (app.py) that presents inputs, metrics, and drill-downs.
- **LLM**: The large language model service, accessed through the Groq API using model `openai/gpt-oss-120b` via the `groq` Python client.
- **Theme**: A free-form macro or technology investment theme entered by the user (for example "Critical Power Grid Modernization").
- **Taxonomy**: A two-layer tree produced by the Taxonomy_Generator. Layer 1 is 3-4 sub-industries; Layer 2 is 2-3 business activities (keywords) per sub-industry.
- **Sub_Industry**: A Layer 1 node in the Taxonomy.
- **Business_Activity**: A Layer 2 keyword or phrase under a Sub_Industry.
- **Portfolio_File**: An Excel or CSV file of portfolio holdings. The default is `data/portfolio_80.xlsx`.
- **Segment_File**: The S&P Capital IQ export `data/spglobal_segments.xlsx` containing segment names, segment revenues, and business descriptions.
- **Segment**: A reported business segment of a company, consisting of a segment name and a segment revenue, with up to 5 segments per company.
- **Segment_Rev_Pct**: A segment's share of a company's total positive segment revenue, expressed so that a company's segment percentages sum to 100%.
- **Alignment_Score**: A deterministic value S in the range [0.0, 1.0] representing how well a Segment matches the Taxonomy Business_Activities. It is derived from Semantic_Similarity computed by the Embedding_Model (not from keyword overlap).
- **Company_Exposure**: A company's revenue-weighted thematic exposure, expressed as a percentage from 0% to 100%, equal to the sum over segments of (Segment_Rev_Pct * Alignment_Score).
- **Portfolio_Thematic_Exposure**: The portfolio-weighted sum of Company_Exposure using the Portfolio weight column.
- **Benchmark_Exposure**: The reference-weighted sum of Company_Exposure using the Reference weight column.
- **Active_Tilt**: Portfolio_Thematic_Exposure minus Benchmark_Exposure.
- **GROQ_API_KEY**: The Groq API credential read from a `.env` file.
- **Evidence_Snippet**: An excerpt extracted from a company's business description that supports a Segment's Alignment_Score.
- **Auth_Service**: The component that authenticates users via Supabase and manages login sessions.
- **User_Profile**: A stored record identifying a user and owning that user's saved portfolios, persisted in Supabase.
- **Saved_Portfolio**: A Portfolio_File that a logged-in user has uploaded and stored under their User_Profile for automatic reloading in later sessions.
- **Embedding_Model**: An offline, deterministic static sentence-embedding model (the `model2vec` library, model "minishlab/potion-base-8M") used to compute semantic similarity between company segment text and taxonomy sub-themes. It runs locally with no network call and is not the generative LLM.
- **Semantic_Similarity**: The cosine similarity, in the range [0.0, 1.0], between the Embedding_Model vector of a Segment's text and the vector of a Business_Activity or Sub_Industry.
- **Confidence_Score**: A per-company value in the range [0.0, 1.0] indicating how strongly the available evidence supports the company's thematic classification.
- **Sub_Theme_Attribution**: The decomposition of a company's or the portfolio's Company_Exposure across the taxonomy's Sub_Industries, showing how much exposure each sub-theme contributes.

## Requirements

### Requirement 1: Data Files and Portfolio Input

**User Story:** As a Schroders analyst, I want the prototype to load the supplied portfolio and S&P segment files and optionally accept my own portfolio, so that I can analyze either the default universe or my own holdings.

#### Acceptance Criteria

1. THE System SHALL store the two source files under a `data/` folder named `portfolio_80.xlsx` and `spglobal_segments.xlsx`.
2. WHEN the System starts, THE Data_Loader SHALL load `data/portfolio_80.xlsx` as the default Portfolio_File.
3. THE Data_Loader SHALL read the Portfolio_File from a single sheet with the header on row index 0 and the columns Company, Bloomberg ticker, Country, Sector, SEDOL, Portfolio wt, Reference wt, and Active wt.
4. THE Data_Loader SHALL treat the Portfolio wt and Reference wt values as decimal fractions in which 0.132 represents 13.2%.
5. WHERE the user uploads a custom portfolio in CSV or XLSX format, THE Data_Loader SHALL use the uploaded file as the Portfolio_File in place of the default.
6. IF a required portfolio column is missing from an uploaded Portfolio_File, THEN THE UI SHALL display a message that names the missing column and SHALL retain the previously loaded Portfolio_File.
7. IF the default Portfolio_File or the Segment_File is missing from the `data/` folder, THEN THE System SHALL display a message that names the missing file and SHALL continue running without crashing.

### Requirement 2: S&P Segment Data Parsing

**User Story:** As a Schroders analyst, I want the system to correctly parse the messy S&P Capital IQ export, so that company segment names, segment revenues, and business descriptions are read accurately.

#### Acceptance Criteria

1. THE Data_Loader SHALL read the Segment_File from the sheet named "Sheet1" and SHALL ignore the sheet named "Screening Criteria".
2. THE Data_Loader SHALL treat row index 4 as the header row, SHALL skip the period row at row index 5 and the blank row at row index 6, and SHALL read company data starting at row index 7.
3. THE Data_Loader SHALL read the columns SP_ENTITY_NAME, SP_ENTITY_ID, SP_COMPANY_NAME, SP_EXCHANGE, SP_LONG_BUSINESS_DESCRIPTION, IQ_INDUSTRY_CLASSIFICATION, IQ_SECTOR, IQ_INDUSTRY_GROUP, IQ_INDUSTRY, and IQ_PRIMARY_INDUSTRY from the header row.
4. THE Data_Loader SHALL read the 5 segment names positionally from the 5 repeated IQ_BUS_SEG_ABS columns and the 5 segment revenues positionally from the 5 repeated IQ_BUS_SEG_REV_ABS columns, rather than by header name, because the headers repeat identically.
5. THE Data_Loader SHALL pair the segment name at position i with the segment revenue at position i for each of the 5 positions (i from 1 to 5).
6. IF a position has a segment name but a blank or "NA" revenue, or a revenue but a blank name, THEN THE Data_Loader SHALL retain the available value and mark the missing counterpart as absent rather than discarding the row.
7. WHEN SP_ENTITY_NAME contains a parenthetical in the format "Name (EXCHANGE:TICKER)", THE Data_Loader SHALL extract the exchange and ticker from it.
8. IF the parenthetical ticker is absent or does not match the "EXCHANGE:TICKER" pattern, THEN THE Data_Loader SHALL leave the exchange and ticker unset for that company and SHALL continue parsing.
9. WHEN a segment revenue value is numeric, THE Data_Loader SHALL interpret it as an amount stated in thousands of the local reporting currency.
10. IF a segment revenue value is non-numeric and not "NA", THEN THE Data_Loader SHALL treat that revenue as absent and SHALL continue parsing.
11. IF the Segment_File does not contain a sheet named "Sheet1", THEN THE Data_Loader SHALL return a handled error that THE UI displays without crashing.
12. IF any required named column from criterion 3 is absent on the header row, THEN THE Data_Loader SHALL return a handled error that names the missing column and THE UI displays without crashing.

### Requirement 3: Portfolio-to-Segment Company Matching

**User Story:** As a Schroders analyst, I want portfolio holdings matched to their S&P segment records, so that each company is scored against the correct underlying data.

#### Acceptance Criteria

1. THE Data_Loader SHALL match each portfolio Company to a Segment_File record using the portfolio Company name, the portfolio Bloomberg ticker, and the S&P SP_ENTITY_NAME, SP_COMPANY_NAME, and parenthetical ticker.
2. THE Data_Loader SHALL perform the match using normalized comparison that is case-insensitive, given that portfolio Company names are uppercase.
3. WHERE an exact normalized match is not found, THE Data_Loader SHALL apply fuzzy name matching to associate a portfolio Company with a Segment_File record.
4. IF a portfolio Company has no matching Segment_File record, THEN THE Company_Scorer SHALL assign that company a Company_Exposure of 0% and THE UI SHALL flag that company as having no segment data.
5. THE System SHALL analyze the portfolio companies that have matching segment data, SHALL support the current prototype set of approximately 20 such companies, and SHALL process up to 80 portfolio companies without redesign.

### Requirement 4: Revenue Cleaning and Segment Weighting

**User Story:** As a Schroders analyst, I want negative and non-revenue line items excluded from segment math, so that segment revenue percentages are accurate and sum to 100%.

#### Acceptance Criteria

1. THE Company_Scorer SHALL clamp any negative segment revenue to 0 before computing segment percentages.
2. THE Company_Scorer SHALL clamp to 0 the revenue of elimination and write-off line items including "Intersegment Elimination", "Adjustment and Write-Off", "Internal Write-Off", and "Number of Write-Offs".
3. THE Company_Scorer SHALL treat a segment revenue value of "NA" as an absent segment and SHALL exclude it from the computation.
4. THE Company_Scorer SHALL compute Segment_Rev_Pct for segment i as max(0, SegmentRev_i) divided by the sum over all segments j of max(0, SegmentRev_j).
5. WHEN at least one segment has positive revenue, THE Company_Scorer SHALL produce Segment_Rev_Pct values that sum to 100% for that company.
6. IF a company has no segment with positive revenue, THEN THE Company_Scorer SHALL assign that company a Company_Exposure of 0%.

### Requirement 5: LLM Taxonomy Generation

**User Story:** As a Schroders analyst, I want a theme decomposed into a structured two-layer taxonomy, so that company segments can be matched against concrete sub-industries and activities.

#### Acceptance Criteria

1. WHEN the user submits a Theme, THE Taxonomy_Generator SHALL call the LLM exactly once using model `openai/gpt-oss-120b` through the Groq API.
2. THE Taxonomy_Generator SHALL request and parse a strict JSON response representing a two-layer Taxonomy.
3. THE Taxonomy_Generator SHALL produce a Layer 1 of 3 to 4 Sub_Industries.
4. THE Taxonomy_Generator SHALL produce a Layer 2 of 2 to 3 Business_Activities for each Sub_Industry.
5. THE Taxonomy_Generator SHALL read the GROQ_API_KEY from a `.env` file.
6. IF the GROQ_API_KEY is missing or invalid, THEN THE Taxonomy_Generator SHALL return a handled error and THE UI SHALL display a message indicating that the Groq API key is missing or invalid.
7. IF the LLM response is not valid JSON or does not conform to the two-layer structure, THEN THE Taxonomy_Generator SHALL attempt one repair or retry and, failing that, SHALL return a handled error that THE UI SHALL display without crashing.

### Requirement 6: Deterministic Company Scoring

**User Story:** As a Schroders analyst, I want each company's thematic exposure computed deterministically from its segments, so that the exposure figures are reproducible and auditable.

#### Acceptance Criteria

1. THE Company_Scorer SHALL compute an Alignment_Score for each Segment as the maximum Semantic_Similarity, computed by the Embedding_Model, between the Segment's text (its name combined with the business description) and the Taxonomy's Business_Activities.
2. THE Company_Scorer SHALL constrain each Alignment_Score to the inclusive range 0.0 to 1.0 rounded to 4 decimal places.
3. THE Company_Scorer SHALL apply a configurable Semantic_Similarity threshold below which a Segment's Alignment_Score is set to 0.0, so that clearly unrelated categories such as "Kitchen Hardware", "Real Estate", and "Consumer DX" do not accrue spurious exposure.
4. WHEN scoring a company, THE Company_Scorer SHALL compute Company_Exposure as the sum over all segments of (Segment_Rev_Pct_i * Alignment_Score_i), where Segment_Rev_Pct_i is the segment's reported revenue percentage.
5. THE Company_Scorer SHALL express Company_Exposure as a percentage constrained to the inclusive range 0% to 100% rounded to 2 decimal places.
6. THE Company_Scorer SHALL return the complete set of matched Sub_Industries and Business_Activities for the company, returning an empty set when no matches exist.
7. THE Company_Scorer SHALL return an Evidence_Snippet of between 1 and 500 characters extracted verbatim from the company's business description that supports the highest non-zero Alignment_Score.
8. IF no Segment achieves an Alignment_Score greater than 0.0, THEN THE Company_Scorer SHALL return an empty Evidence_Snippet and a Company_Exposure of 0%.
9. THE Company_Scorer SHALL derive Company_Exposure only from reported segment revenue percentages and Alignment_Scores, SHALL NOT invoke the generative taxonomy LLM to assign exposure percentages, and SHALL produce an identical Company_Exposure for identical inputs on every execution. The Embedding_Model used for Semantic_Similarity is a static, offline model that is deterministic (identical text yields identical vectors), so scoring remains reproducible.
10. IF a Segment is missing a reported revenue percentage, THEN THE Company_Scorer SHALL exclude that Segment from the Company_Exposure sum and SHALL record an indication that the Segment was excluded for missing revenue data.

### Requirement 7: Portfolio-Level Metrics

**User Story:** As a Schroders analyst, I want aggregated portfolio exposure, benchmark exposure, and active tilt, so that I can judge the portfolio's positioning against the theme and the benchmark.

#### Acceptance Criteria

1. THE Portfolio_Calculator SHALL compute Portfolio_Thematic_Exposure as the sum over companies of (Company_Exposure * Portfolio wt).
2. THE Portfolio_Calculator SHALL compute Benchmark_Exposure as the sum over companies of (Company_Exposure * Reference wt).
3. THE Portfolio_Calculator SHALL compute Active_Tilt as Portfolio_Thematic_Exposure minus Benchmark_Exposure.
4. THE Portfolio_Calculator SHALL compute each company's Contribution to Portfolio Exposure as Company_Exposure multiplied by Portfolio wt.

### Requirement 8: User Interface Inputs and Status

**User Story:** As a Schroders analyst, I want to select a portfolio, enter or pick a theme, and see the Groq connection status, so that I can start an analysis quickly and know the system is ready.

#### Acceptance Criteria

1. THE UI SHALL provide a sidebar with portfolio selection that defaults to the preloaded `data/portfolio_80.xlsx` and allows uploading a custom CSV or XLSX portfolio.
2. THE UI SHALL display a Groq API status indicator in the sidebar reflecting whether the GROQ_API_KEY is present and the API is reachable.
3. THE UI SHALL provide a theme input box in the top bar for entering a free-form Theme.
4. THE UI SHALL accept any free-form Theme typed into the theme input box.
5. THE UI SHALL support dark mode and light mode and SHALL present metrics in institutional financial metric cards.

### Requirement 9: User Interface Results and Drill-Down

**User Story:** As a Schroders analyst, I want to see headline exposure metrics, the taxonomy, top contributors, and per-company evidence, so that I can defend the analysis to an investment committee.

#### Acceptance Criteria

1. WHEN an analysis completes, THE UI SHALL display an executive KPI row showing Portfolio_Thematic_Exposure as a percentage, Benchmark_Exposure as a percentage, and Active_Tilt as a percentage.
2. THE UI SHALL display the Taxonomy as a tree or expandable badges showing Layer 1 Sub_Industries and Layer 2 Business_Activities.
3. THE UI SHALL display a Top Contributors table with the columns Company, Sector, Portfolio Wt, Thematic Exposure %, and Contribution to Portfolio Exposure.
4. WHEN the user opens a company drill-down, THE UI SHALL display a bar chart of that company's Capital IQ segment breakdown, the segment revenue weights, and the Evidence_Snippet from the business description that backs the score.
5. WHERE a company has no segment data, THE UI SHALL present that company as flagged with a Company_Exposure of 0% rather than failing.

### Requirement 10: Error Handling and Resilience

**User Story:** As a Schroders analyst, I want the prototype to fail gracefully on missing keys, bad data, or unmatched companies, so that a single problem never crashes the whole analysis.

#### Acceptance Criteria

1. IF the GROQ_API_KEY is missing or invalid, THEN THE System SHALL present a user-facing error and SHALL continue running without crashing.
2. IF the LLM returns a malformed or non-JSON response, THEN THE System SHALL attempt one repair or retry and SHALL present a user-facing error if the repair fails.
3. IF the Segment_File or default Portfolio_File is missing, THEN THE System SHALL present a message naming the missing file and SHALL continue running without crashing.
4. IF a portfolio Company has no S&P segment data, THEN THE System SHALL assign a Company_Exposure of 0%, SHALL flag the company, and SHALL continue the analysis for the remaining companies.

### Requirement 11: User Authentication and Profiles (Phase 2)

**User Story:** As a Schroders analyst, I want to create an account and log in, so that the platform can recognize me and keep my portfolios separate from other users'.

#### Acceptance Criteria

1. THE Auth_Service SHALL allow a user to register and log in using Supabase authentication.
2. WHEN a user logs in with valid credentials, THE Auth_Service SHALL establish an authenticated session for that user.
3. WHEN a user logs out, THE Auth_Service SHALL end the authenticated session.
4. THE System SHALL associate each User_Profile with exactly one authenticated user identity.
5. IF a user is not authenticated, THEN THE System SHALL still provide the full Phase 1 analysis functionality using the default or an uploaded portfolio.
6. IF authentication fails or the Auth_Service is unreachable, THEN THE System SHALL present a user-facing error and SHALL continue to allow unauthenticated Phase 1 analysis without crashing.
7. THE System SHALL store Supabase connection and credential configuration outside of source code so that it can differ between local development and Streamlit Community Cloud deployment.

### Requirement 12: Saved Portfolio Persistence (Phase 2)

**User Story:** As a logged-in Schroders analyst, I want my uploaded portfolio stored to my profile, so that it loads automatically the next time I log in without re-uploading.

#### Acceptance Criteria

1. WHEN an authenticated user uploads a Portfolio_File, THE System SHALL store it as a Saved_Portfolio under that user's User_Profile in Supabase.
2. WHEN an authenticated user with a Saved_Portfolio starts a session, THE System SHALL load that Saved_Portfolio automatically as the active Portfolio_File.
3. WHERE an authenticated user has more than one Saved_Portfolio, THE System SHALL allow the user to select which Saved_Portfolio to load.
4. WHEN an authenticated user uploads a new Portfolio_File, THE System SHALL update or add the Saved_Portfolio for that user.
5. THE System SHALL restrict access to a Saved_Portfolio to the User_Profile that owns it.
6. IF loading a Saved_Portfolio fails, THEN THE System SHALL fall back to the default portfolio and SHALL present a user-facing message without crashing.

### Requirement 13: Reproducibility via Taxonomy Caching

**User Story:** As a Schroders analyst, I want the same theme to produce the same results every time, so that my analysis is reproducible and defensible to an investment committee.

#### Acceptance Criteria

1. WHEN a Theme is submitted that was generated before, THE Taxonomy_Generator SHALL reuse the previously generated and cached Taxonomy rather than calling the LLM again.
2. THE System SHALL key the Taxonomy cache by the exact Theme string.
3. GIVEN the same cached Taxonomy and the same portfolio and Segment data, THE System SHALL produce identical exposure results on every run.
4. THE System SHALL allow the user to force regeneration of a Taxonomy, bypassing the cache.
5. THE System SHALL treat caching as the mechanism that delivers run-to-run reproducibility for a given Theme, because a temperature of 0 does not by itself guarantee identical LLM output.

### Requirement 14: Confidence Scores and Sub-Theme Attribution

**User Story:** As a Schroders analyst, I want a confidence score and a per-sub-theme breakdown for each company's exposure, so that I can judge how much to trust a number and see which parts of the theme drive it.

#### Acceptance Criteria

1. THE Company_Scorer SHALL return a Confidence_Score in the range [0.0, 1.0] per company derived from the strength of its Segment Semantic_Similarity matches and the share of revenue that is matched.
2. THE Company_Scorer SHALL attribute each company's Company_Exposure across the Taxonomy Sub_Industries as a Sub_Theme_Attribution, and the attributions SHALL sum to the company's Company_Exposure.
3. THE Portfolio_Calculator SHALL aggregate Sub_Theme_Attribution to the portfolio level so that Portfolio_Thematic_Exposure can be broken down by Sub_Industry.
4. WHERE a company's Segment data is insufficient to classify it, THE System SHALL flag the company as low confidence or insufficient data rather than reporting a misleadingly precise exposure.

### Requirement 15: Taxonomy and Sub-Theme Visualization

**User Story:** As a Schroders analyst, I want to see the theme's sub-theme hierarchy clearly and how exposure maps to each sub-theme, so that I understand what the system is actually measuring.

#### Acceptance Criteria

1. THE UI SHALL display the generated Taxonomy as a clear hierarchy, such as a tree or inverted-pyramid layout, that makes the Layer 1 Sub_Industries and their Layer 2 Business_Activities visible at a glance.
2. THE UI SHALL show, for the portfolio, the exposure attributed to each Sub_Industry (Sub_Theme_Attribution).
3. WHEN the user drills into a company, THE UI SHALL show which Sub_Industries that company contributes to and the per-sub-theme breakdown.
