# ThemeIQ — AI-Powered Thematic Portfolio Analysis

*BU5203 Project · Sponsored problem: AI-Powered Thematic Analysis for Investment Research (Schroders)*

---

## 1. The problem, in plain English

Big investors like Schroders don't just buy "tech stocks" or "energy stocks."
They increasingly think in **themes** — long-term structural shifts like *AI
infrastructure*, *the energy transition*, *digital payments*, or *critical power
grid modernization*. The question a portfolio manager (PM) constantly asks is:

> **"How exposed is my portfolio to this theme — really?"**

Today they answer this with blunt tools:

- **Sector labels** ("this is an Industrials stock") — too broad. A company
  labelled "Industrials" might get 5% or 95% of its revenue from the theme you
  care about. The label doesn't tell you.
- **Yes/No thematic tags** ("this stock is an AI stock") — too binary. Being
  "an AI stock" doesn't say whether AI is 10% or 80% of the business.

Both **overstate or understate** how much a company really touches a theme, and
neither sees the **ripple effects** down a supply chain (e.g. an AI boom drives
demand for data centres → electricity → power grids → copper → mining companies).

**ThemeIQ fixes this.** You type any theme in plain language. ThemeIQ breaks it
into sub-themes, reads each company's *actual reported business segments*, and
calculates — with real numbers, not guesses — what percentage of the portfolio
is genuinely exposed to that theme, how that compares to the benchmark, and
which companies drive it. It then layers on recent **news** to show whether a
current event is pushing exposure up or down right now.

---

## 2. A worked example (read this first)

Say a PM types the theme: **"Semiconductors."**

**Step 1 — ThemeIQ breaks the theme into sub-themes.** An AI model turns the
one-word theme into a small, structured tree:

```
Semiconductors
├─ Logic Integrated Circuits      (CPU design, GPU architecture, ASIC design)
├─ Memory Devices                 (DRAM, NAND flash, SRAM)
├─ Analog & Mixed-Signal          (power management ICs, sensors)
└─ Semiconductor Foundry Services (wafer fabrication, advanced packaging)
```

**Step 2 — ThemeIQ reads each company's real business segments.** From S&P
Capital IQ data (sourced from companies' own financial filings), it knows, for
example, that **TSMC** reports 100% of revenue from a segment called "Foundry,"
while **Samsung** splits revenue across "Device Solutions" (chips), "Device
Experience" (phones/TVs), "Harman" (car audio), and "Displays."

**Step 3 — ThemeIQ scores each segment against the theme.** It measures how
closely each business segment matches the sub-themes:

| Company | Segment | % of its revenue | Match to theme | 
|---|---|---|---|
| TSMC | Foundry | 100% | very high (0.75) |
| Samsung | Device Solutions (chips) | 36% | medium (0.45) |
| Samsung | Device Experience (phones/TVs) | 52% | ~0 (not semiconductors) |
| Commonwealth Bank | Retail Banking | 100% | ~0 (not semiconductors) |

**Step 4 — ThemeIQ computes exposure with simple, auditable arithmetic:**

```
Company exposure = Σ (segment's % of revenue × how well it matches the theme)

TSMC    = 100% × 0.75                     ≈ 75%  (a core semiconductor play)
Samsung = (36% × 0.45) + (52% × 0) + ...  ≈ 39%  (partly a chip company)
CBA     = 100% × 0                        =  0%  (a bank — correctly excluded)
```

**Step 5 — ThemeIQ rolls companies up to the portfolio level**, weighting each
by how much of the portfolio it represents, and compares to the benchmark:

| Metric | Value | Meaning |
|---|---|---|
| **Portfolio thematic exposure** | 19% | ~a fifth of the portfolio is tied to semiconductors |
| **Benchmark exposure** | 21% | the market index is slightly more exposed |
| **Active tilt** | −2% | this portfolio is *underweight* the theme vs the benchmark |

**Step 6 (optional) — News overlay.** The PM clicks "Fetch news." ThemeIQ reads
recent headlines, and if a material event is happening (say, new rare-earth
export curbs affecting chip supply chains), it shows a **separate, clearly
labelled adjustment** — e.g. *"+2% from recent events"* — with the headlines and
reasoning behind it. The underlying 19% baseline never silently changes.

That's the whole product: **turn a fuzzy theme into a defensible number, broken
down by sub-theme and company, with evidence.**

---

## 3. What you see on screen

ThemeIQ is a web dashboard (branded for Schroders). After logging in and
uploading a portfolio, you type a theme and get five tabs:

- **Overview** — the three headline numbers (portfolio %, benchmark %, active
  tilt) as big metric cards, a dial showing portfolio exposure, and a donut
  chart breaking exposure down by sub-theme. Plus a **data-coverage panel**
  that honestly shows how many holdings we could classify vs. flag.
- **Taxonomy** — the theme broken into sub-themes and activities, shown as a
  hierarchy with each sub-theme's share of the exposure.
- **Contributors** — a ranked table of which companies drive the exposure, each
  with a confidence level.
- **Company Detail** — drill into any holding: its segment breakdown, how each
  segment matched, the confidence score, and the source data behind it.
- **News & Events** — the recent-news overlay (described above), opt-in via a
  button so it only runs when you ask.

---

## 4. How the numbers are calculated — and why they stay consistent

This is the most important design decision in the whole project, so it gets its
own section. **An investment committee will not trust a number it cannot
defend.** So ThemeIQ is built around one principle:

> **The AI breaks the theme into words. Plain, repeatable arithmetic turns those
> words into the exposure numbers. The AI never makes up a percentage.**

### 4a. Where the exposure number actually comes from

The headline exposure is pure arithmetic on real reported revenue:

```
Company exposure  = Σ over segments ( segment's revenue share × match score )
Portfolio exposure = Σ over companies ( company exposure × portfolio weight )
Benchmark exposure = Σ over companies ( company exposure × benchmark weight )
Active tilt        = Portfolio exposure − Benchmark exposure
```

- **Revenue shares** come straight from S&P Capital IQ segment data (the
  companies' own filings). We clean these first — negative accounting lines and
  "elimination/write-off" items are set to zero so the percentages are sensible
  and sum to 100%.
- **Match score (0 to 1)** is how closely a segment relates to the theme. This
  is computed with a **mathematical text-similarity model that runs offline on
  our own machine** (not a chatbot). The same text always produces the exact
  same score — it is deterministic.

Because every step is arithmetic over fixed inputs, **the same portfolio and the
same theme always produce the same exposure number.** You can hand the
calculation to an auditor.

### 4b. The one place AI is used (and why it's safe)

The AI language model is used for **exactly one job**: turning your free-text
theme ("semiconductors") into the structured sub-theme tree. That's a language
task AI is genuinely good at, and getting it slightly differently worded doesn't
break anything because the matching step is based on *meaning*, not exact words
("renewables" and "renewable energy" score as near-identical).

To keep even this consistent, **the sub-theme tree for a given theme is cached.**
Type "semiconductors" today and tomorrow — you get the same tree, so you get the
same exposure. (We also validate that the input is actually a real investment
theme — type your name and it politely refuses rather than inventing nonsense.)

### 4c. Why we are frugal with AI calls

AI calls cost money and add latency and variability, so we minimise them by
design. **A full analysis of an 80-stock portfolio uses only ONE AI call** — the
one that builds the sub-theme tree. Scoring all 80 companies across all their
segments uses **zero** AI calls; it's all the offline similarity model and
arithmetic. The only other AI call in the entire system is an optional **second
call** for the news overlay, and only if you click the news button.

| Task | How often | What it uses |
|---|---|---|
| Break theme into sub-themes | **1 AI call** per theme (then cached) | AI language model |
| Score 80 companies' segments | **0 AI calls** | offline math model + arithmetic |
| Roll up to portfolio metrics | **0 AI calls** | arithmetic |
| News overlay | **1 AI call**, only if you click the button | AI language model |

So a normal analysis is **one AI call**, not hundreds. This is deliberate: it's
cheaper, faster, and far more reproducible than asking an AI to score every
company one by one.

### 4d. The news overlay — kept honest with a rubric

News is inherently a judgement call, so we made it **transparent and bounded**:

- The AI reads recent headlines and, for each sub-theme, judges only two things:
  a **direction** (Tailwind = raises exposure / Headwind = lowers it / Neutral)
  and a **strength** (High / Medium / Low / None).
- **We** — not the AI — convert that to points using a fixed, published table
  (High = ±4 points, Medium = ±2, Low = ±1). So the magnitude is auditable: a PM
  can see "Medium Headwind, therefore −2, because of *this cited headline*."
- The total news adjustment is **capped at ±10 points**, so a headline can
  inform the view but never overwhelm the structural, filings-based baseline.
- The baseline and the news-adjusted figure are shown as **separate numbers** —
  news never silently changes the audited baseline.

The scoring rubric is printed right on the News tab so anyone can see exactly
where the adjustment came from.

---

## 5. What data goes in

- **A portfolio file** (Excel/CSV) with holdings: company name, ticker, country,
  sector, and the portfolio & benchmark weights. The user uploads this; logged-in
  users have it saved to their profile and auto-loaded next time.
- **S&P Capital IQ segment data** (bundled) — each company's reported business
  segments, segment revenues, and business descriptions, drawn from their
  financial filings. This is the "ground truth" the exposure is built on.
- **Recent news** (optional) — fetched live from a news API when the user asks.

---

## 6. The honest limitations (so we present with integrity)

- **Segment data coverage.** The current dataset has detailed segment data for
  ~20 of the 80 holdings. Companies without it are **flagged, not hidden** — the
  dashboard shows exactly which ones we couldn't classify. More data = more
  coverage; nothing else changes.
- **The text-matching model is small.** It's fast, free, and offline, but it can
  occasionally mis-score a vaguely named segment. The confidence score flags
  when a result is shaky.
- **News is a point-in-time signal.** Unlike the baseline, the news overlay
  depends on the day's headlines and the AI's reading of them, so it is *not*
  reproducible over time — and we label it clearly as such. The baseline is the
  trustworthy anchor; news is the live overlay on top.
- **Free news API limits.** On the free tier the news lookback realistically
  only reaches back a few days regardless of the window selected; we note this
  on screen.

---

## 7. How it's built (one-paragraph technical summary)

Python throughout. `engine.py` holds all the deterministic logic (data loading,
company matching, the offline similarity scoring, and the portfolio arithmetic)
and has no UI code, which makes it independently testable — there are 40
automated tests, including property-based tests that check the maths holds for
thousands of generated cases. `app.py` is the Streamlit web dashboard (branded
UI, charts, tabs). `news.py` handles the optional news overlay. `supabase_client.py`
handles login and saving portfolios. The AI model is Groq's hosted
`openai/gpt-oss-120b`; the offline text-similarity model is `model2vec`;
accounts and saved portfolios use Supabase; the app is deployed on Streamlit
Community Cloud.

---

## 8. Running it yourself

```bash
pip install -r requirements.txt
# add your keys to a .env file (see .env.example)
streamlit run app.py
```

The app opens in your browser. Log in (or use it anonymously if accounts aren't
configured), upload a portfolio, type a theme, and click **Analyze**.

> **Keys needed:** `GROQ_API_KEY` (the AI model). Optional: `SUPABASE_URL` /
> `SUPABASE_ANON_KEY` for login & saved portfolios, and a news API key
> (`CURRENTS_API_KEY` or `NEWSDATA_API_KEY`) for the news overlay. The app runs
> without the optional ones — those features simply switch off gracefully.
