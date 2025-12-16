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
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "jpeirce/daily-macro-summary") # Defaults if not running in Actions 

PDF_URL = "https://www.wisdomtree.com/investments/-/media/us-media-files/documents/resource-library/daily-dashboard.pdf"
OPENROUTER_MODEL = "nvidia/nemotron-nano-12b-v2-vl" 
GEMINI_MODEL = "gemini-3-pro-preview" 

# --- Prompts ---

EXTRACTION_PROMPT = """
You are a precision data extractor. Your job is to read PDF pages containing financial dashboards and extract specific numerical data into valid JSON.

• DO NOT provide commentary, analysis, or summary.
• ONLY return a valid JSON object.
• Extract numbers as decimals (e.g., 2.84, not "2.84%" or "two point eight four").
• If a value is missing or unreadable, use `null`.

Extract the following keys from the PDF.

{
  "hy_spread_current": float, // High Yield Spread (e.g. 2.84)
  "hy_spread_median": float, // Historical Median HY Spread (if available)
  "forward_pe_current": float, // S&P 500 Forward P/E
  "forward_pe_median": float, // S&P 500 Forward P/E Median
  "real_yield_10y": float, // 10-Year Real Yield (TIPS)
  "inflation_expectations_5y5y": float, // 5y5y Forward Inflation Expectation
  "yield_10y": float, // 10-Year Treasury Nominal Yield
  "yield_2y": float, // 2-Year Treasury Nominal Yield
  "interest_coverage_small_cap": float // S&P 600 Interest Coverage Ratio
}
"""

SUMMARY_SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.
Task: Analyze the attached “Daily Market Snapshot” PDF and produce a strategic, easy-to-digest market outlook.

CRITICAL: You have been provided with PRE-CALCULATED Ground Truth Scores for the Dashboard. You MUST use these exact scores in your Scoreboard table. Do not hallucinate or recalculate them.

However, if you find qualitative evidence in the PDF that contradicts a score (e.g., Score says 'Safe' but text says 'Bankruptcies rising'), you must explicitly mention this DIVERGENCE in the 'Justification' column or the Executive Takeaway. Do not simply rubber-stamp the score if the context suggests otherwise.

Ground Truth Data:
{ground_truth_json}

Format Constraints:
Length: Total output must be 700–1,000 words.
Tables: The "Dashboard Scoreboard" is the only table allowed.
formatting: Use '###' for all section headers.

Output Structure:

### 1. The Dashboard (Scoreboard)

Create a table with these 6 Dials. USE THE PRE-CALCULATED SCORES PROVIDED ABOVE.

| Dial | Score (0-10) | Justification (Data Source: Daily Market Snapshot) |
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

def download_pdf(url, filename):
    print(f"Downloading PDF from {url}...")
    response = requests.get(url)
    response.raise_for_status()
    with open(filename, "wb") as f:
        f.write(response.content)
    print("Download complete.")

def pdf_to_images(pdf_path):
    print(f"Converting PDF to images for Vision...")
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

import math

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
            # Logic: ... (Same as before)
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

def extract_metrics_gemini(pdf_path):
    print("Extracting Ground Truth Data with Gemini...")
    if not AI_STUDIO_API_KEY: return {}

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    try:
        sample_pdf = genai.upload_file(pdf_path, mime_type="application/pdf")
        response = model.generate_content([EXTRACTION_PROMPT, sample_pdf])
        
        # Clean response to get pure JSON
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        print(f"Extracted Data: {data}")
        return data
    except Exception as e:
        print(f"Extraction failed: {e}")
        return {}

# --- Summarization ---

def summarize_openrouter(pdf_path, ground_truth):
    print(f"Summarizing with OpenRouter ({OPENROUTER_MODEL})...")
    if not OPENROUTER_API_KEY: return "Error: Key missing"
    
    images = pdf_to_images(pdf_path)
    
    # Inject Ground Truth into Prompt
    formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(ground_truth_json=json.dumps(ground_truth, indent=2))
    
    content_list = [{"type": "text", "text": formatted_prompt}]
    for img_b64 in images:
        content_list.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/jpeirce/daily-wisdomtree",
        "X-Title": "WisdomTree Daily Summary",
        "Content-Type": "application/json"
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": content_list}]
    }
    
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"OpenRouter Error: {e}"

def summarize_gemini(pdf_path, ground_truth):
    print(f"Summarizing with Gemini ({GEMINI_MODEL})...")
    if not AI_STUDIO_API_KEY: return "Error: Key missing"

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    # Inject Ground Truth
    formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(ground_truth_json=json.dumps(ground_truth, indent=2))
    
    try:
        sample_pdf = genai.upload_file(pdf_path, mime_type="application/pdf")
        response = model.generate_content([formatted_prompt, sample_pdf])
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def clean_llm_output(text):
    text = text.strip()
    if text.startswith("```markdown"):
        text = text[11:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def get_score_color(category, score):
    # Logic: Define what is "Good" (Green) vs "Bad" (Red)
    # High Score (8-10) = Intense. Low Score (0-3) = Weak.
    
    # Categories where High = Risk/Bad (Red)
    high_risk_categories = ["Inflation Pressure", "Credit Stress", "Valuation Risk"]
    
    # Categories where High = Good/Bullish (Green)
    high_good_categories = ["Growth Impulse", "Liquidity Conditions", "Risk Appetite"]
    
    if category in high_risk_categories:
        if score >= 7: return "#e74c3c" # Red (High Risk)
        if score <= 4: return "#27ae60" # Green (Safe)
        
    if category in high_good_categories:
        if score >= 7: return "#27ae60" # Green (Strong)
        if score <= 4: return "#e74c3c" # Red (Weak)
        
    return "#2c3e50" # Default Dark Blue/Black

def generate_html(today, summary_or, summary_gemini, scores, details):
    print("Generating HTML report...")
    summary_or = clean_llm_output(summary_or)
    summary_gemini = clean_llm_output(summary_gemini)
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    # Format scores for display with Colors and Confidence
    score_html = "<ul style='list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 15px;'>"
    for k, v in scores.items():
        color = get_score_color(k, v)
        detail_text = details.get(k, "Unknown")
        warning = ""
        if "Default" in detail_text or "Error" in detail_text:
            warning = " <span title='" + detail_text + "' style='cursor: help;'>⚠️</span>"
        else:
            # Add checkmark for calculated
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
            This is an independently generated summary of the publicly available WisdomTree Daily Dashboard. Not affiliated with WisdomTree Investments.
        </div>
        <div class="pdf-link">
            <a href="{PDF_URL}" target="_blank">📄 View Original PDF</a>
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

def fetch_live_data():
    print("Fetching live market data (fallback)...")
    data = {}
    try:
        # Fetch VIX
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d")
        if not hist.empty:
            data['vix_index'] = round(hist['Close'].iloc[-1], 2)
            print(f"Live VIX: {data['vix_index']}")
    except Exception as e:
        print(f"Error fetching live data: {e}")
    return data

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    pdf_path = "daily-dashboard.pdf"
    
    try:
        download_pdf(PDF_URL, pdf_path)
    except Exception as e:
        print(f"Error fetching PDF: {e}")
        return

    # Phase 1: Ground Truth Extraction
    extracted_metrics = {}
    algo_scores = {}
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        extracted_metrics = extract_metrics_gemini(pdf_path)
    
    # Fetch Live Fallbacks (VIX)
    live_metrics = fetch_live_data()
    
    # Merge (PDF takes precedence usually, but VIX is often missing from PDF)
    # If PDF has null for a key, allow live data to fill it.
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
        summary_or = summarize_openrouter(pdf_path, ground_truth_context)
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        summary_gemini = summarize_gemini(pdf_path, ground_truth_context)
    
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