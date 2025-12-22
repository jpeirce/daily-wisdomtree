import os
import requests
import fitz  # PyMuPDF
import smtplib
import google.generativeai as genai
import markdown
import base64
import json
import re
import math
import yfinance as yf
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import time 
from event_flags import get_event_context

# Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AI_STUDIO_API_KEY = os.getenv("AI_STUDIO_API_KEY")
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SUMMARIZE_PROVIDER = os.getenv("SUMMARIZE_PROVIDER", "ALL").upper() 
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "jpeirce/daily-macro-summary") 

PDF_SOURCES = {
    "wisdomtree": "https://www.wisdomtree.com/investments/-/media/us-media-files/documents/resource-library/daily-dashboard.pdf",
    "cme_vol": "https://www.cmegroup.com/daily_bulletin/current/Section01_Exchange_Overall_Volume_And_Open_Interest.pdf",
    "cme_sec09": "https://www.cmegroup.com/daily_bulletin/current/Section09_Interest_Rate_Futures.pdf"
}
OPENROUTER_MODEL = "openai/gpt-5.2" 
GEMINI_MODEL = "gemini-3-pro-preview" 
RUN_MODE = os.getenv("RUN_MODE", "PRODUCTION") # Options: PRODUCTION, BENCHMARK

# Benchmark Models (for RUN_MODE="BENCHMARK")
BENCHMARK_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-opus-4.5",
    "openai/gpt-5.2",
    "x-ai/grok-4.1-fast",
    "qwen/qwen3-vl-30b-a3b-thinking",
    "meta-llama/llama-4-scout",
    "nvidia/nemotron-nano-12b-v2-vl"
] 

# Noise thresholds by asset class
NOISE_THRESHOLDS = {
    "equity": 50000,
    "rates": 75000,
    "fx": 25000
}

# --- Helpers ---

def parse_int_token(tok):
    if not tok: return None
    # Remove commas AND spaces (LLM sometimes outputs "+ 123")
    t = str(tok).strip().replace(",", "").replace(" ", "")
    if t in {"", "----", "\u2014", "null", "None"}:
        return None
    if t.upper() == "UNCH":
        return 0
    try:
        return int(t)
    except:
        return None

# --- Prompts ---

EXTRACTION_PROMPT = """
You are a precision data extractor. Your job is to read the attached PDF pages (Financial Dashboard + CME Reports) and extract specific numerical data into valid JSON.

- DO NOT provide commentary, analysis, or summary.
- ONLY return a valid JSON object.
- Extract numbers as decimals.
- If a value is missing or unreadable, use `null`.

Extract the following keys:

{
  // From WisdomTree Dashboard
  "wisdomtree_as_of_date": string, // "As of" date found on WisdomTree dashboard (e.g. "Dec 19, 2025")
  "hy_spread_current": float, // High Yield Spread (e.g. 2.84)
  "hy_spread_median": float, // Historical Median HY Spread
  "forward_pe_current": float, // S&P 500 Forward P/E
  "forward_pe_median": float, // S&P 500 Forward P/E Median
  "forward_pe_plus_1sigma": float, // S&P 500 Forward P/E +1 Sigma (Standard Deviation)
  "real_yield_10y": float, // 10-Year Real Yield (TIPS)
  "inflation_expectations_5y5y": float, // 5y5y Forward Inflation Expectation
  "yield_10y": float, // 10-Year Treasury Nominal Yield
  "yield_2y": float, // 2-Year Treasury Nominal Yield
  "interest_coverage_small_cap": float, // S&P 600 Interest Coverage Ratio
  
  // From CME Section 01 Report
  "cme_bulletin_date": string, // Date at top of CME report (e.g. "2025-12-19")
  
  // --- CME Group Overall Totals ---
  // Look for the "CME GROUP TOTALS" section, specifically the "CME GROUP TOTALS" row.
  "cme_total_volume": int, // "OVERALL VOLUME" column for "CME GROUP TOTALS" row
  "cme_total_open_interest": int, // "COMBINED TOTAL" -> "OPEN INTEREST" column for "CME GROUP TOTALS" row
  "cme_total_oi_net_change": int, // "COMBINED TOTAL" -> "NET CHGE OI" column for "CME GROUP TOTALS" row
  "cme_totals_audit_label": string, // The exact row label matched (should be "CME GROUP TOTALS")

  // --- Specific Asset Class Changes (Net Change Column) ---
  
  // 1. INTEREST RATES
  "cme_rates_futures_oi_change": int, // Table "FUTURES ONLY" -> Row "INTEREST RATES" -> Column "NET CHGE OI"
  "cme_rates_futures_audit_label": string, // The exact row label matched (should be "INTEREST RATES")
  
  "cme_rates_options_oi_change": int, // Table "OPTIONS ONLY" -> Row "INTEREST RATES" -> Column "NET CHGE OI"
  "cme_rates_options_audit_label": string, // The exact row label matched (should be "INTEREST RATES")
  
  // 2. EQUITY INDEX
  "cme_equity_futures_oi_change": int, // Table "FUTURES ONLY" -> Row "EQUITY INDEX" -> Column "NET CHGE OI"
  "cme_equity_futures_audit_label": string, // The exact row label matched (should be "EQUITY INDEX")
  
  "cme_equity_options_oi_change": int, // Table "OPTIONS ONLY" -> Row "EQUITY INDEX" -> Column "NET CHGE OI"
  "cme_equity_options_audit_label": string  // The exact row label matched (should be "EQUITY INDEX")
}
"""

EXTRACTION_PROMPT_SEC09 = """
You are a precision data extractor for CME Section 09 (Interest Rate Futures).
Your task is to extract row-level totals for specific Treasury Futures tenors.

ANCHOR RULES:
1. Locate the exact row labels specified below (e.g., "TOTAL 2-YR NOTE FUTURES").
2. From that row, extract the LAST 4 numeric-ish tokens.
   - These correspond to columns: [RTH VOLUME] [GLOBEX VOLUME] [OPEN INTEREST] [NET CHGE OI]
   - "UNCH" is a valid numeric token (means 0).
   - "----" or empty is null.
3. Quality Audit: Scan the document for any lines beginning with "PLEASE NOTE" or "PRELIMINARY" and extract them.

JSON OUTPUT SCHEMA:
{
  "cme_section09": {
    "bulletin_date": "YYYY-MM-DD",
    "is_preliminary": boolean,
    "source": "CME Section 09 Interest Rate Futures",
    "totals": {
      "2y":   {"row_label": "TOTAL 2-YR NOTE FUTURES", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "3y":   {"row_label": "TOTAL 3-YR NOTE FUTURES", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "5y":   {"row_label": "TOTAL 5-YR NOTE FUTURES", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "10y":  {"row_label": "TOTAL 10-YR NOTE FUTURES", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "tn":   {"row_label": "TOTAL TN FUT", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "30y":  {"row_label": "TOTAL 30Y BOND FUT", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string},
      "ultra":{"row_label": "TOTAL ULTRA T-BND FUT", "rth_volume": string, "globex_volume": string, "open_interest": string, "oi_change": string}
    },
    "data_quality_notes": [string]
  }
}
"""

BENCHMARK_DATA_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the provided Ground Truth Data (JSON) to produce a strategic, easy-to-digest market outlook.

Inputs Provided:
1. **Ground Truth Metrics:** Extracted numerical data from WisdomTree & CME.
2. **Event Context:** Active market events (e.g., OPEX).

Format Constraints:
Length: Total output must be 700–1,000 words.
Tables: The "Dashboard Scoreboard" is the only table allowed.
Formatting: Use '###' for all section headers.

Output Structure:

### 1. The Dashboard (Scoreboard)

Create a table with these 6 Dials. CALCULATE THE SCORES YOURSELF (0-10) based on the provided metrics.

| Dial | Score (0-10) | Justification (Data Source: Provided JSON) |
|---|---|---|
| Growth Impulse | [Score] | [Brief justification] |
| Inflation Pressure | [Score] | [Brief justification] |
| Liquidity Conditions | [Score] | [Brief justification] |
| Credit Stress | [Score] | [Brief justification] |
| Valuation Risk | [Score] | [Brief justification] |
| Risk Appetite | [Score] | [Brief justification] |

### 2. Executive Takeaway (5–7 sentences)
[Regime Name, The Driver, The Pivot]

### 3. The "Fiscal Dominance" Check (Monetary Stress)
[Data, Implication]

### 4. Rates & Curve Profile
[Shape, Implication]

### 5. The "Canary in the Coal Mine" (Credit Stress)
[Data, Implication]

### 6. The "Engine Room" (Market Breadth)
[Data, Implication]

### 7. Valuation & "Smart Money"
[Data, International, Implication]

### 8. Conclusion & Trade Tilt
[Cross-Asset Confirmation, Risk Rating, The Trade, Triggers]
"""

BENCHMARK_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the provided visual inputs to produce a strategic, easy-to-digest market outlook.

Inputs Provided:
1. **WisdomTree Dashboard:** General macro/market context.
2. **CME Bulletin (Section 01):** Volume and Open Interest totals.
3. **CME Rates Curve (Section 09):** Treasury futures yield curve positioning.

Format Constraints:
Length: Total output must be 700–1,000 words.
Tables: The "Dashboard Scoreboard" is the only table allowed.
Formatting: Use '###' for all section headers.

Output Structure:

### 1. The Dashboard (Scoreboard)

Create a table with these 6 Dials. CALCULATE THE SCORES YOURSELF (0-10) based on the visual data.

| Dial | Score (0-10) | Justification (Data Source: WisdomTree & CME) |
|---|---|---|
| Growth Impulse | [Score] | [Brief justification] |
| Inflation Pressure | [Score] | [Brief justification] |
| Liquidity Conditions | [Score] | [Brief justification] |
| Credit Stress | [Score] | [Brief justification] |
| Valuation Risk | [Score] | [Brief justification] |
| Risk Appetite | [Score] | [Brief justification] |

### 2. Executive Takeaway (5–7 sentences)
[Regime Name, The Driver, The Pivot]

### 3. The "Fiscal Dominance" Check (Monetary Stress)
[Data, Implication]

### 4. Rates & Curve Profile
[Shape, Implication]

### 5. The "Canary in the Coal Mine" (Credit Stress)
[Data, Implication]

### 6. The "Engine Room" (Market Breadth)
[Data, Implication]

### 7. Valuation & "Smart Money"
[Data, International, Implication]

### 8. Conclusion & Trade Tilt
[Cross-Asset Confirmation, Risk Rating, The Trade, Triggers]
"""

SUMMARY_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the provided visual inputs (Macro Dashboard & CME Bulletin) to produce a strategic, easy-to-digest market outlook.

GLOBAL CONSTRAINTS (Language & Tone):
1. **No Actor Attribution:** Do NOT use terms like "Smart Money", "Whales", "Insiders", "Institutions", "Big Players", "Professionals", "Strong Hands", "Hedge Funds", "Asset Managers", "Dealers", "Banks", "Allocators", "Real Money", "Pensions", "Sovereign", "Macro Funds", "Levered Funds", or "CTAs".
2. **Structural Phrasing:** Describe activity as "futures-led / options-led positioning" without naming specific participant types.
   * *Bad:* "Institutions are shorting aggressively."
   * *Good:* "Futures-led positioning increased; direction remains unknown unless Signal=Directional and Trend is valid."
   * *Bad:* "Smart money is buying the dip."
   * *Good:* "Options-led activity, typically associated with volatility or hedging demand, remains the primary driver."

INPUTS PROVIDED (Vision):
1. WisdomTree Daily Snapshot (Images): Charts, Spreads, and Yield Curve data.
2. CME Daily Bulletin (Images): Dense tables showing Volume and Open Interest (Commitment).

CRITICAL: You have been provided with PRE-CALCULATED Ground Truth Scores, raw Extracted Metrics, and deterministic Signal Labels below.
You MUST use these exact scores and signals. Do NOT attempt to recalculate them.

Ground Truth & Extracted Metrics (Use these values exactly):
{ground_truth_json}

EVENT CONTEXT (Deterministic Flags):
{event_context_json}

# === BLOCK 0: EVENT RISK GATES ===

*   **IF "TRIPLE_WITCHING" or "MONTHLY_OPEX" is present (Today or Recent):**
    *   **DOWNGRADE** confidence on all Volume/OI interpretations.
    *   **BAN** phrases like "aggressive conviction" or "strong directional positioning" unless supported by multiple non-expiry signals.
    *   **REQUIRE** phrasing: "Expiry/roll effects may distort OI/volume."
    *   **CITE** the specific flag when qualifying the signal.
*   **IF "INDEX_REBALANCE" or "RUSSELL_REBALANCE" is present:**
    *   Treat equity index futures flows as potentially mechanical.
*   **IF "AUCTION_WEEK" or "REFUNDING" is present:**
    *   Treat rates OI/volume spikes as potentially auction/hedge-related.

# === BLOCK 1: DETERMINISTIC SIGNAL GATES (HARD INVARIANTS) ===

Signal Quality and Direction Allowed are precomputed deterministically in Python. 
YOU MUST adhere to these flags. Do not attempt to recalculate them.

*   **Equities Signal:** Provided in `cme_signals.equity.signal_label`
*   **Equities Direction Allowed:** Provided in `cme_signals.equity.direction_allowed`
*   **Rates Signal:** Provided in `cme_signals.rates.signal_label`
*   **Rates Direction Allowed:** Provided in `cme_signals.rates.direction_allowed`

**CRITICAL RULES:**
1.  **IF `direction_allowed` is False:** 
    *   The Direction for that asset MUST be "Unknown".
    *   Your narrative MUST remain neutral and non-directional. 
    *   BAN all directional terms: "Bullish", "Bearish", "Rally", "Selloff", "Conviction".
2.  **IF `signal_label` is "Low Signal / Noise":** 
    *   Direction MUST be "Unknown".
    *   You MUST explicitly state: "Signal is below noise threshold."
3.  **PARTICIPATION CHECK:**
    *   Use `cme_signals.equity.participation_label` and `cme_signals.rates.participation_label` (Expanding/Contracting) when describing market interest.
    *   Do NOT claim "broad participation" or "new money" if the label is "Contracting".

# === BLOCK 2: VISUAL EXTRACTION INSTRUCTIONS ===

Use the WisdomTree images primarily for growth, inflation, and yield curve context. Use the CME Bulletin primarily for positioning and sentiment context.

**OUTPUT INSTRUCTION:**
Proceed directly to the Final Output Structure. Start with "### 1. The Dashboard (Scoreboard)".
Do NOT include any verification data, raw metrics, or event flags in your output; these are handled by a separate deterministic system.

# === BLOCK 3: FINAL OUTPUT STRUCTURE ===

### 1. The Dashboard (Scoreboard) [SECTION:DASHBOARD]

Create a table with these 6 Dials. USE THE PRE-CALCULATED SCORES PROVIDED ABOVE.
*In the 'Justification' column, reference the visual evidence from the CME images (Volume/OI) to support the score.*

**Constraint:** You must ONLY cite numbers present in the `extracted_metrics` JSON. Do NOT "discover" or hallucinate numbers from the PDF text layer unless they are explicitly in the Ground Truth.

**Justification Rules (Metric Whitelist):**
*   **Growth Impulse:** Must cite Yield Curve (10y-2y) or Interest Coverage. DO NOT cite HY Spreads.
*   **Credit Stress:** Must cite High Yield (HY) Spreads.
*   **Valuation Risk:** Must cite Forward P/E Ratio.
*   **Liquidity Conditions:** Must cite CME Volume.
*   **Inflation Pressure:** Must cite 5y5y Breakeven or Real Yields.
*   **Risk Appetite:** Must cite VIX or CME Participation.

| Dial | Score (0-10) | Justification (Data Source: Daily Market Snapshot + CME) |
|---|---|---|
| Growth Impulse | [Score] | [Cite Yield Curve (10y-2y) or Interest Coverage ONLY.] |
| Inflation Pressure | [Score] | [Cite `inflation_expectations_5y5y` or Real Yields ONLY.] |
| Liquidity Conditions | [Score] | [Cite CME Volume depth ONLY.] |
| Credit Stress | [Score] | [Cite HY Spreads (OAS) ONLY.] |
| Valuation Risk | [Score] | [Cite Forward P/E Ratio ONLY.] |
| Risk Appetite | [Score] | [Cite VIX or CME Participation ONLY.] |

### 2. Executive Takeaway [SECTION:SUMMARY]
[Regime Name, The Driver, The Pivot]
*Constraint: Explicitly state if the CME positioning (OI changes) confirms the price action seen in the WisdomTree charts. Use Combined Totals ONLY for gauging general liquidity/participation. Do NOT use Combined Totals for directional conviction.*

### 3. The "Fiscal Dominance" Check (Monetary Stress) [SECTION:FISCAL]
[Data, Implication]

### 4. Rates & Curve Profile [SECTION:RATES]
[Shape, Implication]
**The Positioning Check (Source: CME Section 01 Images):**
* **Instruction:** Use the provided `cme_signals.rates.signal_label` and `gate_reason` directly. Do NOT attempt to recompute the signal from the images.
* **Output:** State the Signal Label and briefly cite the underlying Open Interest split (Futures vs Options) to justify the label.

### 5. The "Canary in the Coal Mine" (Credit Stress) [SECTION:CREDIT]
[Data, Implication]

### 6. The "Engine Room" (Market Breadth) [SECTION:EQUITIES]
[Data, Implication]
*Synthesize the CME Image data. Describe the Equity Index positioning based on the provided signal label.*

### 7. Valuation & Positioning [SECTION:VALUATION]
[Data, International, Implication]
*Constraint: Do NOT use terms like "Smart Money", "Whales", or "Insiders". Focus on structural positioning (hedging vs. direction).*

### 8. Conclusion & Trade Tilt [SECTION:CONCLUSION]
[Cross-Asset Confirmation, Risk Rating, The Trade, Triggers]
"""

def download_pdfs(sources):
    paths = {}
    for name, url in sources.items():
        print(f"Downloading {name} from {url}...")
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            filename = f"{name}.pdf"
            with open(filename, "wb") as f:
                f.write(response.content)
            paths[name] = filename
            print(f"Downloaded {filename}.")
        except Exception as e:
            print(f"Error downloading {name}: {e}")
    return paths

def fetch_live_data():
    print("Fetching live market data (fallback)...")
    data = {}
    try:
        # Fetch VIX
        vix = yf.Ticker("^VIX")
        hist_vix = vix.history(period="1d")
        if not hist_vix.empty:
            data['vix_index'] = round(hist_vix['Close'].iloc[-1], 2)
            print(f"Live VIX: {data['vix_index']}")

        # Fetch 10Y Yield (^TNX) for precise BPS change
        tnx = yf.Ticker("^TNX")
        hist_tnx = tnx.history(period="5d") # Fetch a few days to ensure we get prev close
        if len(hist_tnx) >= 2:
            # TNX is in percent (e.g. 4.50 for 4.50%)
            current_yield = hist_tnx['Close'].iloc[-1]
            prev_yield = hist_tnx['Close'].iloc[-2]
            change_bps = (current_yield - prev_yield) * 100
            
            data['ust10y_current'] = round(current_yield, 2)
            data['ust10y_change_bps'] = round(change_bps, 1)
            print(f"Live 10Y Yield: {data['ust10y_current']}% (Change: {data['ust10y_change_bps']} bps)")
        else:
            data['ust10y_change_bps'] = None

        # Fetch Macro Context (DXY, WTI, HYG)
        for ticker, key in [("DX-Y.NYB", "dxy"), ("CL=F", "wti"), ("HYG", "hyg")]:
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="5d")
                if len(hist) >= 2:
                    curr = hist['Close'].iloc[-1]
                    prev = hist['Close'].iloc[-2]
                    pct = ((curr - prev) / prev) * 100
                    data[f'{key}_current'] = round(curr, 2)
                    data[f'{key}_1d_chg'] = round(pct, 2)
            except Exception as e:
                print(f"Failed to fetch {ticker}: {e}")

        # Fetch S&P 500 for Trend/Freshness (using ^GSPC Index)
        # Fetch 2mo to safely handle holidays and strict 21-day lookback
        spx = yf.Ticker("^GSPC")
        hist_spx = spx.history(period="2mo")
        
        # Determine strict "Close-to-Close" indices
        if not hist_spx.empty:
            last_date = hist_spx.index[-1].date()
            today_date = datetime.now().date()
            
            # If the last row is today, it's a partial bar (live). Use yesterday's close for trend stability.
            if last_date == today_date:
                # Safety check: Ensure we actually have a previous row to fall back to
                if len(hist_spx) < 2:
                    print(f"Warning: Insufficient SPX data (only {len(hist_spx)} row) to skip partial bar.")
                    data['sp500_trend_status'] = "Unknown"
                    data['sp500_1mo_change_pct'] = None
                    data['sp500_trend_audit'] = "Insufficient data (single partial row)"
                    return data
                current_idx = -2
            else:
                current_idx = -1
            
            # Check staleness: If the "current" data point is older than 7 days (weekend + holidays buffer), flag it.
            # Raised to 7 days to avoid false-positives during long holiday stretches.
            current_data_date = hist_spx.index[current_idx].date()
            days_lag = (today_date - current_data_date).days
            
            if days_lag > 7:
                print(f"Warning: SPX data is stale. Last available: {current_data_date} (Lag: {days_lag} days)")
                data['sp500_trend_status'] = "Unknown"
                data['sp500_1mo_change_pct'] = None
                data['sp500_trend_audit'] = f"Data Stale (Lag: {days_lag} days)"
                return data

            # We want strictly 21 trading days ago
            # If current_idx is -1, we need -22. If -2, we need -23.
            prior_idx = current_idx - 21
            
            # abs(prior_idx) represents the count of rows needed from the end.
            # Pandas iloc[-N] requires len(df) >= N.
            required_len = abs(prior_idx)
            
            # Check if we have enough data
            if len(hist_spx) >= required_len:
                current_close = hist_spx['Close'].iloc[current_idx]
                prior_close = hist_spx['Close'].iloc[prior_idx]
                
                # Store dates for audit
                current_date_str = hist_spx.index[current_idx].strftime('%Y-%m-%d')
                prior_date_str = hist_spx.index[prior_idx].strftime('%Y-%m-%d')

                pct_change = ((current_close - prior_close) / prior_close) * 100
                
                trend_status = "Flat (Range-Bound)"
                if pct_change >= 2.0: trend_status = "Trending Up"
                elif pct_change <= -2.0: trend_status = "Trending Down"
                
                data['sp500_current'] = round(current_close, 2)
                data['sp500_current_date'] = current_date_str
                data['sp500_trend_status'] = trend_status
                data['sp500_1mo_change_pct'] = round(pct_change, 2)
                data['sp500_trend_audit'] = f"Change from {prior_date_str} ({prior_close:.2f}) to {current_date_str} ({current_close:.2f})"
                
                print(f"SPX Trend: {trend_status} ({pct_change:.2f}%) | {data['sp500_trend_audit']}")
            else:
                print(f"Warning: Insufficient SPX data. Rows: {len(hist_spx)}, Required: {required_len}")
                data['sp500_trend_status'] = "Unknown"
                data['sp500_1mo_change_pct'] = None
                data['sp500_trend_audit'] = "Insufficient data"
        else:
            data['sp500_trend_status'] = "Unknown"
            data['sp500_1mo_change_pct'] = None
            data['sp500_trend_audit'] = "No data fetched"

    except Exception as e:
        print(f"Error fetching live data: {e}")
        data['sp500_trend_status'] = "Unknown"
        data['sp500_trend_audit'] = f"Error: {str(e)}"
        
    return data

def pdf_to_images(pdf_path):
    print(f"Converting {pdf_path} to images for Vision...")
    doc = fitz.open(pdf_path)
    images = []
    # Production: Limit to first 25 pages (skipping glossary/legal)
    for page_num in range(min(len(doc), 25)): 
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3)) # 3x zoom for maximum clarity
        img_data = pix.tobytes("jpeg")
        base64_img = base64.b64encode(img_data).decode('utf-8')
        images.append(base64_img)
    print(f"Converted {len(images)} pages to images.")
    return images

# --- Deterministic Scoring Logic ---

def determine_signal(futures_delta, options_delta, noise_threshold=50000):
    # Default safe state
    res = {
        "signal_label": "Unknown",
        "direction_allowed": False,
        "noise_filtered": False,
        "gate_reason": "Missing Data",
        "participation_label": "Unknown",
        "futures_oi_delta": futures_delta,
        "options_oi_delta": options_delta,
        "noise_threshold": noise_threshold,
        "dominance_ratio": 0.0
    }

    if futures_delta is None or options_delta is None:
        return res
    
    fut_abs = abs(futures_delta)
    opt_abs = abs(options_delta)
    net_delta = futures_delta + options_delta
    
    # Calculate Dominance Ratio (Options / Futures)
    dom_ratio = opt_abs / max(fut_abs, 1)
    res["dominance_ratio"] = round(dom_ratio, 2)
    
    # Participation Logic
    res["participation_label"] = "Expanding" if net_delta > 0 else "Contracting"
    
    # 1. Noise Filter
    if max(fut_abs, opt_abs) < noise_threshold:
        res.update({
            "signal_label": "Low Signal / Noise",
            "direction_allowed": False,
            "noise_filtered": True,
            "gate_reason": f"Max delta ({max(fut_abs, opt_abs)}) < Threshold ({noise_threshold})"
        })
        return res
    
    # 2. The Gate (Dominance check)
    if opt_abs >= fut_abs:
        res.update({
            "signal_label": "Hedging-Vol",
            "direction_allowed": False,
            "noise_filtered": False,
            "gate_reason": f"Options {dom_ratio:.1f}x Futures [|{opt_abs}| >= |{fut_abs}|]"
        })
    else:
        res.update({
            "signal_label": "Directional",
            "direction_allowed": True,
            "noise_filtered": False,
            "gate_reason": f"Futures > Options ({1/dom_ratio:.1f}x) [|{fut_abs}| > |{opt_abs}|]"
        })
        
    return res

def generate_verification_block(effective_date, extracted_metrics, cme_signals, event_context):
    eq_sig = cme_signals.get('equity', {})
    rt_sig = cme_signals.get('rates', {})
    
    def fmt_val(v): return f"{v:,}" if isinstance(v, int) else str(v)
    
    def b(val, reason=""):
        c = 'badge-gray'
        v_lower = str(val).lower()
        if 'directional' in v_lower: c = 'badge-blue'
        elif 'hedging' in v_lower: c = 'badge-orange'
        elif 'allowed' in v_lower: c = 'badge-green'
        elif 'expanding' in v_lower: c = 'badge-green'
        elif 'contracting' in v_lower: c = 'badge-red'
        elif 'trending up' in v_lower or (isinstance(val, str) and val.startswith('+')): c = 'badge-green'
        elif 'trending down' in v_lower or (isinstance(val, str) and val.startswith('-')): c = 'badge-red'
        return f'<span class="badge {c}" title="{reason}">{val}</span>'

    # Helper to fmt deltas
    def d(val):
        if val is None: return "N/A"
        return f"{val:+}"

    bps_change = extracted_metrics.get('ust10y_change_bps')
    rates_text = f"Signal: {b(rt_sig.get('signal_label', 'Unknown'), rt_sig.get('gate_reason', ''))}"
    if bps_change is not None:
        rates_text += f" | 10Y Move: {bps_change:+.1f} bps (Live)"

    # Add Raw Deltas to Verification
    eq_deltas = f"[Fut: <span class=\"numeric\">{d(eq_sig.get('futures_oi_delta'))}</span> | Opt: <span class=\"numeric\">{d(eq_sig.get('options_oi_delta'))}</span>]"
    rt_deltas = f"[Fut: <span class=\"numeric\">{d(rt_sig.get('futures_oi_delta'))}</span> | Opt: <span class=\"numeric\">{d(rt_sig.get('options_oi_delta'))}</span>]"

    # Direction Strings
    eq_dir_str = "Allowed" if eq_sig.get('direction_allowed') else "Unknown"
    rt_dir_str = "Allowed" if rt_sig.get('direction_allowed') else "Unknown"

    block = f"""
<div class="algo-box" style="margin-bottom: 10px; padding: 10px;">
    <strong>Audit Summary:</strong> 
    {b(eq_sig.get('signal_label', 'Unknown'), eq_sig.get('gate_reason', ''))} Equities ({b(eq_dir_str)}) &nbsp;|&nbsp; 
    {b(rt_sig.get('signal_label', 'Unknown'), rt_sig.get('gate_reason', ''))} Rates ({b(rt_dir_str)})
</div>

<details>
<summary><strong>Full Data Verification</strong> (Click to Expand)</summary>

> **DATA VERIFICATION (DETERMINISTIC):**
> * **Event Flags:** Today: {event_context.get('flags_today', [])} | Recent: {event_context.get('flags_recent', [])}
> * **CME Provenance:** Bulletin Date: "{extracted_metrics.get('cme_bulletin_date', 'Unknown')}" | Total Volume: {fmt_val(extracted_metrics.get('cme_total_volume', 'N/A'))} | Total OI: {fmt_val(extracted_metrics.get('cme_total_open_interest', 'N/A'))}
> * **CME Audit Anchors:** Totals: "{extracted_metrics.get('cme_totals_audit_label', 'N/A')}" | Rates: "{extracted_metrics.get('cme_rates_futures_audit_label', 'N/A')}" | Equities: "{extracted_metrics.get('cme_equity_futures_audit_label', 'N/A')}"
> * **Date Check:** Report Date: {effective_date} | SPX Trend Source: yfinance
> * **SPX Trend Audit:** {extracted_metrics.get('sp500_trend_audit', 'N/A')}
> * **Equities:** Signal: {b(eq_sig.get('signal_label', 'Unknown'), eq_sig.get('gate_reason', ''))} {eq_deltas} | Part.: {b(eq_sig.get('participation_label', 'Unknown'))} | Trend: {extracted_metrics.get('sp500_trend_status', 'Unknown')} | Dir: {b(eq_dir_str)}
> * **Rates:** {rates_text} {rt_deltas} | Part.: {b(rt_sig.get('participation_label', 'Unknown'))} | Dir: {b(rt_dir_str)}
</details>
"""
    return block

def calculate_deterministic_scores(extracted_data):
    print("Calculating deterministic scores...")
    scores = {}
    details = {} # Track confidence/source
    data = extracted_data or {}
    
    # --- 1. LIQUIDITY CONDITIONS (Higher = Looser/Better) ---
    try:
        hy_spread = data.get('hy_spread_current')
        real_yield = data.get('real_yield_10y')
        
        if hy_spread is not None and real_yield is not None:
            median_spread = 4.5
            if hy_spread <= 0: hy_spread = 0.01 
            spread_component = 5.0 + (math.log(median_spread / hy_spread, 2) * 3.0)
            ry_penalty = max(0, (real_yield - 1.5) * 2.0)
            final_liq = spread_component - ry_penalty
            scores['Liquidity Conditions'] = round(min(max(final_liq, 0), 10), 1)
            details['Liquidity Conditions'] = "Calculated (Spread + Real Yield)"
        else:
            scores['Liquidity Conditions'] = 5.0
            details['Liquidity Conditions'] = "Default (Missing Data)"
    except Exception as e:
        print(f"Error calc Liquidity: {e}")
        scores['Liquidity Conditions'] = 5.0
        details['Liquidity Conditions'] = "Error (Defaulted)"

    # --- 2. VALUATION RISK (Higher = Expensive/Riskier) ---
    try:
        pe_ratio = data.get('forward_pe_current')
        if pe_ratio is not None:
            val_score = 5.0 + ((pe_ratio - 18.0) * 0.66)
            scores['Valuation Risk'] = round(min(max(val_score, 0), 10), 1)
            details['Valuation Risk'] = f"Calculated (P/E {pe_ratio})"
        else:
            scores['Valuation Risk'] = 5.0
            details['Valuation Risk'] = "Default (Missing P/E)"
    except Exception as e:
        print(f"Error calc Valuation: {e}")
        scores['Valuation Risk'] = 5.0
        details['Valuation Risk'] = "Error (Defaulted)"

    # --- 3. INFLATION PRESSURE (Higher = High Inflation) ---
    try:
        inf_exp = data.get('inflation_expectations_5y5y')
        if inf_exp is not None:
            inf_score = 5.0 + ((inf_exp - 2.25) * 10.0)
            scores['Inflation Pressure'] = round(min(max(inf_score, 0), 10), 1)
            details['Inflation Pressure'] = f"Calculated (5y5y {inf_exp}%)"
        else:
            scores['Inflation Pressure'] = 5.0
            details['Inflation Pressure'] = "Default (Missing 5y5y)"
    except Exception as e:
        print(f"Error calc Inflation: {e}")
        scores['Inflation Pressure'] = 5.0
        details['Inflation Pressure'] = "Error (Defaulted)"

    # --- 4. CREDIT STRESS (Higher = Panic) ---
    try:
        hy_spread = data.get('hy_spread_current')
        if hy_spread is not None:
            if hy_spread < 3.0:
                stress_score = 2.0
            else:
                stress_score = 2.0 + ((hy_spread - 3.0) * 1.6)
            scores['Credit Stress'] = round(min(max(stress_score, 0), 10), 1)
            details['Credit Stress'] = f"Calculated (Spread {hy_spread}%)"
        else:
            scores['Credit Stress'] = 5.0
            details['Credit Stress'] = "Default (Missing Spread)"
    except Exception as e:
        print(f"Error calc Credit: {e}")
        scores['Credit Stress'] = 5.0
        details['Credit Stress'] = "Error (Defaulted)"

    # --- 5. GROWTH IMPULSE (Higher = Boom) ---
    try:
        y10 = data.get('yield_10y')
        y2 = data.get('yield_2y')
        if y10 is not None and y2 is not None:
            curve_slope = y10 - y2
            growth_score = 5.0 + ((curve_slope - 0.50) * 3.5)
            scores['Growth Impulse'] = round(min(max(growth_score, 0), 10), 1)
            details['Growth Impulse'] = f"Calculated (Curve {curve_slope:.2f}%)"
        else:
            scores['Growth Impulse'] = 5.0
            details['Growth Impulse'] = "Default (Missing Yields)"
    except Exception as e:
        print(f"Error calc Growth: {e}")
        scores['Growth Impulse'] = 5.0
        details['Growth Impulse'] = "Error (Defaulted)"

    # --- 6. RISK APPETITE (Higher = Greed) ---
    try:
        vix = data.get('vix_index')
        if vix is not None:
            risk_score = 10.0 - ((vix - 10.0) * 0.5)
            scores['Risk Appetite'] = round(min(max(risk_score, 0), 10), 1)
            details['Risk Appetite'] = f"Calculated (VIX {vix})"
        else:
            scores['Risk Appetite'] = 7.0 
            details['Risk Appetite'] = "Default (Missing VIX)"
    except:
        scores['Risk Appetite'] = 7.0
        details['Risk Appetite'] = "Error (Defaulted)"
    
    print(f"Calculated Scores: {scores}")
    return scores, details

def process_cme_sec09(raw_data):
    """
    Process raw CME Section 09 extraction into a deterministic rates curve object.
    """
    if not raw_data or "cme_section09" not in raw_data:
        return {}

    sec09 = raw_data["cme_section09"]
    totals = sec09.get("totals", {})
    notes = sec09.get("data_quality_notes", [])
    
    # 1. Normalize & Cast
    processed_tenors = {}
    missing_tenors = []
    
    # Tenor mapping for clean iteration
    tenor_keys = ["2y", "3y", "5y", "10y", "tn", "30y", "ultra"]
    
    for k in tenor_keys:
        if k not in totals:
            missing_tenors.append(k)
            continue
            
        row = totals[k]
        # Parse fields
        rth = parse_int_token(row.get("rth_volume")) or 0
        globex = parse_int_token(row.get("globex_volume")) or 0
        oi = parse_int_token(row.get("open_interest"))
        change = parse_int_token(row.get("oi_change")) or 0
        
        processed_tenors[k] = {
            "total_volume": rth + globex,
            "open_interest": oi,
            "oi_change": change
        }

    # 2. Clusters
    clusters = {
        "Short End": ["2y", "3y"],
        "Belly": ["5y"],
        "Tens": ["10y", "tn"],
        "Long End": ["30y", "ultra"]
    }
    
    cluster_stats = {}
    for name, tenors in clusters.items():
        abs_sum = 0
        signed_sum = 0
        for t in tenors:
            if t in processed_tenors:
                chg = processed_tenors[t]["oi_change"]
                abs_sum += abs(chg)
                signed_sum += chg
        cluster_stats[name] = {"abs_oi_change": abs_sum, "net_oi_change": signed_sum}

    # 3. Dominance & Regime
    active_cluster = max(cluster_stats, key=lambda k: cluster_stats[k]["abs_oi_change"]) if cluster_stats else "N/A"
    active_tenor = max(processed_tenors, key=lambda k: abs(processed_tenors[k]["oi_change"])) if processed_tenors else "N/A"
    
    # Optional Regime Label (Section 3C)
    short_abs = cluster_stats.get("Short End", {}).get("abs_oi_change", 0)
    long_abs = cluster_stats.get("Long End", {}).get("abs_oi_change", 0)
    regime = "Mixed"
    if long_abs > short_abs and long_abs > 0: regime = "Long-end dominant"
    elif short_abs > long_abs and short_abs > 0: regime = "Front-end dominant"

    # Concentration
    total_abs_delta = sum(abs(t["oi_change"]) for t in processed_tenors.values())
    top2_abs = sum(sorted([abs(t["oi_change"]) for t in processed_tenors.values()], reverse=True)[:2])
    concentration = (top2_abs / total_abs_delta) if total_abs_delta > 0 else 0.0

    # 4. Quality Guards
    is_complete = len(processed_tenors) >= 5
    if not is_complete:
        notes.append("partial_section09_parse")

    return {
        "tenors": processed_tenors,
        "clusters": cluster_stats,
        "dominance": {
            "active_cluster": active_cluster,
            "active_tenor": active_tenor,
            "concentration": concentration,
            "regime_label": regime
        },
        "quality": {
            "missing_tenors": missing_tenors,
            "is_complete": is_complete,
            "notes": notes,
            "is_preliminary": sec09.get("is_preliminary", False)
        }
    }

def extract_metrics_gemini(pdf_paths, prompt_override=None):
    print("Extracting Ground Truth Data with Gemini...")
    if not AI_STUDIO_API_KEY: 
        print("Error: AI_STUDIO_API_KEY not found. Skipping PDF extraction.")
        return {}

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    try:
        content = [prompt_override if prompt_override else EXTRACTION_PROMPT]
        # Upload all PDFs
        for name, path in pdf_paths.items():
            print(f"Uploading {name} ({path})...")
            f = genai.upload_file(path, mime_type="application/pdf")
            content.append(f"Document: {name}")
            content.append(f)
            
        response = model.generate_content(content)
        
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        print(f"Extracted Data: {data}")
        return data
    except Exception as e:
        print(f"Extraction failed (CME/WisdomTree Source): {e}")
        return {}

# --- Summarization ---

def summarize_openrouter(pdf_paths, ground_truth, event_context, model_override=None):
    target_model = model_override if model_override else OPENROUTER_MODEL
    print(f"Summarizing with OpenRouter ({target_model})...")
    if not OPENROUTER_API_KEY: return "Error: Key missing"
    
    # Process images for ALL PDFs (Skip if BENCHMARK_JSON)
    images = []
    if RUN_MODE != "BENCHMARK_JSON":
        # Logic: Convert WisdomTree first
        if "wisdomtree" in pdf_paths:
            images.extend(pdf_to_images(pdf_paths["wisdomtree"]))
        
        # Then CME (limit pages to first 1 since it's a summary sheet)
        if "cme_vol" in pdf_paths:
            cme_images = pdf_to_images(pdf_paths["cme_vol"])
            images.extend(cme_images[:1]) # Just the first page

        # And CME Rates Curve (Section 09) - First page usually has the summary table
        if "cme_sec09" in pdf_paths:
            sec09_images = pdf_to_images(pdf_paths["cme_sec09"])
            images.extend(sec09_images[:1])
    
    if RUN_MODE == "BENCHMARK":
        formatted_prompt = BENCHMARK_SYSTEM_PROMPT
    elif RUN_MODE == "BENCHMARK_JSON":
        formatted_prompt = BENCHMARK_DATA_SYSTEM_PROMPT + f"\n\nGround Truth Data:\n{json.dumps(ground_truth, indent=2)}\n\nEvent Context:\n{json.dumps(event_context, indent=2)}"
    else:
        formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(
            ground_truth_json=json.dumps(ground_truth, indent=2),
            event_context_json=json.dumps(event_context, indent=2)
        )
    
    content_list = [{"type": "text", "text": formatted_prompt}]
    for img_b64 in images:
        content_list.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/jpeirce/daily-macro-summary",
        "X-Title": "Daily Macro Summary",
        "Content-Type": "application/json"
    }
    body = {
        "model": target_model,
        "messages": [{"role": "user", "content": content_list}]
    }
    
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=300)
        if response.status_code != 200:
            return f"Error {response.status_code}: {response.text}"
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"OpenRouter Error: {e}"

def summarize_gemini(pdf_paths, ground_truth, event_context):
    print(f"Summarizing with Gemini ({GEMINI_MODEL})...")
    if not AI_STUDIO_API_KEY: return "Error: Key missing"

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    if RUN_MODE == "BENCHMARK":
        formatted_prompt = BENCHMARK_SYSTEM_PROMPT
    elif RUN_MODE == "BENCHMARK_JSON":
        formatted_prompt = BENCHMARK_DATA_SYSTEM_PROMPT + f"\n\nGround Truth Data:\n{json.dumps(ground_truth, indent=2)}\n\nEvent Context:\n{json.dumps(event_context, indent=2)}"
    else:
        formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(
            ground_truth_json=json.dumps(ground_truth, indent=2),
            event_context_json=json.dumps(event_context, indent=2)
        )
    
    content = [formatted_prompt]
    
    # Only upload PDFs if NOT in BENCHMARK_JSON mode
    if RUN_MODE != "BENCHMARK_JSON":
        try:
            for name, path in pdf_paths.items():
                f = genai.upload_file(path, mime_type="application/pdf")
                content.append(f"Document: {name}")
                content.append(f)
        except Exception as e:
            return f"Gemini Upload Error: {e}"
            
    try:
        response = model.generate_content(content)
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def clean_llm_output(text, cme_signals=None):
    text = text.strip()
    if text.startswith("```markdown"): text = text[11:]
    elif text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    
    # Post-generation validator for banned terms
    
    # Pass 1: Adjectives (e.g. "institutional flows" -> "market-participant flows")
    adj_pattern = re.compile(r"\b(institutional)\b", re.IGNORECASE)
    if adj_pattern.search(text):
        print("Warning: Banned adjective found. Normalizing...")
        text = adj_pattern.sub("market-participant", text)
        if "Language normalization applied" not in text:
            text += "\n\n*(Note: Language normalization applied to remove attribution)*"

    # Pass 2: Nouns (e.g. "whales sold" -> "market participants sold")
    noun_pattern = re.compile(r"\b(smart money|whales?|insiders?|institutions?|big players?|professionals?|strong hands?|hedge funds?|asset managers?|dealers?|banks?|allocators?|funds?|big money|real money|pensions?|pension funds?|sovereign|sovereign wealth|macro funds?|levered funds?|CTAs)\b", re.IGNORECASE)
    if noun_pattern.search(text):
        print("Warning: Banned noun found. Normalizing...")
        text = noun_pattern.sub("market participants", text)
        if "Language normalization applied" not in text:
            text += "\n\n*(Note: Language normalization applied to remove attribution)*"
    
    # Normalize Signal Vocabulary (e.g. Hedging/Vol -> Hedging-Vol)
    text = re.sub(r"\bHedging/Vol\b", "Hedging-Vol", text, flags=re.IGNORECASE)

    # Pass 3: Targeted Directional Leakage Validator
    if cme_signals:
        eq_sig_val = cme_signals.get('equity', {}).get('signal_label', 'Unknown')
        rt_sig_val = cme_signals.get('rates', {}).get('signal_label', 'Unknown')
        
        # 3a. Force-Overwrite "Signal:" lines with Deterministic Truth
        lines = text.split('\n')
        new_lines = []
        current_section = "Unknown"
        
        for line in lines:
            # Detect Section using deterministic sentinels
            if "[SECTION:RATES]" in line:
                current_section = "Rates"
            elif "[SECTION:EQUITIES]" in line:
                current_section = "Equities"
            elif "[SECTION:SUMMARY]" in line:
                current_section = "Summary"
            
            # Detect Signal/Direction Lines
            if "Signal:" in line:
                if current_section == "Rates":
                    prefix = line.split("Signal:")[0]
                    line = f"{prefix}Signal: {rt_sig_val}"
                elif current_section == "Equities":
                    prefix = line.split("Signal:")[0]
                    line = f"{prefix}Signal: {eq_sig_val}"
            elif "Direction:" in line:
                # Enforcement/Normalization
                eq_allowed = cme_signals.get('equity', {}).get('direction_allowed', True)
                rt_allowed = cme_signals.get('rates', {}).get('direction_allowed', True)
                
                if current_section == "Rates" and not rt_allowed:
                    prefix = line.split("Direction:")[0]
                    line = f"{prefix}Direction: Unknown"
                elif current_section == "Equities" and not eq_allowed:
                    prefix = line.split("Direction:")[0]
                    line = f"{prefix}Direction: Unknown"
            
            new_lines.append(line)
        
        text = "\n".join(new_lines)

        eq_allowed = cme_signals.get('equity', {}).get('direction_allowed', True)
        rt_allowed = cme_signals.get('rates', {}).get('direction_allowed', True)
        
        # Expanded Directional Vocabulary (including euphemisms)
        leakage_pattern = re.compile(r"\b(bullish|bearish|conviction|aggressive|rally|selloff|breakout|risk[- ]on|risk[- ]off|bull steepener|bear steepener|short covering|long liquidation|new longs|new shorts|breakdown|melt[- ]up|buying the dip|selling the rip|upside bias|downside bias|tilted? bullish|tilted? bearish|skewed? bullish|skewed? bearish|upside skew|downside skew|risk[- ]on skew|risk[- ]off skew|bull bias|bear bias)\b", re.IGNORECASE)
        
        # Split text into sections by headers (robust against ##, ###, ####)
        sections = re.split(r"(?m)(?=^#{2,4}\s)", text)
        processed_sections = []
        filter_applied = False
        
        for section in sections:
            is_rates = "[SECTION:RATES]" in section
            is_equities = "[SECTION:EQUITIES]" in section
            
            should_scrub = False
            if is_rates and not rt_allowed: should_scrub = True
            if is_equities and not eq_allowed: should_scrub = True
            
            if should_scrub and leakage_pattern.search(section):
                # Aggressive Redaction
                section = leakage_pattern.sub("[neutral phrasing enforced]", section)
                filter_applied = True
            
            processed_sections.append(section)
            
        text = "".join(processed_sections)
        
        # Grammatical cleanup after actor sanitization
        text = text.replace("participants flows", "participant flows")
        
        if filter_applied and "Note: Automatic direction filter applied" not in text:
            text += "\n\n*(Note: Automatic direction filter applied to non-directional signal sections)*"

    # Pass 4: Scoreboard Justification Validator
    lines = text.split('\n')
    in_scoreboard = False
    new_lines_pass4 = []
    
    # Constraints Mapping (Dial -> Forbidden Keywords)
    sb_constraints = {
        "Growth Impulse": ["spread", "credit", "hyg", "junk", "default"],
        "Liquidity Conditions": ["spread", "hyg", "junk", "credit", "default"], 
        "Credit Stress": ["p/e", "valuation", "earnings", "curve", "slope", "10y", "2y", "yield"],
        "Valuation Risk": ["spread", "credit", "vix", "curve", "yield", "slope"],
        "Inflation Pressure": ["vix", "participation", "volume", "p/e", "valuation"],
        "Risk Appetite": ["p/e", "valuation", "earnings", "curve", "slope"]
    }

    for line in lines:
        if "### 1. The Dashboard" in line:
            in_scoreboard = True
        elif line.startswith("### ") and "1. The Dashboard" not in line:
            in_scoreboard = False
        
        if in_scoreboard and line.strip().startswith("|") and "Score" not in line and "---" not in line:
            # Table row processing
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 4: # | Dial | Score | Justif |
                dial_name = parts[1]
                justification = parts[3].lower()
                
                # Check for constraints
                forbidden_found = False
                for dial_key, forbidden_list in sb_constraints.items():
                    if dial_key in dial_name: # Partial match "Growth Impulse"
                        for word in forbidden_list:
                            # Use regex word boundary to avoid partial matches (e.g. "expensive" containing "p/e"?) No, simple string match is risky.
                            # "yield" matches "yields". "spread" matches "spreads".
                            # But "spread" matches "widespread" (unlikely in this context but possible).
                            # Let's use regex for safety.
                            if re.search(r'\b' + re.escape(word) + r'\w*', justification):
                                forbidden_found = True
                                break
                    if forbidden_found: break
                
                if forbidden_found:
                    parts[3] = " (Needs revision: out-of-scope metric cited)"
                    line = "|".join(parts)
        
        new_lines_pass4.append(line)
    
    text = "\n".join(new_lines_pass4)

    # Inject TOC Anchors
    text = re.sub(r"(?i)(### 1\. The Dashboard.*SECTION:DASHBOARD\])", r'<a id="scoreboard"></a>\n\1', text)
    text = re.sub(r"(?i)(### 2\. Executive Takeaway.*SECTION:SUMMARY\])", r'<a id="takeaway"></a>\n\1', text)
    text = re.sub(r"(?i)(### 3\. The .*Fiscal.*SECTION:FISCAL\])", r'<a id="fiscal"></a>\n\1', text)
    text = re.sub(r"(?i)(### 4\. Rates.*SECTION:RATES\])", r'<a id="rates"></a>\n\1', text)
    text = re.sub(r"(?i)(### 5\. The .*Canary.*SECTION:CREDIT\])", r'<a id="credit"></a>\n\1', text)
    text = re.sub(r"(?i)(### 6\. The .*Engine.*SECTION:EQUITIES\])", r'<a id="engine"></a>\n\1', text)
    text = re.sub(r"(?i)(### 7\. Valuation.*SECTION:VALUATION\])", r'<a id="valuation"></a>\n\1', text)
    text = re.sub(r"(?i)(### 8\. Conclusion.*SECTION:CONCLUSION\])", r'<a id="conclusion"></a>\n\1', text)

    # Strip Sentinels from final output
    text = re.sub(r"\s*\[SECTION:[A-Z]+\]", "", text)

    # Markdown Hardening: Remove unbalanced bold markers
    if text.count("**") % 2 != 0:
        text = text.replace("**", "")

    return text.strip()

def get_score_color(category, score):
    high_risk_categories = ["Inflation Pressure", "Credit Stress", "Valuation Risk"]
    high_good_categories = ["Growth Impulse", "Liquidity Conditions", "Risk Appetite"]
    
    if category in high_risk_categories:
        if score >= 7: return "#e74c3c" # Red (High Risk)
        if score <= 4: return "#27ae60" # Green (Safe)
        
    if category in high_good_categories:
        if score >= 7: return "#27ae60" # Green (Strong)
        if score <= 4: return "#e74c3c" # Red (Weak)
        
    return "#2c3e50" 

# --- HTML Rendering Helpers ---

def render_chip(label, val, tooltip=""):
    c = 'badge-gray'
    v_lower = str(val).lower()
    if 'directional' in v_lower: c = 'badge-blue'
    elif 'hedging' in v_lower: c = 'badge-orange'
    elif 'allowed' in v_lower: c = 'badge-green'
    elif 'expanding' in v_lower: c = 'badge-green'
    elif 'contracting' in v_lower: c = 'badge-red'
    elif 'trending up' in v_lower or (isinstance(val, str) and val.startswith('+')): c = 'badge-green'
    elif 'trending down' in v_lower or (isinstance(val, str) and val.startswith('-')): c = 'badge-red'
    return f'<span class="badge {c}" title="{tooltip}" style="font-size:0.85em; padding:2px 6px;">{val}</span>'

def fmt_num(val):
    if val is None: return "N/A"
    try: return f"{val:,}" if isinstance(val, int) else f"{val:.2f}"
    except: return str(val)

def fmt_delta(val):
    if val is None: return "N/A"
    try: return f"{int(val):+}"
    except: return str(val)

def get_curve_color(net_chg):
    if net_chg > 0: return "color: #27ae60;"
    if net_chg < 0: return "color: #e74c3c;"
    return "color: #7f8c8d;"

def render_provenance_strip(extracted_metrics, cme_signals):
    if not extracted_metrics: return ""
    return f"""
    <div class="provenance-strip">
        <div class="provenance-item">
            <span class="provenance-label">Equities:</span>
            {render_chip('Signal', cme_signals.get('equity', {}).get('signal_label', 'Unknown'), cme_signals.get('equity', {}).get('gate_reason', ''))}
            {render_chip('Part', cme_signals.get('equity', {}).get('participation_label', 'Unknown'), "Are participants adding (Expanding) or removing (Contracting) money?")}
            {render_chip('Dir', "Allowed" if cme_signals.get('equity', {}).get('direction_allowed') else "Unknown", "Directional Conviction: Is the system allowed to interpret price direction?")}
            {render_chip('Trend', extracted_metrics.get('sp500_trend_status', 'Unknown'), "1-month price action (Source: yfinance)")}
        </div>
        <div class="provenance-item" style="border-left: 1px solid #e1e4e8; padding-left: 15px;">
            <span class="provenance-label">Rates:</span>
            {render_chip('Signal', cme_signals.get('rates', {}).get('signal_label', 'Unknown'), cme_signals.get('rates', {}).get('gate_reason', ''))}
            {render_chip('Part', cme_signals.get('rates', {}).get('participation_label', 'Unknown'), "Are participants adding (Expanding) or removing (Contracting) money?")}
            {render_chip('Dir', "Allowed" if cme_signals.get('rates', {}).get('direction_allowed') else "Unknown", "Directional Conviction: Is the system allowed to interpret price direction?")}
            {render_chip('Move', f"{extracted_metrics.get('ust10y_change_bps', 0):+.1f} bps", "Basis point change in the 10-Year Treasury yield today")}
        </div>
    </div>
    """

def render_key_numbers(extracted_metrics):
    kn = extracted_metrics or {}
    key_numbers_items = [
        ("S&P 500", fmt_num(kn.get('sp500_current')), "Broad US Equity Market Index"),
        ("Forward P/E", f"{fmt_num(kn.get('forward_pe_current'))}x", "Valuation: Price / Expected Earnings (next 12m)"),
        ("HY Spread", f"{fmt_num(kn.get('hy_spread_current'))}%", "Credit Risk: Yield difference between Junk Bonds and Treasuries"),
        ("10Y Nominal", f"{fmt_num(kn.get('yield_10y'))}%", "US Treasury 10-Year Yield (Risk-free rate proxy)"),
        ("DXY", f"{fmt_num(kn.get('dxy_current'))}", "US Dollar Index (Strength vs Basket)"),
        ("WTI Crude", f"${fmt_num(kn.get('wti_current'))}", "Oil Price (Energy Cost Proxy)"),
        ("HYG", f"${fmt_num(kn.get('hyg_current'))}", "High Yield Bond ETF (Liquidity Proxy)"),
        ("VIX", f"{fmt_num(kn.get('vix_index'))}", "Market Volatility Index (Fear Gauge)"),
        ("CME Vol", f"{fmt_num(kn.get('cme_total_volume'))}", "Total Volume across CME Exchange")
    ]
    
    html = "<div class='key-numbers'>"
    for label, val, tooltip in key_numbers_items:
        html += f"<div class='key-number-item' title='{tooltip}' style='cursor: help;'><span class='key-number-label'>{label}</span><span class='key-number-value numeric'>{val}</span></div>"
    html += "</div>"
    return html

def render_rates_curve_panel(rates_curve):
    if not rates_curve or not rates_curve.get("clusters"): return ""
    
    clusters = rates_curve["clusters"]
    dom = rates_curve.get("dominance", {})
    
    # Cluster definitions
    cluster_defs = {
        "Short End": "2-Year & 3-Year Notes (Fed Policy Proxy)",
        "Belly": "5-Year Note (Transition Zone)",
        "Tens": "10-Year & Ultra 10-Year (Benchmark Duration)",
        "Long End": "30-Year & Ultra Bond (Inflation/Growth Proxy)"
    }

    rows = ""
    for name in ["Short End", "Belly", "Tens", "Long End"]:
        data = clusters.get(name, {})
        net = data.get("net_oi_change", 0)
        rows += f"""
        <div class="curve-item" title="{cluster_defs.get(name, '')}">
            <span class="curve-label" style="border-bottom: 1px dotted #ccc; cursor: help;">{name}</span>
            <span class="curve-value" style="{get_curve_color(net)}">{fmt_delta(net)}</span>
        </div>
        """
    
    # Tenor Detail Table
    tenors_data = rates_curve.get("tenors", {})
    active_cluster_name = dom.get('active_cluster', '')
    cluster_map = {
        "Short End": ["2y", "3y"],
        "Belly": ["5y"],
        "Tens": ["10y", "tn"],
        "Long End": ["30y", "ultra"]
    }
    active_tenors = cluster_map.get(active_cluster_name, [])

    tenor_rows = ""
    for tenor in ["2y", "3y", "5y", "10y", "tn", "30y", "ultra"]:
        t_data = tenors_data.get(tenor, {})
        is_active = tenor in active_tenors
        row_class = "active-tenor-row" if is_active else ""
        
        tenor_rows += f"""
        <tr class="{row_class}">
            <td style="text-align: left; padding: 4px 8px;">{tenor.upper()}</td>
            <td class="numeric" style="padding: 4px 8px;">{fmt_num(t_data.get('total_volume', 0))}</td>
            <td class="numeric" style="padding: 4px 8px; {get_curve_color(t_data.get('oi_change', 0))}">{fmt_delta(t_data.get('oi_change', 0))}</td>
        </tr>
        """

    return f"""
    <div class="rates-curve-panel">
        <div class="curve-header">
            <strong>Rates Curve Structure</strong>
            <span style="font-size: 0.85em; color: #666;" title="Regime: Which part of the yield curve has the highest absolute Open Interest change?">
                {render_chip('Regime', dom.get('regime_label', 'Mixed'), "Dominant Cluster based on total activity")}
                Active: {dom.get('active_cluster')} ({dom.get('active_tenor')})
            </span>
        </div>
        <div class="curve-grid">
            {rows}
        </div>
        <div style="margin-top: 15px; border-top: 1px solid #eee; padding-top: 10px;">
            <table style="font-size: 0.85em; width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="color: #7f8c8d; border-bottom: 1px solid #eee;">
                        <th style="text-align: left; padding: 4px 8px; font-weight: 600;">Tenor</th>
                        <th style="text-align: right; padding: 4px 8px; font-weight: 600;">Vol</th>
                        <th style="text-align: right; padding: 4px 8px; font-weight: 600;">OI Chg</th>
                    </tr>
                </thead>
                <tbody>
                    {tenor_rows}
                </tbody>
            </table>
        </div>
    </div>
    """

def render_event_callout(event_context, rates_curve=None):
    combined_notes = []
    callout_flags = []
    
    if event_context and event_context.get('flags_today'):
        callout_flags.extend(event_context['flags_today'])
        # Handle cases where notes might be missing for a flag
        notes_dict = event_context.get('notes', {})
        for f in event_context['flags_today']:
            if f in notes_dict:
                combined_notes.append(notes_dict[f])
        
    if rates_curve and rates_curve.get('quality', {}).get('notes'):
        q_notes = rates_curve['quality']['notes']
        clean_q_notes = [n for n in q_notes if not n.startswith("partial_")]
        if clean_q_notes:
            combined_notes.extend(clean_q_notes)
            if "DATA_QUALITY_ALERT" not in callout_flags:
                callout_flags.append("DATA_QUALITY_ALERT")

    if not combined_notes:
        return ""

    return f"""
    <div class="event-callout">
        <span style="font-size: 1.5em;">&#9888;&#65039;</span>
        <div>
            <strong>Event/Data Alert:</strong> {', '.join(callout_flags)}<br>
            <small style="color: #666; font-style: italic;">{' '.join(combined_notes)}</small>
        </div>
    </div>
    """

def render_signals_panel(cme_signals):
    def sig_panel_item(label, sig_data):
        quality = sig_data.get('signal_label', 'Unknown')
        deltas = f"Fut: {fmt_delta(sig_data.get('futures_oi_delta'))} | Opt: {fmt_delta(sig_data.get('options_oi_delta'))}"
        reason = sig_data.get('gate_reason', '')
        return f"""
        <div class="signal-chip" title="{reason}">
            <strong>{label}:</strong> {render_chip('Sig', quality)} <span style="color:#777; font-size:0.9em;">{deltas}</span>
        </div>
        """
    
    return f"""
    <div class="signals-panel">
        {sig_panel_item('Equities', cme_signals.get('equity', {}))}
        {sig_panel_item('Rates', cme_signals.get('rates', {}))}
    </div>
    """

def render_algo_box(scores, details, cme_signals):
    # Scoreboard
    score_html = "<div class='score-grid'>"
    for k, v in scores.items():
        color = get_score_color(k, v)
        detail_text = details.get(k, "Unknown")
        status_icon = f"<span title='{detail_text}' style='cursor: help; opacity: 0.5;'>&#9989;</span>"
        if "Default" in detail_text or "Error" in detail_text:
            status_icon = f"<span title='{detail_text}' style='cursor: help;'>&#9888;&#65039;</span>"

        score_html += f"""
        <div class='score-card' style='border-left: 5px solid {color};'>
            <div class='score-label'><span>{k}</span>{status_icon}</div>
            <div class='score-value' style='color: {color};'>{v}/10</div>
        </div>"""
    score_html += "</div>"

    # Signals
    sig_html = ""
    if cme_signals:
        sig_html = "<div class='score-grid' style='margin-top: 20px; border-top: 2px dashed #eee; padding-top: 20px;'>"
        for label, data in cme_signals.items():
            quality = data.get('signal_label', 'Unknown')
            reason = data.get('gate_reason', '')
            allowed = "Allowed" if data.get('direction_allowed') else "Redacted"
            color = "#27ae60" if data.get('direction_allowed') else "#7f8c8d"
            
            sig_html += f"""
            <div style='background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; border-left: 5px solid {color};' title='{reason}'>
                <span class='key-number-label'>{label.upper()} SIGNAL</span><br>
                <span class='key-number-value' style='color: {color};'>{quality}</span><br>
                <small style='font-size:0.7em; color:#999;'>{allowed}</small>
            </div>"""
        sig_html += "</div>"
        
    return f"""
    <div class="algo-box">
        <h3>&#129518; Technical Audit: Ground Truth Calculation</h3>
        {score_html}
        {sig_html}
        <small><em>These scores are calculated purely from extracted data points using fixed algorithms, serving as a benchmark for the AI models below.</em></small>
        <details style="margin-top: 15px; cursor: pointer;">
            <summary style="font-weight: bold; color: #3498db;">Show Calculation Formulas</summary>
            <div style="margin-top: 10px; font-size: 0.9em; background: #fff; padding: 10px; border: 1px solid #ddd; border-radius: 5px;">
                <ul style="list-style-type: disc; padding-left: 20px;">
                    <li><strong>Liquidity Conditions:</strong> 5.0 + (log2(4.5 / HY_Spread) * 3.0) - max(0, (Real_Yield_10Y - 1.5) * 2.0)</li>
                    <li><strong>Valuation Risk:</strong> 5.0 + ((Forward_PE - 18.0) * 0.66)</li>
                    <li><strong>Inflation Pressure:</strong> 5.0 + ((Inflation_Expectations_5y5y - 2.25) * 10.0)</li>
                    <li><strong>Credit Stress:</strong> 2.0 + ((HY_Spread - 3.0) * 1.6) [Min 2.0]</li>
                    <li><strong>Growth Impulse:</strong> 5.0 + ((Yield_10Y - Yield_2Y - 0.50) * 3.5)</li>
                    <li><strong>Risk Appetite:</strong> 10.0 - ((VIX - 10.0) * 0.5)</li>
                </ul>
                <p style="margin-top: 5px; font-style: italic;">All scores are clamped between 0.0 and 10.0.</p>
            </div>
        </details>
    </div>
    """

def generate_benchmark_html(today, summaries, ground_truth=None, event_context=None, filename="benchmark.html"):
    print(f"Generating Benchmark HTML report ({filename})...")
    
    # Extract Context
    extracted_metrics = ground_truth.get('extracted_metrics', {}) if ground_truth else {}
    cme_signals = ground_truth.get('cme_signals', {}) if ground_truth else {}
    rates_curve = ground_truth.get('cme_rates_curve', {}) if ground_truth else {}
    scores = ground_truth.get('calculated_scores', {}) if ground_truth else {}
    # We don't have score details in ground_truth dict usually (it's separate in main), 
    # but we can pass them or just default them.
    # Actually main() passes ground_truth_context which has: extracted_metrics, calculated_scores, cme_signals, cme_rates_curve.
    # It does NOT have score_details. I'll just use a dummy for now or update main to include it.
    score_details = {} 

    # Render Header Components
    header_html = ""
    header_html += render_provenance_strip(extracted_metrics, cme_signals)
    
    # PDF Links
    header_html += f"""
        <div style="text-align: center; margin-bottom: 15px; color: #7f8c8d; font-size: 0.9em; font-style: italic;">
            Independently generated summary. Informational use only—NOT financial advice. Full disclaimers in footer.
        </div>
        <div class="pdf-link">
            <h3>Inputs</h3>
            <a href="{PDF_SOURCES['wisdomtree']}" target="_blank">📄 View WisdomTree PDF</a>
            &nbsp;&nbsp;
            <a href="https://www.cmegroup.com/market-data/daily-bulletin.html" target="_blank" style="background-color: #2c3e50;">📊 View CME Bulletin</a>
        </div>
    """
    
    # Event Callout
    header_html += render_event_callout(event_context, rates_curve)

    header_html += render_key_numbers(extracted_metrics)
    
    # Rates & Algo Box (Inject AFTER the dropdown, or maybe above?)
    # Let's put everything above the dropdown for maximum context visibility.
    # Actually, Rates Panel and Algo Box are large. Maybe put them below the Key Numbers but above the dropdown?
    # Yes.
    
    # Render Rates Panel
    rates_html = render_rates_curve_panel(rates_curve)
    
    # Render Algo Box (Ground Truth)
    algo_html = render_algo_box(scores, score_details, cme_signals)

    options = ""
    divs = ""
    
    # Sort models: Gemini Native first, then others
    sorted_models = [GEMINI_MODEL] + [m for m in summaries.keys() if m != GEMINI_MODEL]
    
    for i, model in enumerate(sorted_models):
        content = summaries.get(model, "No content")
        html_content = markdown.markdown(content, extensions=['tables'])
        
        display_style = "block" if i == 0 else "none"
        is_selected = "selected" if i == 0 else ""
        
        options += f'<option value="{model}" {is_selected}>{model}</option>'
        divs += f'<div id="{model}" class="model-content" style="display: {display_style};">{html_content}</div>'

    # ... CSS ...
    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f6f8; }
    h1 { text-align: center; color: #2c3e50; }
    .controls { text-align: center; margin-bottom: 30px; background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); position: sticky; top: 10px; z-index: 900; border: 1px solid #ddd; }
    select { padding: 8px; font-size: 1em; border-radius: 4px; border: 1px solid #ccc; width: 300px; }
    .model-content { background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); animation: fadeIn 0.3s ease-in-out; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; }
    h3 { border-bottom: 2px solid #eee; padding-bottom: 8px; margin-top: 30px; }
    .pdf-link { display: block; text-align: center; margin-bottom: 20px; }
    .pdf-link a { display: inline-block; background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; margin: 0 5px; }
    /* Provenance Strip */
    .provenance-strip { display: flex; justify-content: center; gap: 30px; background: #fff; padding: 12px; border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; border: 1px solid #e1e4e8; font-size: 0.95em; color: #586069; }
    .provenance-item { display: flex; align-items: center; gap: 8px; }
    .provenance-label { font-weight: 700; color: #24292e; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.5px; }
    /* Key Numbers */
    .key-numbers { display: flex; flex-wrap: wrap; gap: 25px; justify-content: center; background: #fff; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; border: 1px solid #eee; }
    .key-number-item { display: flex; flex-direction: column; align-items: center; min-width: 100px; }
    .key-number-label { color: #7f8c8d; font-size: 0.75em; text-transform: uppercase; font-weight: bold; margin-bottom: 4px; }
    .key-number-value { font-weight: bold; color: #2c3e50; font-size: 1.1em; }
    .numeric { text-align: right; font-variant-numeric: tabular-nums; font-family: "SF Mono", "Segoe UI Mono", "Roboto Mono", monospace; }
    /* Rates Panel */
    .rates-curve-panel { background: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 15px; margin-bottom: 20px; }
    .curve-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-bottom: 10px; }
    .curve-grid { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
    .curve-item { flex: 1; min-width: 80px; text-align: center; }
    .curve-label { display: block; font-size: 0.75em; color: #7f8c8d; text-transform: uppercase; font-weight: bold; margin-bottom: 4px; }
    .curve-value { font-family: ui-monospace, monospace; font-weight: bold; font-size: 1.1em; }
    .active-tenor-row { background-color: #f1f8ff; font-weight: bold; border-left: 3px solid #3498db; }
    /* Algo Box */
    .algo-box { background: #e8f6f3; padding: 25px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #d1f2eb; }
    .score-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 20px; }
    .score-card { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; display: flex; flex-direction: column; justify-content: space-between; min-height: 110px; }
    .score-label { font-size: 0.85em; color: #2c3e50; display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 3.2em; line-height: 1.2; margin-bottom: 10px; font-weight: 600; }
    .score-value { font-size: 1.8em; font-weight: bold; }
    /* Event Callout */
    .event-callout { background: #f4f6f8; border: 1px solid #d1d5da; border-radius: 6px; padding: 12px 20px; margin-bottom: 30px; display: flex; align-items: center; gap: 12px; font-size: 0.9em; color: #444; }
    .event-callout strong { color: #24292e; }
    /* Badges */
    .badge { padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.9em; white-space: nowrap; display: inline-block; }
    .badge-blue { background: #ebf5fb; color: #2980b9; border: 1px solid #aed6f1; }
    .badge-orange { background: #fef5e7; color: #d35400; border: 1px solid #f9e79f; }
    .badge-gray { background: #f4f6f6; color: #7f8c8d; border: 1px solid #d5dbdb; }
    .badge-green { background: #e9f7ef; color: #27ae60; border: 1px solid #abebc6; }
    .badge-red { background: #fdedec; color: #c0392b; border: 1px solid #fadbd8; }
    .badge-warning { background: #fff3cd; color: #856404; border: 1px solid #ffeeba; }

    
    /* Native Dark Mode */
    @media (prefers-color-scheme: dark) {
        body { background: #0d1117; color: #c9d1d9; }
        .controls, .model-content { background: #161b22; border-color: #30363d; box-shadow: none; color: #c9d1d9; }
        h1, h2, h3, strong { color: #c9d1d9 !important; }
        select { background: #0d1117; color: #c9d1d9; border-color: #30363d; }
        th { background-color: #21262d; color: #c9d1d9; border-color: #30363d; }
        td { color: #c9d1d9; border-color: #30363d; }
        a { color: #58a6ff; }
        .key-numbers, .provenance-strip, .rates-curve-panel, .algo-box, .score-card { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
        .key-number-value, .score-value, .curve-value { color: #c9d1d9 !important; }
        .key-number-label, .score-label, .curve-label, .provenance-label { color: #8b949e !important; }
        .badge { filter: brightness(0.9); }
        .event-callout { background: #1c2128 !important; border-color: #444c56 !important; color: #c9d1d9 !important; }
        .active-tenor-row { background-color: rgba(56, 139, 253, 0.15) !important; border-left-color: #58a6ff !important; }
        .algo-box details div { background: #161b22 !important; color: #c9d1d9 !important; border-color: #30363d !important; }
    }
    """
    
    script = """
    function showModel(modelId) {
        // Hide all
        const contents = document.getElementsByClassName('model-content');
        for (let i = 0; i < contents.length; i++) {
            contents[i].style.display = 'none';
        }
        // Show selected
        document.getElementById(modelId).style.display = 'block';
    }
    """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Benchmark Arena: Daily Macro Summary - {today}</title>
        <style>{css}</style>
        <script>{script}</script>
    </head>
    <body>
        <h1>Benchmark Arena: Daily Macro Summary ({today})</h1>
        <p style="text-align:center; color:#666;">Mode: {'Data-Driven (JSON)' if 'JSON' in filename else 'Visual (PDFs)'}</p>
        
        {header_html}
        {rates_html}
        {algo_html}
        
        <div class="controls">
            <label for="model-select"><strong>Select Model:</strong></label>
            <select id="model-select" onchange="showModel(this.value)">
                {options}
            </select>
        </div>
        
        {divs}
        
        <div style="text-align: center; margin-top: 40px; color: #666; font-size: 0.9em;">
            Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | <a href="https://github.com/jpeirce/daily-macro-summary" style="color: #666;">View Source</a>
        </div>
    </body>
    </html>
    """
    
    # Save to specific filename
    os.makedirs("summaries", exist_ok=True)
    with open(f"summaries/{filename}", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML report generated and saved to summaries/{filename}")

def generate_html(today, summary_or, summary_gemini, scores, details, extracted_metrics, cme_signals=None, verification_block="", event_context=None, rates_curve=None):
    print("Generating HTML report...")
    
    # Prepend Verification Block to the raw text BEFORE markdown conversion
    if verification_block:
        summary_or = verification_block + "\n\n" + summary_or
        summary_gemini = verification_block + "\n\n" + summary_gemini

    summary_or = clean_llm_output(summary_or, cme_signals)
    summary_gemini = clean_llm_output(summary_gemini, cme_signals)
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    # Render Components using Helpers
    kn_html = render_key_numbers(extracted_metrics)
    signals_panel_html = render_signals_panel(cme_signals)
    rates_curve_html = render_rates_curve_panel(rates_curve)
    
    # Algo Box (Technical Audit) construction
    # Note: daily report adds "Technical Audit" heading manually in HTML template, 
    # but render_algo_box includes it. 
    # We need to adjust the HTML template below to avoid double heading.
    # Actually, render_algo_box returns the full <div class="algo-box">...</div>
    # In the original generate_html, the algo box code was generated into `score_html` and `sig_html`.
    # Let's use the helper.
    algo_box_html = render_algo_box(scores, details, cme_signals)

    # Build columns conditionally
    columns_html = ""
    if "Gemini summary skipped" not in summary_gemini:
        columns_html += f"""
            <div class="column">
                <h2>&#129302; Gemini ({GEMINI_MODEL})</h2>
                {signals_panel_html}
                {rates_curve_html}
                {html_gemini}
            </div>
        """
    
    if "OpenRouter summary skipped" not in summary_or:
        columns_html += f"""
            <div class="column">
                <h2>&#129504; OpenRouter ({OPENROUTER_MODEL})</h2>
                {signals_panel_html}
                {rates_curve_html}
                {html_or}
            </div>
        """

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f6f8; transition: background 0.3s, color 0.3s; }
    h1 { text-align: center; color: #2c3e50; margin-bottom: 20px; }
    .pdf-link { display: block; text-align: center; margin-bottom: 20px; }
    .pdf-link a { display: inline-block; background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; margin: 0 5px; }
    
    /* Provenance Strip */
    .provenance-strip { position: sticky; top: 0; z-index: 1000; display: flex; justify-content: center; gap: 30px; background: #fff; padding: 12px; border-radius: 0 0 6px 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 30px; border: 1px solid #e1e4e8; border-top: none; font-size: 0.95em; color: #586069; }
    .provenance-item { display: flex; align-items: center; gap: 8px; }
    .provenance-label { font-weight: 700; color: #24292e; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.5px; }
    
    /* Layout & TOC */
    .layout-wrapper { display: flex; gap: 20px; max-width: 1400px; margin: 0 auto; }
    .toc-sidebar { width: 200px; position: sticky; top: 80px; align-self: flex-start; background: #fff; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); font-size: 0.9em; max-height: 80vh; overflow-y: auto; }
    .toc-sidebar h3 { margin-top: 0; font-size: 1em; color: #7f8c8d; text-transform: uppercase; border-bottom: 1px solid #eee; padding-bottom: 8px; }
    .toc-sidebar a { display: block; padding: 6px 0; color: #34495e; text-decoration: none; border-bottom: 1px solid #f9f9f9; }
    .toc-sidebar a:hover { color: #3498db; padding-left: 4px; transition: padding 0.2s; }
    
    .container { flex: 1; display: flex; gap: 20px; flex-wrap: wrap; }
    .column { flex: 1; min-width: 350px; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); line-height: 1.75; }
    
    /* Numeric Formatting */
    .numeric { text-align: right; font-variant-numeric: tabular-nums; font-family: "SF Mono", "Segoe UI Mono", "Roboto Mono", monospace; }
    table td:nth-child(2) { text-align: right; font-variant-numeric: tabular-nums; } /* Auto-target Score column */
    
    /* Signals Panel */
    .signals-panel { background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 6px; padding: 10px; margin-bottom: 20px; display: flex; gap: 15px; flex-wrap: wrap; font-size: 0.85em; }
    .signal-chip { background: #fff; border: 1px solid #ddd; padding: 4px 8px; border-radius: 4px; display: flex; align-items: center; gap: 6px; }
    
    /* Rates Curve Panel */
    .rates-curve-panel { background: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 15px; margin-bottom: 20px; }
    .curve-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-bottom: 10px; }
    .curve-grid { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
    .curve-item { flex: 1; min-width: 80px; text-align: center; }
    .curve-label { display: block; font-size: 0.75em; color: #7f8c8d; text-transform: uppercase; font-weight: bold; margin-bottom: 4px; }
    .curve-value { font-family: ui-monospace, monospace; font-weight: bold; font-size: 1.1em; }
    
    /* Event Callout */
    .event-callout { background: #f4f6f8; border: 1px solid #d1d5da; border-radius: 6px; padding: 12px 20px; margin-bottom: 30px; display: flex; align-items: center; gap: 12px; font-size: 0.9em; color: #444; }
    .event-callout strong { color: #24292e; }
    
    /* Deterministic Separation */
    .deterministic-tint { background-color: #fbfcfd; border-left: 4px solid #d1d5da; padding: 10px 15px; margin: 10px 0; font-style: italic; color: #586069; }

    /* Heading Normalization within Columns */
    .column h1, .column h2 { font-size: 1.4em; border-bottom: 2px solid #eee; padding-bottom: 8px; margin-top: 0; color: #34495e; margin-bottom: 15px; }
    .column h3 { font-size: 1.15em; color: #2c3e50; margin-top: 20px; margin-bottom: 10px; font-weight: 700; }
    .column h4 { font-size: 1.05em; color: #555; margin-top: 15px; font-weight: 600; }
    
    strong { font-weight: 600; color: #2c3e50; } /* Soften bold density */
    
    .footer { text-align: center; margin-top: 40px; font-size: 0.9em; color: #666; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; }
    .algo-box { background: #e8f6f3; padding: 25px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #d1f2eb; }
    
    /* Grid Scoring */
    .score-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 20px; }
    .score-card { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; display: flex; flex-direction: column; justify-content: space-between; min-height: 110px; }
    .score-label { font-size: 0.85em; color: #2c3e50; display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 3.2em; line-height: 1.2; margin-bottom: 10px; font-weight: 600; }
    .score-value { font-size: 1.8em; font-weight: bold; }
    
    /* Key Numbers Strip */
    .key-numbers { display: flex; flex-wrap: wrap; gap: 25px; justify-content: center; background: #fff; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; border: 1px solid #eee; }
    .key-number-item { display: flex; flex-direction: column; align-items: center; min-width: 100px; }
    .key-number-label { color: #7f8c8d; font-size: 0.75em; text-transform: uppercase; font-weight: bold; margin-bottom: 4px; }
    .key-number-value { font-weight: bold; color: #2c3e50; font-size: 1.1em; }

    /* Signal Badges */
    .badge { padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.9em; white-space: nowrap; display: inline-block; }
    .badge-blue { background: #ebf5fb; color: #2980b9; border: 1px solid #aed6f1; }
    .badge-orange { background: #fef5e7; color: #d35400; border: 1px solid #f9e79f; }
    .badge-gray { background: #f4f6f6; color: #7f8c8d; border: 1px solid #d5dbdb; }
    .badge-green { background: #e9f7ef; color: #27ae60; border: 1px solid #abebc6; }
    .badge-red { background: #fdedec; color: #c0392b; border: 1px solid #fadbd8; }
    .badge-warning { background: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
    .active-tenor-row { background-color: #f1f8ff; font-weight: bold; border-left: 3px solid #3498db; }

    /* Native Dark Mode */
    @media (prefers-color-scheme: dark) {
        body { background: #0d1117; color: #c9d1d9; }
        .column, .algo-box, .score-grid > div, .footer, .key-numbers, .provenance-strip, .toc-sidebar, .signals-panel, .score-card, .rates-curve-panel { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
        .event-callout { background: #1c2128 !important; border-color: #444c56 !important; color: #c9d1d9 !important; }
        .event-callout strong { color: #58a6ff !important; }
        .score-label { color: #8b949e !important; }
        .signal-chip { background: #21262d !important; border-color: #30363d !important; color: #c9d1d9 !important; }
        .deterministic-tint { background-color: #1c2128 !important; border-left-color: #444c56 !important; color: #8b949e !important; }
        .active-tenor-row { background-color: rgba(56, 139, 253, 0.15) !important; border-left-color: #58a6ff !important; }
        .toc-sidebar a { color: #c9d1d9; border-bottom-color: #21262d; }
        .toc-sidebar a:hover { background: #21262d; }
        h1, h2, h3, strong { color: #c9d1d9 !important; }
        th { background-color: #21262d; color: #c9d1d9; border-color: #30363d; }
        td { color: #c9d1d9; border-color: #30363d; }
        a { color: #58a6ff; }
        .key-number-value { color: #c9d1d9 !important; }
        .key-number-label { color: #8b949e !important; }
        .provenance-label { color: #8b949e !important; }
        .provenance-strip { color: #c9d1d9 !important; }
        .badge { filter: brightness(0.9); }
        .algo-box details div { background: #161b22 !important; color: #c9d1d9 !important; border-color: #30363d !important; }
        .badge-warning { background: #3e3725; color: #ffca2c; border-color: #534824; }
    }
    """
    
    # We can add links to CME pdfs too if desired, but for now just Main
    main_pdf_url = PDF_SOURCES['wisdomtree']
    cme_bulletin_url = "https://www.cmegroup.com/market-data/daily-bulletin.html"
    
    # Extract provenance info
    cme_date_str = extracted_metrics.get('cme_bulletin_date', 'N/A')
    wt_date_str = extracted_metrics.get('wisdomtree_as_of_date', 'N/A')
    spx_audit = extracted_metrics.get('sp500_trend_audit', 'N/A')
    
    # Check for missing CME data
    cme_warning_flag = ""
    cme_keys_to_check = ['cme_total_volume', 'cme_total_open_interest', 'cme_rates_futures_oi_change', 'cme_equity_futures_oi_change']
    missing_cme = [k for k in cme_keys_to_check if extracted_metrics.get(k) is None]
    if missing_cme:
        cme_warning_flag = f' <span class="badge badge-warning" title="Missing fields: {", ".join(missing_cme)}">&#9888;&#65039; DATA INCOMPLETE</span>'

    # CME Staleness Check
    cme_staleness_flag = ""
    display_cme_date = cme_date_str
    try:
        if cme_date_str != 'N/A':
            # CME date usually comes as "YYYY-MM-DD" from extraction
            cme_dt = datetime.strptime(cme_date_str, "%Y-%m-%d").date()
            eff_dt = datetime.strptime(today, "%Y-%m-%d").date()
            
            # Reformat for display consistency
            display_cme_date = cme_dt.strftime("%Y-%m-%d")
            
            days_diff = (eff_dt - cme_dt).days
            if days_diff > 3:
                cme_staleness_flag = f' <span class="badge badge-red" title="Data is lagging today by {days_diff} days" style="font-size:0.8em; padding:1px 4px;">STALE ({days_diff}d lag)</span>'
                cme_warning_flag = cme_warning_flag # Ensure warning persists if both issues exist
            else:
                cme_staleness_flag = ' <span class="badge badge-green" title="Data is current (within 3-day buffer)" style="font-size:0.8em; padding:1px 4px;">FRESH</span>'
    except:
        pass

    # WisdomTree Staleness Check
    wt_staleness_flag = ""
    display_wt_date = wt_date_str
    try:
        if wt_date_str != 'N/A':
            # Parse WT format: "December 19, 2025" or "Dec 19, 2025"
            clean_wt = wt_date_str.strip()
            try:
                wt_dt = datetime.strptime(clean_wt, "%B %d, %Y").date()
            except:
                wt_dt = datetime.strptime(clean_wt, "%b %d, %Y").date()
            
            # Reformat for display consistency
            display_wt_date = wt_dt.strftime("%Y-%m-%d")
                
            eff_dt = datetime.strptime(today, "%Y-%m-%d").date()
            days_diff = (eff_dt - wt_dt).days
            
            if days_diff > 3:
                wt_staleness_flag = f' <span class="badge badge-red" title="Dashboard date lags today by {days_diff} days" style="font-size:0.8em; padding:1px 4px;">STALE ({days_diff}d lag)</span>'
            else:
                wt_staleness_flag = ' <span class="badge badge-green" title="Dashboard date is current (within 3-day buffer)" style="font-size:0.8em; padding:1px 4px;">FRESH</span>'
    except:
        pass

    # Construct Event Callout
    event_callout_html = ""
    combined_notes = []
    callout_flags = []
    
    if event_context and event_context.get('flags_today'):
        callout_flags.extend(event_context['flags_today'])
        combined_notes.extend([event_context['notes'].get(f, "Market event.") for f in event_context['flags_today']])
        
    # Append Quality Notes from Rates Curve (Section 09)
    if rates_curve and rates_curve.get('quality', {}).get('notes'):
        q_notes = rates_curve['quality']['notes']
        # Filter out internal sentinel notes like 'partial_section09_parse' for cleaner UI
        clean_q_notes = [n for n in q_notes if not n.startswith("partial_")]
        if clean_q_notes:
            combined_notes.extend(clean_q_notes)
            if "DATA_QUALITY_ALERT" not in callout_flags:
                callout_flags.append("DATA_QUALITY_ALERT")

    if combined_notes:
        event_callout_html = f"""
        <div class="event-callout">
            <span style="font-size: 1.5em;">&#9888;&#65039;</span>
            <div>
                <strong>Event/Data Alert:</strong> {', '.join(callout_flags)}<br>
                <small style="color: #666; font-style: italic;">{' '.join(combined_notes)}</small>
            </div>
        </div>
        """

    # Gather Status Bar Fields
    eq_sig_label = cme_signals.get('equity', {}).get('signal_label', 'Unknown')
    eq_part_label = cme_signals.get('equity', {}).get('participation_label', 'Unknown')
    eq_dir_allowed = cme_signals.get('equity', {}).get('direction_allowed', False)
    eq_dir_str = "Allowed" if eq_dir_allowed else "Unknown"
    spx_trend_status = extracted_metrics.get('sp500_trend_status', 'Unknown')
    
    rt_sig_label = cme_signals.get('rates', {}).get('signal_label', 'Unknown')
    rt_part_label = cme_signals.get('rates', {}).get('participation_label', 'Unknown')
    rt_dir_allowed = cme_signals.get('rates', {}).get('direction_allowed', False)
    rt_dir_str = "Allowed" if rt_dir_allowed else "Unknown"
    ust10y_move = extracted_metrics.get('ust10y_change_bps')
    ust10y_move_str = f"{ust10y_move:+.1f} bps" if ust10y_move is not None else "N/A"

    # Construct Glossary
    glossary_items = [
        ("Signal Badges", [
            ("Directional", "blue", "Futures volume > Options volume. High conviction positioning."),
            ("Hedging-Vol", "orange", "Options volume >= Futures volume. Positioning is driven by hedging or volatility bets."),
            ("Low Signal / Noise", "gray", "Total volume change is below the noise threshold. Ignored.")
        ]),
        ("Trend & Participation", [
            ("Trending Up", "green", "Price is rising (>2% over 21 days)."),
            ("Trending Down", "red", "Price is falling (<-2% over 21 days)."),
            ("Expanding", "green", "Open Interest is increasing (New money entering)."),
            ("Contracting", "red", "Open Interest is decreasing (Money leaving/liquidating).")
        ]),
        ("Status & Freshness", [
            ("Allowed", "green", "Directional narrative is permitted."),
            ("Unknown/Redacted", "gray", "Directional narrative is blocked due to low signal quality."),
            ("FRESH", "green", "Data source is current (within 3 days)."),
            ("STALE", "red", "Data source is outdated (>3 days old)."),
            ("&#9888;&#65039; DATA INCOMPLETE", "warning", "Critical data fields were missing from the extraction.")
        ])
    ]

    glossary_content = ""
    for category, items in glossary_items:
        glossary_content += f"<div style='margin-bottom: 15px;'><h4 style='margin-bottom:8px; border-bottom:1px solid #eee;'>{category}</h4>"
        for label, color, desc in items:
            glossary_content += f"<div style='margin-bottom: 4px;'><span class='badge badge-{color}' style='min-width: 120px; width: auto; text-align: center; display: inline-block;'>{label}</span> <span style='font-size: 0.9em; color: #666;'>{desc}</span></div>"
        glossary_content += "</div>"

    glossary_html = f"""
    <div class="algo-box" style="margin-top: 20px;">
        <details>
            <summary style="font-weight: bold; color: #3498db; cursor: pointer;">&#128214; Legend & Glossary</summary>
            <div style="margin-top: 15px; padding: 10px; background: #fff; border-radius: 6px; border: 1px solid #eee;">
                {glossary_content}
            </div>
        </details>
    </div>
    """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Macro Summary - {today}</title>
        <style>{css}</style>
    </head>
    <body>
        <h1>Daily Macro Summary ({today})</h1>
        
        <div style="display: flex; justify-content: center; gap: 15px; margin-bottom: 20px;">
            <span class="badge badge-gray">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</span>
            <span class="badge badge-blue">Data as of: WT: {display_wt_date} / CME: {display_cme_date}</span>
        </div>
        
        <div class="provenance-strip">
            <div class="provenance-item">
                <span class="provenance-label">Equities:</span>
                {make_chip('Pos', eq_sig_label, "Positioning Signal: Based on Futures vs Options dominance")}
                {make_chip('Part', eq_part_label, "Participation: Are participants adding (Expanding) or removing (Contracting) money?")}
                {make_chip('Dir', eq_dir_str, "Directional Conviction: Is the system allowed to interpret price direction?")}
                {make_chip('Trend', spx_trend_status, "Price Trend: 1-month price action (Source: yfinance)")}
            </div>
            <div class="provenance-item" style="border-left: 1px solid #e1e4e8; padding-left: 15px;">
                <span class="provenance-label">Rates:</span>
                {make_chip('Pos', rt_sig_label, "Positioning Signal: Based on Futures vs Options dominance")}
                {make_chip('Part', rt_part_label, "Participation: Are participants adding (Expanding) or removing (Contracting) money?")}
                {make_chip('Dir', rt_dir_str, "Directional Conviction: Is the system allowed to interpret price direction?")}
                {make_chip('10Y', ust10y_move_str, "Yield Move: Basis point change in the 10-Year Treasury yield today")}
            </div>
        </div>

        <div style="text-align: center; margin-bottom: 15px; color: #7f8c8d; font-size: 0.9em; font-style: italic;">
            Independently generated summary. Informational use only&mdash;NOT financial advice. Full disclaimers in footer.
        </div>
        <div class="pdf-link">
            <h3>Inputs</h3>
            <a href="{main_pdf_url}" target="_blank">&#128196; View WisdomTree PDF</a>
            &nbsp;&nbsp;
            <a href="{cme_bulletin_url}" target="_blank" style="background-color: #2c3e50;">&#128202; View CME Bulletin{cme_warning_flag}</a>
        </div>

        {event_callout_html}
        {kn_html}

        <div class="layout-wrapper">
            <div class="toc-sidebar">
                <h3>Contents</h3>
                <a href="#scoreboard">1. Scoreboard</a>
                <a href="#takeaway">2. Executive Takeaway</a>
                <a href="#fiscal">3. Fiscal Dominance</a>
                <a href="#rates">4. Rates & Curve</a>
                <a href="#credit">5. Credit Stress</a>
                <a href="#engine">6. Engine Room</a>
                <a href="#valuation">7. Valuation</a>
                <a href="#conclusion">8. Conclusion</a>
            </div>
            
            <div class="container">
                {columns_html}
            </div>
        </div>

        <div class="algo-box">
            <h3>&#129518; Technical Audit: Ground Truth Calculation</h3>
            {score_html}
            {sig_html}
            <small><em>These scores are calculated purely from extracted data points using fixed algorithms, serving as a benchmark for the AI models below.</em></small>
            
            <details style="margin-top: 15px; cursor: pointer;">
                <summary style="font-weight: bold; color: #3498db;">Show Calculation Formulas</summary>
                <div style="margin-top: 10px; font-size: 0.9em; background: #fff; padding: 10px; border: 1px solid #ddd; border-radius: 5px;">
                    <ul style="list-style-type: disc; padding-left: 20px;">
                        <li><strong>Liquidity Conditions:</strong> 5.0 + (log2(4.5 / HY_Spread) * 3.0) - max(0, (Real_Yield_10Y - 1.5) * 2.0)</li>
                        <li><strong>Valuation Risk:</strong> 5.0 + ((Forward_PE - 18.0) * 0.66)</li>
                        <li><strong>Inflation Pressure:</strong> 5.0 + ((Inflation_Expectations_5y5y - 2.25) * 10.0)</li>
                        <li><strong>Credit Stress:</strong> 2.0 + ((HY_Spread - 3.0) * 1.6) [Min 2.0]</li>
                        <li><strong>Growth Impulse:</strong> 5.0 + ((Yield_10Y - Yield_2Y - 0.50) * 3.5)</li>
                        <li><strong>Risk Appetite:</strong> 10.0 - ((VIX - 10.0) * 0.5)</li>
                    </ul>
                    <p style="margin-top: 5px; font-style: italic;">All scores are clamped between 0.0 and 10.0.</p>
                </div>
            </details>
        </div>

        {glossary_html}

        <div class="footer">
...
            <div style="margin-bottom: 20px; color: #7f8c8d; font-size: 0.85em; font-style: italic; line-height: 1.4; border-top: 1px solid #eee; padding-top: 20px;">
                This is an independently generated summary of the publicly available WisdomTree Daily Dashboard and CME Data. Not affiliated with, reviewed by, or approved by WisdomTree or CME Group. Third-party sources are not responsible for the accuracy of this summary. No warranties are made regarding completeness, accuracy, or timeliness; data may be delayed or incorrect.
                <br><strong>This content is for informational purposes only and is NOT financial advice.</strong> No fiduciary or advisor-client relationship is formed. This is not an offer or solicitation to buy or sell any security. Trading involves significant risk of loss.
                <br>Use at your own risk; the author disclaims liability for any losses or decisions made based on this content. Consult a qualified financial professional. Past performance is not indicative of future results. Automated extraction and AI analysis may contain errors or misinterpretations.
            </div>
            Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        </div>
    </body>
    </html>
    """
    
    with open("summaries/index.html", "w", encoding="utf-8") as f:
        # Add hidden provenance data for reproducibility
        provenance_data = {
            "today": today,
            "pdfs": [PDF_SOURCES[k] for k in PDF_SOURCES],
            "extracted_metrics": extracted_metrics
        }
        f.write(f"<!-- Provenance: {json.dumps(provenance_data)} -->\n")
        f.write(html_content)
    print("HTML report generated.")

def send_email(subject, body_markdown, pages_url):
    print("Sending email...")
    if not (SMTP_EMAIL and SMTP_PASSWORD and RECIPIENT_EMAIL): return

    msg = MIMEMultipart()
    msg['From'] = SMTP_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = subject

    full_body = body_markdown
    if pages_url:
        full_body = f"\U0001F310 **View as Webpage:** {pages_url}\n\n" + full_body

    msg.attach(MIMEText(full_body, 'plain'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        print("Email sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}")

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    
    try:
        pdf_paths = download_pdfs(PDF_SOURCES)
    except Exception as e:
        print(f"Error fetching PDFs: {e}")
        return

    # Phase 1: Ground Truth Extraction
    extracted_metrics = {}
    sec09_raw = {}
    algo_scores = {}
    
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        # 1. Main Extraction (WisdomTree + CME Vol)
        main_pdfs = {k: v for k, v in pdf_paths.items() if k in ['wisdomtree', 'cme_vol']}
        extracted_metrics = extract_metrics_gemini(main_pdfs)
        
        # 2. Section 09 Extraction (CME Rates Curve)
        sec09_pdf = {k: v for k, v in pdf_paths.items() if k == 'cme_sec09'}
        if sec09_pdf:
            print("Extracting CME Section 09 (Rates Curve)...")
            sec09_raw = extract_metrics_gemini(sec09_pdf, prompt_override=EXTRACTION_PROMPT_SEC09)
    
    # Process Curve Data
    cme_rates_curve = process_cme_sec09(sec09_raw)
    
    # Fetch Live Fallbacks (VIX)
    live_metrics = fetch_live_data()
    if not live_metrics:
        print("Warning: Live data fetch (yfinance source) failed completely.")
    
    # Merge
    for k, v in live_metrics.items():
        if k not in extracted_metrics or extracted_metrics[k] is None:
            extracted_metrics[k] = v

    algo_scores, score_details = calculate_deterministic_scores(extracted_metrics)
    
    # Pre-calculate Signals with Asset-Specific Thresholds
    equity_signal = determine_signal(
        extracted_metrics.get('cme_equity_futures_oi_change'),
        extracted_metrics.get('cme_equity_options_oi_change'),
        noise_threshold=NOISE_THRESHOLDS.get("equity", 50000)
    )
    rates_signal = determine_signal(
        extracted_metrics.get('cme_rates_futures_oi_change'),
        extracted_metrics.get('cme_rates_options_oi_change'),
        noise_threshold=NOISE_THRESHOLDS.get("rates", 75000)
    )

    ground_truth_context = {
        "extracted_metrics": extracted_metrics,
        "calculated_scores": algo_scores,
        "cme_signals": {
            "equity": equity_signal,
            "rates": rates_signal
        },
        "cme_rates_curve": cme_rates_curve
    }
    
    # Event Context - Anchored to effective market date
    effective_date = live_metrics.get('sp500_current_date', today)
    event_context = get_event_context(effective_date)
    print(f"Event Context (as of {effective_date}): {json.dumps(event_context, indent=2)}")

    # Generate Deterministic Verification Block
    verification_block = generate_verification_block(effective_date, extracted_metrics, ground_truth_context['cme_signals'], event_context)

    # Phase 2: Summarization
    
    if RUN_MODE.startswith("BENCHMARK"):
        print(f"--- RUNNING {RUN_MODE} MODE ---")
        summaries = {}
        
        # 1. Run Gemini Native
        try:
            summaries[GEMINI_MODEL] = summarize_gemini(pdf_paths, ground_truth_context, event_context)
        except Exception as e:
            summaries[GEMINI_MODEL] = f"Failed: {e}"

        # 2. Run OpenRouter Benchmark Models
        for model in BENCHMARK_MODELS:
            print(f"Running {model}...")
            # We re-use summarize_openrouter but override the model
            summaries[model] = summarize_openrouter(pdf_paths, ground_truth_context, event_context, model_override=model)
            
        # Save Report
        target_file = "benchmark_data.html" if RUN_MODE == "BENCHMARK_JSON" else "benchmark.html"
        generate_benchmark_html(today, summaries, ground_truth=ground_truth_context, event_context=event_context, filename=target_file)
        
    else:
        # PRODUCTION MODE
        summary_or = "OpenRouter summary skipped."
        summary_gemini = "Gemini summary skipped."

        if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
            summary_or = summarize_openrouter(pdf_paths, ground_truth_context, event_context)
        if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
            summary_gemini = summarize_gemini(pdf_paths, ground_truth_context, event_context)
        
        # Save & Report
        os.makedirs("summaries", exist_ok=True)
        generate_html(today, summary_or, summary_gemini, algo_scores, score_details, extracted_metrics, ground_truth_context.get('cme_signals'), verification_block, event_context, cme_rates_curve)
        
        # Email (Production Only)
        repo_name = GITHUB_REPOSITORY.split("/")[-1]
        owner_name = GITHUB_REPOSITORY.split("/")[0]
        pages_url = f"https://{owner_name}.github.io/{repo_name}/"
        
        full_audit_data = {
            "ground_truth": ground_truth_context,
            "event_context": event_context
        }
        
        email_body = f"Check the attached report for today's summary.\n\nAudit Data: {json.dumps(full_audit_data, indent=2)}"
        send_email(f"Daily Macro Summary - {today}", email_body, pages_url)

if __name__ == "__main__":
    main()