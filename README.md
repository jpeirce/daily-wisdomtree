# Daily Macro Summary Bot

An automated macro strategist that extracts, scores, and summarizes financial data from multiple high-signal sources to produce a strategic market outlook.

The system uses a **Three-Pass Intelligence Architecture** to ensure accuracy and objectivity:

1.  **Extraction (Pass 1 - Vision):** Uses **Gemini 3 Pro** to extract raw numerical data (Spreads, P/E Ratios, Yields, CME Volume/OI) from visual charts and dense tables.
2.  **Ground Truth Engine (Python):**
    *   **Deterministic Scoring:** Calculates scores (0-10) for Liquidity, Valuation, etc., using fixed financial formulas.
    *   **Live Trend Analysis:** Uses `yfinance` to fetch real-time S&P 500 data, calculating a robust 21-trading-day trend to bypass stale PDF charts.
    *   **Event Calendar:** Detects Monthly OPEX, Triple Witching, and Month-End rebalancing via a deterministic rules engine + manual overrides.
3.  **Summarization (Pass 2 - Logic Gates):** Feeds extracted data, Ground Truth scores, and the Event Context to Gemini 3 Pro. Strict **Invariant Gates** and **Event Risk Gates** mandate specific phrasing and confidence downgrades based on the market regime.

## 🚀 Features

*   **Multi-Source Synthesis:** Combines the WisdomTree Daily Dashboard, CME Daily Bulletin (Section 01), and Yahoo Finance live data.
*   **Event-Aware Intelligence:** Automatically detects expiry cycles (OPEX/Witching) and downgrades directional conviction when volume/OI signals may be distorted.
*   **Guaranteed Narrative Safety:** 
    *   **Global Constraints:** Explicitly bans "Actor Attribution" (Smart Money, Whales, Institutions).
    *   **Post-Generation Scrubbing:** A regex validator scans and normalizes LLM output to ensure structural, neutral phrasing (e.g., *"institutional flows"* -> *"market-participant flows"*).
*   **Interactive HTML Dashboard:**
    *   **Audit Trail:** Prints a detailed "Data Verification" block.
    *   **Show Formulas:** Collapsible section showing the exact Python math behind the scores.
    *   **Inputs Section:** Direct links to the source PDFs used for the run.
*   **Daily Email:** Delivers the briefing to your inbox every morning.

## 🛠️ Setup

### GitHub Secrets
Required for the GitHub Actions pipeline:
*   `AI_STUDIO_API_KEY`: For Gemini extraction and summarization.
*   `OPENROUTER_API_KEY`: (Optional) For side-by-side comparison with other models.
*   `SMTP_EMAIL`: Sender Gmail address.
*   `SMTP_PASSWORD`: Gmail App Password.
*   `RECIPIENT_EMAIL`: Target email address.

### Configuration
Controlled via `.github/workflows/summary.yml`:
*   `SUMMARIZE_PROVIDER`: Set to `GEMINI` (default), `OPENROUTER`, or `ALL`.
*   `GEMINI_MODEL`: Set to `gemini-3-pro`.

## 📊 Benchmark Arena
The `benchmark` branch allows testing the summary logic against 8+ different models (Claude, GPT-4o, etc.) without Ground Truth constraints to measure raw reasoning performance.

```bash
gh workflow run summary.yml --ref benchmark
```

## 📈 Live Dashboard
View the latest report: **[Daily Macro Summary](https://jpeirce.github.io/daily-macro-summary/)**

## Running Locally
1.  **Clone:** `git clone https://github.com/jpeirce/daily-macro-summary.git`
2.  **Install:** `pip install -r requirements.txt`
3.  **Set Env:** Provide API keys in your environment.
4.  **Run:** `python scripts/fetch_and_summarize.py`