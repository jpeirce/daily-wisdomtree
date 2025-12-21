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
  "real_yield_10y": float, // 10-Year Real Yield (TIPS)
  "inflation_expectations_5y5y": float, // 5y5y Forward Inflation Expectation
  "yield_10y": float, // 10-Year Treasury Nominal Yield
  "yield_2y": float, // 2-Year Treasury Nominal Yield
  "interest_coverage_small_cap": float, // S&P 600 Interest Coverage Ratio
  
  // From CME Section 01 Report
  "cme_total_volume": int, // Total Exchange Volume (Combined Total)
  "cme_total_open_interest": int, // Total Open Interest (Combined Total)
  
  // Specific OI Changes (Net Change Column)
  "cme_rates_futures_oi_change": float, // INTEREST RATES -> FUTURES ONLY -> NET CHGE OI
  "cme_rates_options_oi_change": float, // INTEREST RATES -> OPTIONS ONLY -> NET CHGE OI
  "cme_equity_futures_oi_change": float, // EQUITY INDEX -> FUTURES ONLY -> NET CHGE OI
  "cme_equity_options_oi_change": float  // EQUITY INDEX -> OPTIONS ONLY -> NET CHGE OI
}
"""

SUMMARY_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the provided visual inputs (Macro Dashboard & CME Bulletin) to produce a strategic, easy-to-digest market outlook.

INPUTS PROVIDED (Vision):
1. WisdomTree Daily Snapshot (Images): Charts, Spreads, and Yield Curve data.
2. CME Daily Bulletin (Images): Dense tables showing Volume and Open Interest (Commitment).

CRITICAL: You have been provided with PRE-CALCULATED Ground Truth Scores below. You MUST use these exact scores in your Scoreboard.

Ground Truth Data (Use these scores exactly):
{ground_truth_json}

# === BLOCK 1: CME ANALYTIC FRAMEWORK (Strict Signal Logic) ===

A. DEFINITIONS & PRE-CHECKS:
   * **Rates Definition:** "Price DOWN" = Treasury Yields RISING (from WisdomTree PDF). "Price UP" = Treasury Yields FALLING.
   * **Noise Filter (Per Asset Class):** IF max(abs(Futures OI Δ), abs(Options OI Δ)) < 50,000 contracts for a specific asset:
     * Label = "Low Signal / Noise".
     * Direction = "Unknown".
     * **SKIP Block 1.B (The Gate) and 1.E (Directional Interpretation) for this asset.**

B. THE "FUTURES vs. OPTIONS" GATE (Evaluate Separately per Asset Class):
   * **Rule:** Evaluate using ABSOLUTE values to determine dominance.
   * **Logic:**
     * IF abs(Options OI Δ) >= abs(Futures OI Δ): Signal Quality = **Hedging/Vol** (Low Confidence). Downgrade language.
     * IF abs(Futures OI Δ) > abs(Options OI Δ): Signal Quality = **Directional** (High Confidence). Proceed to Step C.

C. PRICE TREND & STALENESS CHECK (Priority: Live Data > WisdomTree PDF):
   * **Step 1: Check Ground Truth Data.**
     * IF `sp500_trend_status` exists and is not "Unknown":
       * **Trend Status** = The value provided in `sp500_trend_status` (e.g., "Trending Up", "Flat").
       * **Status** = "Fresh" (treated as Fresh when auditable and not Unknown).
       * **MUST CITE:** You must quote the `sp500_trend_audit` string in your verification block to prove the source.
       * **Ignore** the WisdomTree PDF chart for trend determination.
   * **Step 2: Fallback to PDF (Only if Live Data is missing/unknown):**
     * **Freshness Rule:** Compare Chart "as of" Date vs. Report Date.
       * Status = **Fresh** ONLY IF trading-day difference is confidently <= 10.
       * Otherwise, Status = **Stale**.
     * **Impact:** IF Status = Stale, then Trend Status MUST be **Stale** and Direction MUST be **Unknown**.
     * **Readability:** If the chart is pixelated or the last month's slope is ambiguous, Trend Status = **Unreadable**.
   * **Valid States:** Flat, Trending Up, Trending Down, Stale, Unreadable.

D. THE "SIDEWAYS" PROTOCOL (Only if Signal = Directional AND Trend Status = Flat):
   * **LABEL:** "Position Build in Balance."
     * *Meaning:* Risk added, buyers/sellers matched. Direction Balanced.
     * *Constraint:* Do not upgrade to "Breakout Imminent" (insufficient chart granularity).

E. DIRECTIONAL INTERPRETATION (Only if Trend is Valid/Current + Directional Signal):
   * **CRITICAL INVARIANT:** IF Signal Quality is NOT "Directional", Direction MUST be **Unknown**. NO EXCEPTIONS.
   * Trend UP + Futures OI UP = Bullish Conviction (New Longs).
   * Trend DOWN + Futures OI UP = Bearish Conviction (New Shorts).
   * Trend UP + Futures OI DOWN = Short Covering (Weak Rally).
   * Trend DOWN + Futures OI DOWN = Long Liquidation (Weak Selloff).
   * **IF Trend = Stale/Unreadable:** Direction = **Unknown** (Do not guess).

# === BLOCK 2: VISUAL EXTRACTION INSTRUCTIONS ===

### 0. Visual Data Extraction (Internal Logic)
1. **Scan CME Section 01 (Separately):**
   * **Equities:** Extract Signed OI Δ. Check Noise Filter first. If Valid, compare ABS values for the Gate.
   * **Rates:** Extract Signed OI Δ. Check Noise Filter first. If Valid, compare ABS values for the Gate.
2. **Scan WisdomTree PDF (Price Direction & Freshness):**
   * **Equities:** Check S&P 500 Chart (Header: "S&P 500 Index Price Level...").
     * *Date Check:* Is it Fresh (<=10 days) or Stale?
     * *Trend Check:* If Fresh, determine state: Flat, Trending Up, or Trending Down?
   * **Rates:** Check "Treasury Yields" Table (Pg 1). Did 10Y Yields rise (Price Down) or fall (Price Up)?
3. **Construct The Verdicts:**
   * *Signal Quality:* [Directional / Hedging-Vol / Noise]
   * *Direction:* [Bullish / Bearish / Balanced / Unknown]
   * *Trend Status:* [Trending Up / Trending Down / Flat / Stale / Unreadable]

**OUTPUT INSTRUCTION:**
Print the **DATA VERIFICATION** block below first (exactly as shown). **THEN** continue with the Final Output Structure (Scoreboard, Executive Takeaway, etc.).

> **DATA VERIFICATION:**
> * **Invariant Check:** IF Signal != "Directional" THEN Direction = "Unknown".
> * **Date Check:** Report Date: [Date] | SPX Trend Source: [yfinance/PDF]
> * **Trend Audit:** [Quote `sp500_trend_audit` here if yfinance used, else "PDF Chart Analysis"]
> * **Equities:** Futures OI Δ [Signed Val] | Options OI Δ [Signed Val] | Signal: [Type] | Trend Status: [Status] | Direction: [Status]
> * **Rates:** Futures OI Δ [Signed Val] | Options OI Δ [Signed Val] | Signal: [Type] | Direction: [Status]

# === BLOCK 3: FINAL OUTPUT STRUCTURE ===

### 1. The Dashboard (Scoreboard)

Create a table with these 6 Dials. USE THE PRE-CALCULATED SCORES PROVIDED ABOVE.
*In the 'Justification' column, reference the visual evidence from the CME images (Volume/OI) to support the score.*

| Dial | Score (0-10) | Justification (Data Source: Daily Market Snapshot + CME) |
|---|---|---|
| Growth Impulse | [Score] | [Brief justification] |
| Inflation Pressure | [Score] | [Brief justification] |
| Liquidity Conditions | [Score] | [Look at CME Image: Is Volume high (deep liquidity) or low?] |
| Credit Stress | [Score] | [Brief justification] |
| Valuation Risk | [Score] | [Brief justification] |
| Risk Appetite | [Score] | [Is participation/leverage expanding (OI rising) or contracting (OI falling)? Direction depends on Gate.] |

### 2. Executive Takeaway (5–7 sentences)
[Regime Name, The Driver, The Pivot]
*Constraint: Explicitly state if the CME positioning (OI changes) confirms the price action seen in the WisdomTree charts. Use Combined Totals ONLY for gauging general liquidity/participation. Do NOT use Combined Totals for directional conviction.*

### 3. The "Fiscal Dominance" Check (Monetary Stress)
[Data, Implication]

### 4. Rates & Curve Profile
[Shape, Implication]
**The Positioning Check (Source: CME Section 01 Images):**
* **Step 1:** Compare "FUTURES ONLY" OI Change vs. "OPTIONS ONLY" OI Change for Interest Rates.
* **Step 2: Determine Signal Quality:**
    * *IF Futures OI > Options OI:* You may describe the move with **High Confidence** (e.g., "Directional positioning likely increased").
    * *IF Options OI > Futures OI:* You must **Qualify** the signal (e.g., "Dominated by Options activity, suggesting complex positioning or hedging rather than a pure directional bet").
* **Output:** State the Futures/Options split to justify your confidence level. Combine with WisdomTree Yield direction (e.g., Yields Up + Futures OI Up = Likely Shorting).

### 5. The "Canary in the Coal Mine" (Credit Stress)
[Data, Implication]

### 6. The "Engine Room" (Market Breadth)
[Data, Implication]
*Synthesize the CME Image data. Check the Equity Index Futures vs. Options OI split. Is the leverage expansion driven by directional bets (Futures) or hedging (Options)?*

### 7. Valuation & "Smart Money"
[Data, International, Implication]

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

        # Fetch S&P 500 for Trend/Freshness (using ^GSPC Index)
        # Fetch 3mo to safely handle holidays and strict 21-day lookback
        spx = yf.Ticker("^GSPC")
        hist_spx = spx.history(period="3mo")
        
        # Determine strict "Close-to-Close" indices
        if not hist_spx.empty:
            last_date = hist_spx.index[-1].date()
            today_date = datetime.now().date()
            
            # If the last row is today, it's a partial bar (live). Use yesterday's close for trend stability.
            if last_date == today_date:
                current_idx = -2
            else:
                current_idx = -1
            
            # Check staleness: If the "current" data point is older than 5 days (weekend + holidays), flag it.
            current_data_date = hist_spx.index[current_idx].date()
            days_lag = (today_date - current_data_date).days
            
            if days_lag > 5:
                print(f"Warning: SPX data is stale. Last available: {current_data_date} (Lag: {days_lag} days)")
                data['sp500_trend_status'] = "Unknown"
                data['sp500_1mo_change_pct'] = None
                data['sp500_trend_audit'] = f"Data Stale (Lag: {days_lag} days)"
                return data

            # We want strictly 21 trading days ago
            # If current_idx is -1, we need -22. If -2, we need -23.
            prior_idx = current_idx - 21
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

def summarize_openrouter(pdf_paths, ground_truth):
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
    
    formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(ground_truth_json=json.dumps(ground_truth, indent=2))
    
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

def summarize_gemini(pdf_paths, ground_truth):
    print(f"Summarizing with Gemini ({GEMINI_MODEL})...")
    if not AI_STUDIO_API_KEY: return "Error: Key missing"

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(ground_truth_json=json.dumps(ground_truth, indent=2))
    
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

def clean_llm_output(text):
    text = text.strip()
    if text.startswith("```markdown"): text = text[11:]
    elif text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
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

def generate_html(today, summary_or, summary_gemini, scores, details):
    print("Generating HTML report...")
    summary_or = clean_llm_output(summary_or)
    summary_gemini = clean_llm_output(summary_gemini)
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    score_html = "<ul style='list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 15px;'>"
    for k, v in scores.items():
        color = get_score_color(k, v)
        detail_text = details.get(k, "Unknown")
        warning = ""
        if "Default" in detail_text or "Error" in detail_text:
            warning = " <span title='" + detail_text + "' style='cursor: help;'>⚠️</span>"
        else:
             warning = " <span title='" + detail_text + "' style='cursor: help; opacity: 0.5;'>✅</span>"

        score_html += f"<li style='background: white; padding: 10px; border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); flex: 1 0 140px; text-align: center; border-left: 5px solid {color};'><strong>{k}</strong>{warning}<br><span style='font-size: 1.5em; color: {color}; font-weight: bold;'>{v}/10</span></li>"
    score_html += "</ul>"

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f6f8; }
    h1 { text-align: center; color: #2c3e50; margin-bottom: 30px; }
    .pdf-link { display: block; text-align: center; margin-bottom: 20px; }
    .pdf-link a { display: inline-block; background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; }
    .container { display: flex; gap: 20px; flex-wrap: wrap; }
    .column { flex: 1; min-width: 300px; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    .column h2 { border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top: 0; color: #34495e; }
    .footer { text-align: center; margin-top: 40px; font-size: 0.9em; color: #666; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; }
    .algo-box { background: #e8f6f3; padding: 15px; border-radius: 5px; margin-bottom: 20px; border: 1px solid #d1f2eb; }
    """
    
    # We can add links to CME pdfs too if desired, but for now just Main
    main_pdf_url = PDF_SOURCES['wisdomtree']
    cme_pdf_url = PDF_SOURCES['cme_vol']
    
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
            This is an independently generated summary of the publicly available WisdomTree Daily Dashboard and CME Data. Not affiliated with WisdomTree or CME.
            <br><strong>This content is for informational purposes only and is NOT financial advice.</strong>
        </div>
        <div class="pdf-link">
            <h3>Inputs</h3>
            <a href="{main_pdf_url}" target="_blank">📄 View WisdomTree PDF</a>
            &nbsp;&nbsp;
            <a href="{cme_pdf_url}" target="_blank" style="background-color: #2c3e50;">📊 View CME Report</a>
        </div>
        
        <div class="algo-box">
            <h3>🧮 Deterministic "Ground Truth" Scores (Python Calculated)</h3>
            {score_html}
            <small><em>These scores are calculated purely from extracted data points using fixed algorithms, serving as a benchmark for the AI models below.</em></small>
        </div>

        <div class="container">
            <div class="column">
                <h2>🤖 Gemini ({GEMINI_MODEL})</h2>
                {html_gemini}
            </div>
            <div class="column">
                <h2>🧠 OpenRouter ({OPENROUTER_MODEL})</h2>
                {html_or}
            </div>
        </div>
        <div class="footer">
            Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        </div>
    </body>
    </html>
    """
    
    with open("summaries/index.html", "w", encoding="utf-8") as f:
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
    
    ground_truth_context = {
        "extracted_metrics": extracted_metrics,
        "calculated_scores": algo_scores
    }

    # Phase 2: Summarization
    summary_or = "OpenRouter summary skipped."
    summary_gemini = "Gemini summary skipped."

    if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
        summary_or = summarize_openrouter(pdf_paths, ground_truth_context)
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        summary_gemini = summarize_gemini(pdf_paths, ground_truth_context)
    
    # Save & Report
    os.makedirs("summaries", exist_ok=True)
    generate_html(today, summary_or, summary_gemini, algo_scores, score_details)
    
    # Email
    repo_name = GITHUB_REPOSITORY.split("/")[-1]
    owner_name = GITHUB_REPOSITORY.split("/")[0]
    pages_url = f"https://{owner_name}.github.io/{repo_name}/"
    
    email_body = f"Check the attached report for today's summary.\n\nGround Truth Data: {json.dumps(algo_scores, indent=2)}"
    send_email(f"Daily Macro Summary - {today}", email_body, pages_url)

if __name__ == "__main__":
    main()