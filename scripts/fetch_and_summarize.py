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
    "cme_vol": "https://www.cmegroup.com/daily_bulletin/current/Section01_Exchange_Overall_Volume_And_Open_Interest.pdf"
}
OPENROUTER_MODEL = "openai/gpt-5.2" 
GEMINI_MODEL = "gemini-3-pro-preview" 

# --- Prompts ---

EXTRACTION_PROMPT = """
You are a precision data extractor. Your job is to read the attached PDF pages (Financial Dashboard + CME Reports) and extract specific numerical data into valid JSON.

• DO NOT provide commentary, analysis, or summary.
• ONLY return a valid JSON object.
• Extract numbers as decimals.
• If a value is missing or unreadable, use `null`.

Extract the following keys:

{
  // From WisdomTree Dashboard
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

SUMMARY_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the provided visual inputs (Macro Dashboard & CME Bulletin) to produce a strategic, easy-to-digest market outlook.

GLOBAL CONSTRAINTS (Language & Tone):
1. **No Actor Attribution:** Do NOT use terms like "Smart Money", "Whales", "Insiders", "Institutions", "Big Players", "Professionals", "Strong Hands", "Hedge Funds", "Asset Managers", "Dealers", "Banks", "Allocators", "Real Money", "Pensions", "Sovereign", "Macro Funds", "Levered Funds", or "CTAs".
2. **Structural Phrasing:** Describe activity as "futures-led / options-led positioning" without naming specific participant types.
   * *Bad:* "Institutions are shorting aggressively."
   * *Good:* "Futures-led positioning increased; direction remains unknown unless Signal=Directional and Trend is valid."
   * *Bad:* "Smart money is buying the dip."
   * *Good:* "Options skew indicates hedging activity has moderated."

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

*   **Equities Signal:** Provided in `cme_signals.equity.signal_quality`
*   **Equities Direction Allowed:** Provided in `cme_signals.equity.direction_allowed`
*   **Rates Signal:** Provided in `cme_signals.rates.signal_quality`
*   **Rates Direction Allowed:** Provided in `cme_signals.rates.direction_allowed`

**CRITICAL RULES:**
1.  **IF `direction_allowed` is False:** 
    *   The Direction for that asset MUST be "Unknown".
    *   Your narrative MUST remain neutral and non-directional. 
    *   BAN all directional terms: "Bullish", "Bearish", "Rally", "Selloff", "Conviction".
2.  **IF `signal_quality` is "Noise":** 
    *   Direction MUST be "Unknown".
    *   You MUST explicitly state: "Signal is below noise threshold."

# === BLOCK 2: VISUAL EXTRACTION INSTRUCTIONS ===

### 0. Visual Data Validation
1. **Confirm Extracted Values:** Quickly verify that the provided Ground Truth JSON aligns with the visual evidence in the PDF pages.
2. **Rates Focus:** Check "Treasury Yields" Table (Pg 1) to cross-reference yield moves with CME positioning.

**OUTPUT INSTRUCTION:**
Do NOT output a "DATA VERIFICATION" block; one will be prepended automatically by the system using deterministic Python logic. 
Proceed directly to the Final Output Structure (Scoreboard, Executive Takeaway, etc.).

# === BLOCK 3: FINAL OUTPUT STRUCTURE ===

### 1. The Dashboard (Scoreboard)

Create a table with these 6 Dials. USE THE PRE-CALCULATED SCORES PROVIDED ABOVE.
*In the 'Justification' column, reference the visual evidence from the CME images (Volume/OI) to support the score.*

| Dial | Score (0-10) | Justification (Data Source: Daily Market Snapshot + CME) |
|---|---|---|
| Growth Impulse | [Score] | [Brief justification] |
| Inflation Pressure | [Score] | [Brief justification] |
| Liquidity Conditions | [Look at CME Image: Is Volume high (deep liquidity) or low?] |
| Credit Stress | [Score] | [Brief justification] |
| Valuation Risk | [Score] | [Brief justification] |
| Risk Appetite | [Score] | [Cite VIX from Ground Truth. Secondary: Is CME participation expanding or contracting?] |

### 2. Executive Takeaway (5–7 sentences)
[Regime Name, The Driver, The Pivot]
*Constraint: Explicitly state if the CME positioning (OI changes) confirms the price action seen in the WisdomTree charts. Use Combined Totals ONLY for gauging general liquidity/participation. Do NOT use Combined Totals for directional conviction.*

### 3. The "Fiscal Dominance" Check (Monetary Stress)
[Data, Implication]

### 4. Rates & Curve Profile
[Shape, Implication]
**The Positioning Check (Source: CME Section 01 Images):**
* **Step 1:** Use the provided Rates Signal Quality.
* **Step 2:** Combine with WisdomTree Yield direction (e.g., Yields Up + Futures OI Up = Likely Shorting).
* **Output:** State the Futures/Options split to justify your confidence level. 

### 5. The "Canary in the Coal Mine" (Credit Stress)
[Data, Implication]

### 6. The "Engine Room" (Market Breadth)
[Data, Implication]
*Synthesize the CME Image data. Describe the Equity Index positioning based on the provided signal label.*

### 7. Valuation & Positioning
[Data, International, Implication]
*Constraint: Do NOT use terms like "Smart Money", "Whales", or "Insiders". Focus on structural positioning (hedging vs. direction).*

### 8. Conclusion & Trade Tilt
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
                
                trend_status = "Flat"
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
    if futures_delta is None or options_delta is None:
        return {"signal_quality": "Unknown", "direction_allowed": False}
    
    fut_abs = abs(futures_delta)
    opt_abs = abs(options_delta)
    
    # 1. Noise Filter
    if max(fut_abs, opt_abs) < noise_threshold:
        return {"signal_quality": "Noise", "direction_allowed": False}
    
    # 2. The Gate (Dominance check)
    if opt_abs >= fut_abs:
        return {"signal_quality": "Hedging-Vol", "direction_allowed": False}
    else:
        return {"signal_quality": "Directional", "direction_allowed": True}

def generate_verification_block(effective_date, extracted_metrics, cme_signals, event_context):
    eq_sig = cme_signals.get('equity', {})
    rt_sig = cme_signals.get('rates', {})
    
    def fmt_val(v): return f"{v:,}" if isinstance(v, int) else str(v)
    
    bps_change = extracted_metrics.get('ust10y_change_bps')
    rates_text = f"Signal: {rt_sig.get('signal_quality', 'Unknown')}"
    if bps_change is not None:
        rates_text += f" | 10Y Move: {bps_change:+.1f} bps (Live)"

    block = f"""
<details>
<summary><strong>Data Verification</strong> (Click to Expand)</summary>

> **DATA VERIFICATION (DETERMINISTIC):**
> * **Event Flags:** Today: {event_context.get('flags_today', [])} | Recent: {event_context.get('flags_recent', [])}
> * **CME Provenance:** Bulletin Date: "{extracted_metrics.get('cme_bulletin_date', 'Unknown')}" | Total Volume: {fmt_val(extracted_metrics.get('cme_total_volume', 'N/A'))} | Total OI: {fmt_val(extracted_metrics.get('cme_total_open_interest', 'N/A'))}
> * **CME Audit Anchors:** Totals: "{extracted_metrics.get('cme_totals_audit_label', 'N/A')}" | Rates: "{extracted_metrics.get('cme_rates_futures_audit_label', 'N/A')}" | Equities: "{extracted_metrics.get('cme_equity_futures_audit_label', 'N/A')}"
> * **Date Check:** Report Date: {effective_date} | SPX Trend Source: yfinance
> * **Trend Audit:** {extracted_metrics.get('sp500_trend_audit', 'N/A')}
> * **Equities:** Signal: {eq_sig.get('signal_quality', 'Unknown')} | Trend Status: {extracted_metrics.get('sp500_trend_status', 'Unknown')} | Direction Allowed: {eq_sig.get('direction_allowed', False)}
> * **Rates:** {rates_text} | Direction Allowed: {rt_sig.get('direction_allowed', False)}
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

def extract_metrics_gemini(pdf_paths):
    print("Extracting Ground Truth Data with Gemini...")
    if not AI_STUDIO_API_KEY: return {}

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    try:
        content = [EXTRACTION_PROMPT]
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
        print(f"Extraction failed: {e}")
        return {}

# --- Summarization ---

def summarize_openrouter(pdf_paths, ground_truth, event_context):
    print(f"Summarizing with OpenRouter ({OPENROUTER_MODEL})...")
    if not OPENROUTER_API_KEY: return "Error: Key missing"
    
    # Process images for ALL PDFs
    images = []
    # Only prioritize WisdomTree visuals for summarization context if cost/time is concern,
    # but for completeness, we can send all.
    # Note: Nemotron might struggle with >10 pages. 
    # Let's prioritize 'wisdomtree' then 'cme_vol'.
    
    # Logic: Convert WisdomTree first
    if "wisdomtree" in pdf_paths:
        images.extend(pdf_to_images(pdf_paths["wisdomtree"]))
    
    # Then CME (limit pages to first 1 since it's a summary sheet)
    if "cme_vol" in pdf_paths:
        cme_images = pdf_to_images(pdf_paths["cme_vol"])
        images.extend(cme_images[:1]) # Just the first page
    
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
        "model": OPENROUTER_MODEL,
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
    
    formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(
        ground_truth_json=json.dumps(ground_truth, indent=2),
        event_context_json=json.dumps(event_context, indent=2)
    )
    
    content = [formatted_prompt]
    try:
        for name, path in pdf_paths.items():
            f = genai.upload_file(path, mime_type="application/pdf")
            content.append(f"Document: {name}")
            content.append(f)
            
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
        eq_sig_val = cme_signals.get('equity', {}).get('signal_quality', 'Unknown')
        rt_sig_val = cme_signals.get('rates', {}).get('signal_quality', 'Unknown')
        
        # 3a. Force-Overwrite "Signal:" lines with Deterministic Truth
        lines = text.split('\n')
        new_lines = []
        current_section = "Unknown"
        
        for line in lines:
            # Detect Section
            if "Rates & Curve Profile" in line or "Positioning Check" in line:
                current_section = "Rates"
            elif "Engine Room" in line or "Market Breadth" in line:
                current_section = "Equities"
            elif "Executive Takeaway" in line:
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
        
        leakage_pattern = re.compile(r"\b(bullish|bearish|conviction|aggressive|rally|selloff|breakout)\b", re.IGNORECASE)
        
        # Split text into sections by H3 headers
        sections = re.split(r"(?=### )", text)
        processed_sections = []
        filter_applied = False
        
        for section in sections:
            is_rates = "Rates & Curve Profile" in section
            is_equities = "Engine Room" in section or "Market Breadth" in section
            is_summary = "Executive Takeaway" in section or "Conclusion" in section
            
            should_scrub = False
            if is_rates and not rt_allowed: should_scrub = True
            if is_equities and not eq_allowed: should_scrub = True
            if is_summary and (not eq_allowed or not rt_allowed): should_scrub = True
            
            if should_scrub and leakage_pattern.search(section):
                section = leakage_pattern.sub("[direction-redacted]", section)
                filter_applied = True
            
            processed_sections.append(section)
            
        text = "".join(processed_sections)
        if filter_applied and "Note: Automatic direction filter applied" not in text:
            text += "\n\n*(Note: Automatic direction filter applied to non-directional signal sections)*"

    # Pass 4: Signal Badges (Visual Polish)
    # Define color-coded badges for common signal keywords
    badges = {
        r"\bDirectional\b": "badge-blue",
        r"\bHedging[/-]Vol\b": "badge-orange",
        r"\bLow Signal / Noise\b": "badge-gray",
        r"\bBullish\b": "badge-green",
        r"\bBearish\b": "badge-red",
        r"\bTrending Up\b": "badge-green",
        r"\bTrending Down\b": "badge-red",
        r"\bFlat\b": "badge-gray",
        r"\bBalanced\b": "badge-blue",
        r"\bFresh\b": "badge-green",
        r"\bStale\b": "badge-red",
        r"\bUnknown\b": "badge-gray",
        r"\bRedacted\b": "badge-gray"
    }
    for pattern, css_class in badges.items():
        text = re.sub(pattern, f'<span class="badge {css_class}">\\g<0></span>', text, flags=re.IGNORECASE)
        
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

def generate_html(today, summary_or, summary_gemini, scores, details, extracted_metrics, cme_signals=None, verification_block=""):
    print("Generating HTML report...")
    
    # Prepend Verification Block to the raw text BEFORE markdown conversion
    if verification_block:
        summary_or = verification_block + "\n\n" + summary_or
        summary_gemini = verification_block + "\n\n" + summary_gemini

    summary_or = clean_llm_output(summary_or, cme_signals)
    summary_gemini = clean_llm_output(summary_gemini, cme_signals)
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    # Generate Scoreboard using CSS Grid
    score_html = "<div class='score-grid'>"
    for k, v in scores.items():
        color = get_score_color(k, v)
        detail_text = details.get(k, "Unknown")
        warning = ""
        if "Default" in detail_text or "Error" in detail_text:
            warning = " <span title='" + detail_text + "' style='cursor: help;'>⚠️</span>"
        else:
             warning = " <span title='" + detail_text + "' style='cursor: help; opacity: 0.5;'>✅</span>"

        score_html += f"<div style='background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; border-left: 5px solid {color};'><strong>{k}</strong>{warning}<br><span style='font-size: 1.8em; color: {color}; font-weight: bold;'>{v}/10</span></div>"
    score_html += "</div>"

    # Generate Signal Highlights
    sig_html = ""
    if cme_signals:
        sig_html = "<div class='key-numbers' style='border-top: 4px solid #3498db;'>"
        for label, data in cme_signals.items():
            quality = data.get('signal_quality', 'Unknown')
            allowed = "Allowed" if data.get('direction_allowed') else "Redacted"
            color = "#27ae60" if data.get('direction_allowed') else "#7f8c8d"
            sig_html += f"<div class='key-number-item'><span class='key-number-label'>{label.upper()} SIGNAL</span><span class='key-number-value' style='color: {color};'>{quality}</span><small>Direction: {allowed}</small></div>"
        sig_html += "</div>"

    # Generate Key Numbers Strip
    kn = extracted_metrics or {}
    def fmt_num(val):
        if val is None: return "N/A"
        try: return f"{val:,}" if isinstance(val, int) else f"{val:.2f}"
        except: return str(val)

    key_numbers_items = [
        ("S&P 500", fmt_num(kn.get('sp500_current'))),
        ("Forward P/E", f"{fmt_num(kn.get('forward_pe_current'))}x"),
        ("HY Spread", f"{fmt_num(kn.get('hy_spread_current'))}%"),
        ("10Y Nominal", f"{fmt_num(kn.get('yield_10y'))}%"),
        ("10Y Real", f"{fmt_num(kn.get('real_yield_10y'))}%"),
        ("5y5y Inf", f"{fmt_num(kn.get('inflation_expectations_5y5y'))}%"),
        ("VIX", f"{fmt_num(kn.get('vix_index'))}"),
        ("CME Vol", f"{fmt_num(kn.get('cme_total_volume'))}")
    ]
    
    kn_html = "<div class='key-numbers'>"
    for label, val in key_numbers_items:
        kn_html += f"<div class='key-number-item'><span class='key-number-label'>{label}</span><span class='key-number-value'>{val}</span></div>"
    kn_html += "</div>"

    # Build columns conditionally
    columns_html = ""
    if "Gemini summary skipped" not in summary_gemini:
        columns_html += f"""
            <div class="column">
                <h2>🤖 Gemini ({GEMINI_MODEL})</h2>
                {html_gemini}
            </div>
        """
    
    if "OpenRouter summary skipped" not in summary_or:
        columns_html += f"""
            <div class="column">
                <h2>🧠 OpenRouter ({OPENROUTER_MODEL})</h2>
                {html_or}
            </div>
        """

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f6f8; transition: background 0.3s, color 0.3s; }
    h1 { text-align: center; color: #2c3e50; margin-bottom: 20px; }
    .pdf-link { display: block; text-align: center; margin-bottom: 20px; }
    .pdf-link a { display: inline-block; background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; margin: 0 5px; }
    
    /* Provenance Strip */
    .provenance-strip { display: flex; justify-content: center; gap: 20px; background: #fff; padding: 10px; border-radius: 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); margin-bottom: 30px; border: 1px solid #e1e4e8; font-size: 0.85em; color: #586069; }
    .provenance-item { display: flex; align-items: center; gap: 6px; }
    .provenance-label { font-weight: 600; color: #24292e; text-transform: uppercase; font-size: 0.8em; letter-spacing: 0.5px; }
    
    .container { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 40px; }
    .column { flex: 1; min-width: 350px; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    .column h2 { border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top: 0; color: #34495e; }
    .footer { text-align: center; margin-top: 40px; font-size: 0.9em; color: #666; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; }
    .algo-box { background: #e8f6f3; padding: 25px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #d1f2eb; }
    
    /* Grid Scoring */
    .score-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 20px; }
    
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

    /* Native Dark Mode */
    @media (prefers-color-scheme: dark) {
        body { background: #0d1117; color: #c9d1d9; }
        .column, .algo-box, .score-grid > div, .footer, .key-numbers, .provenance-strip { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
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
    }
    """
    
    # We can add links to CME pdfs too if desired, but for now just Main
    main_pdf_url = PDF_SOURCES['wisdomtree']
    cme_pdf_url = PDF_SOURCES['cme_vol']
    
    # Extract provenance info
    cme_date = extracted_metrics.get('cme_bulletin_date', 'N/A')
    spx_audit = extracted_metrics.get('sp500_trend_audit', 'N/A')
    
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
        <div style="text-align: center; margin-bottom: 15px; color: #7f8c8d; font-size: 0.9em; font-style: italic;">
            Independently generated summary. Informational use only—NOT financial advice. Full disclaimers in footer.
        </div>
        <div class="pdf-link">
            <a href="{main_pdf_url}" target="_blank">📄 WisdomTree PDF</a>
            <a href="{cme_pdf_url}" target="_blank">📊 CME Bulletin</a>
        </div>
        
        <div class="provenance-strip">
            <div class="provenance-item">
                <span class="provenance-label">CME Date:</span>
                <span>{cme_date}</span>
            </div>
            <div class="provenance-item">
                <span class="provenance-label">SPX Trend:</span>
                <span title="{spx_audit}">{spx_audit.split('(')[0].strip() if '(' in spx_audit else spx_audit}</span>
            </div>
        </div>

        <div class="container">
            {columns_html}
        </div>

        <div class="algo-box">
            {score_html}
            {sig_html}
            {kn_html}
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

        <div class="footer">
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
        full_body = f"🌐 **View as Webpage:** {pages_url}\n\n" + full_body

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
    algo_scores = {}
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        extracted_metrics = extract_metrics_gemini(pdf_paths)
    
    # Fetch Live Fallbacks (VIX)
    live_metrics = fetch_live_data()
    
    # Merge
    for k, v in live_metrics.items():
        if k not in extracted_metrics or extracted_metrics[k] is None:
            extracted_metrics[k] = v

    algo_scores, score_details = calculate_deterministic_scores(extracted_metrics)
    
    # Pre-calculate Signals
    equity_signal = determine_signal(
        extracted_metrics.get('cme_equity_futures_oi_change'),
        extracted_metrics.get('cme_equity_options_oi_change')
    )
    rates_signal = determine_signal(
        extracted_metrics.get('cme_rates_futures_oi_change'),
        extracted_metrics.get('cme_rates_options_oi_change')
    )

    ground_truth_context = {
        "extracted_metrics": extracted_metrics,
        "calculated_scores": algo_scores,
        "cme_signals": {
            "equity": equity_signal,
            "rates": rates_signal
        }
    }
    
    # Event Context - Anchored to effective market date
    effective_date = live_metrics.get('sp500_current_date', today)
    event_context = get_event_context(effective_date)
    print(f"Event Context (as of {effective_date}): {json.dumps(event_context, indent=2)}")

    # Generate Deterministic Verification Block
    verification_block = generate_verification_block(effective_date, extracted_metrics, ground_truth_context['cme_signals'], event_context)

    # Phase 2: Summarization
    summary_or = "OpenRouter summary skipped."
    summary_gemini = "Gemini summary skipped."

    if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
        summary_or = summarize_openrouter(pdf_paths, ground_truth_context, event_context)
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        summary_gemini = summarize_gemini(pdf_paths, ground_truth_context, event_context)
    
    # Save & Report
    os.makedirs("summaries", exist_ok=True)
    generate_html(today, summary_or, summary_gemini, algo_scores, score_details, extracted_metrics, ground_truth_context.get('cme_signals'), verification_block)
    
    # Email
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