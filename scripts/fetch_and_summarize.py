import os
import requests
import fitz  # PyMuPDF
import smtplib
import google.generativeai as genai
import markdown
import base64
import io
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import time # For retry mechanism

# Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AI_STUDIO_API_KEY = os.getenv("AI_STUDIO_API_KEY")
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SUMMARIZE_PROVIDER = os.getenv("SUMMARIZE_PROVIDER", "ALL").upper() # ALL, OPENROUTER, GEMINI, NONE
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "jpeirce/daily-wisdomtree") # Defaults if not running in Actions

PDF_URL = "https://www.wisdomtree.com/investments/-/media/us-media-files/documents/resource-library/daily-dashboard.pdf"
OPENROUTER_MODEL = "anthropic/claude-3.5-sonnet" 
GEMINI_MODEL = "gemini-3-pro-preview" 

SYSTEM_PROMPT = """
Role: You are a macro strategist for a top-tier hedge fund.Task: Analyze the attached “Daily Market Snapshot” PDF and produce a strategic, easy-to-digest market outlook. Connect the dots between fixed income, credit, equities, and valuation.


Format Constraints:

Length: Total output must be 700–1,000 words (excluding the scoreboard).

Tables: The "Dashboard Scoreboard" is the only table allowed. All other sections must be Headings + Bullets.

Structure: In every section (3-7), you must include at least one Data: line with a specific number/level, followed immediately by an Implication: line.

Output Structure: 


1. The Dashboard (Scoreboard)

Create a table with these 6 Dials (Score 0-10, where 10 = Maximum Intensity/Risk):


Growth Impulse: (0=Recession, 10=Boom)

Inflation Pressure: (0=Deflation, 10=High Inflation)

Liquidity Conditions: (0=Bone Dry, 10=Flood)

Credit Stress: (0=Relaxed, 10=Panic)

Valuation Risk: (0=Cheap, 10=Bubble)

Risk Appetite: (0=Fear, 10=Greed)

Constraint: Briefly justify each score with ONE specific data point from the PDF.

For each dial, higher = more of that attribute (e.g., higher Valuation Risk = worse/fragile; higher Liquidity Conditions = easier/looser). Do not invert scales.


2. Executive Takeaway (5–7 sentences)

Regime Name: Name today’s regime (e.g., Reflation, Stagflation, Goldilocks, Fiscal Dominance).

The Driver: Call out the single most important cross-asset linkage driving the tape today.

The Pivot: If prior-day values are shown in the PDF, describe what changed vs yesterday. If not, define "pivot" as the change vs the PDF’s recent range (min/median/max) and state that explicitly.
3. The "Fiscal Dominance" Check (Monetary Stress)

Data: Report 10-Year Real Yields (Page 3) and Inflation Expectations/5y5y (Page 5).

Implication: Are real yields rising (tightening) or falling (easing)? Are long-term inflation expectations unanchored? What does this imply for Fed credibility?
4. Rates & Curve Profile

Include a 3–5 bullet mini-section summarizing: 


Shape: The Yield Curve shape and key spreads (2s10s, 3m10y) from the data provided.

Implication: Specifically what this shape implies for duration-sensitive equities (Growth/Tech) vs. Cyclicals.
5. The "Canary in the Coal Mine" (Credit Stress)

Data: Report High Yield Spreads (Page 4), Interest Coverage Ratios, and Cost of Debt (Page 23).

Implication: Is the rising cost of debt actually hurting corporate ability to pay interest yet? Are we seeing early signs of a credit cycle turn?
6. The "Engine Room" (Market Breadth)

Data: Compare "Magnificent 7" performance (Page 21) vs. Equal Weight / Small Caps (Page 7).

Implication: Is the rally broad (healthy) or narrow (fragile)? Does this confirm the "Regime" you named above?
7. Valuation & "Smart Money"

Data: Report S&P 500 Forward P/E vs. Median (Page 19) and Earnings Revisions Ratio.

International: Explicitly report the International valuation discount (Page 14) and tie it to the "where value hides" conclusion.

Implication: Are analysts upgrading earnings to justify these prices, or is this pure multiple expansion?

8. Conclusion & Trade Tilt

Cross-Asset Confirmation: Use USD + Gold/Oil (if present) + Volatility (if present) as a confirmation check for your Trade Tilt.

Risk Rating: (1–10). Definition: 10 = maximum downside risk / highest fragility, 1 = unusually benign. Base the rating on composite conditions (rates, credit, liquidity, valuation, breadth), not any single component.

The Trade: State the base-case tilt (Hard Assets vs. Growth Equities vs. Cash). Conditionality: Add one sentence on what would invalidate this tilt today (tie directly to the triggers below).

Triggers: List 3 concrete "change-my-mind" triggers with specific levels derived from the PDF data (e.g., "If Real Yields cross 2.2%...").


Rules:

1. Precision: Include exact levels from the PDF for key series.

2. Missing Data: If a referenced series/page isn’t present in today's PDF, state “Not provided” and proceed immediately. Do not hallucinate data

3. Use only the attached PDF; do not import outside macro narratives or news unless explicitly asked.
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
    # Limit to first 10 pages to avoid huge payloads if PDF is unexpectedly large
    for page_num in range(min(len(doc), 10)): 
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x zoom for better resolution
        img_data = pix.tobytes("jpeg")
        base64_img = base64.b64encode(img_data).decode('utf-8')
        images.append(base64_img)
    print(f"Converted {len(images)} pages to images.")
    return images

def summarize_openrouter(pdf_path):
    print(f"Summarizing with OpenRouter ({OPENROUTER_MODEL}) - Using Vision...")
    if not OPENROUTER_API_KEY:
        return "Error: OPENROUTER_API_KEY not set."
        
    # Convert PDF to images
    try:
        images = pdf_to_images(pdf_path)
    except Exception as e:
        return f"OpenRouter Error: PDF to Image conversion failed - {e}"

    # Construct Payload with Images
    content_list = [{"type": "text", "text": SYSTEM_PROMPT}]
    
    for img_b64 in images:
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}"
            }
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
    except requests.exceptions.HTTPError as e:
        print(f"OpenRouter HTTP Error: {e.response.status_code} - {e.response.text}")
        return f"OpenRouter HTTP Error ({e.response.status_code}): {e.response.text}"
    except Exception as e:
        return f"OpenRouter Error: {e}"

def summarize_gemini(pdf_path):
    print(f"Summarizing with Gemini ({GEMINI_MODEL}) - Using Native PDF Vision...")
    if not AI_STUDIO_API_KEY:
        return "Error: AI_STUDIO_API_KEY not set."

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    # Upload the PDF file
    print("Uploading PDF to Gemini...")
    try:
        sample_pdf = genai.upload_file(pdf_path, mime_type="application/pdf")
        print(f"Uploaded file: {sample_pdf.uri}")
    except Exception as e:
        print(f"Failed to upload PDF to Gemini: {e}")
        return f"Gemini Error: PDF Upload Failed - {e}"
    
    retries = 3
    for i in range(retries):
        try:
            # Pass the prompt AND the file
            response = model.generate_content([SYSTEM_PROMPT, sample_pdf])
            return response.text
        except genai.types.BlockedPromptException as e:
            print(f"Gemini Blocked Prompt Error: {e}")
            return f"Gemini Blocked Prompt Error: {e}"
        except Exception as e:
            if "429" in str(e): 
                print(f"Gemini Rate Limit Error (429): {e}")
                if i < retries - 1:
                    wait_time = (2 ** i) * 5 
                    print(f"Retrying Gemini in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    return "Gemini Error: Rate limit exceeded (429) after multiple retries. Please check your quota."
            else:
                print(f"Gemini Error: {e}")
                return f"Gemini Error: {e}"
    return "Gemini Error: Unknown error after retries."

def generate_html(today, summary_or, summary_gemini):
    print("Generating HTML report...")
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f6f8; }
    h1 { text-align: center; color: #2c3e50; margin-bottom: 30px; }
    .pdf-link { display: block; text-align: center; margin-bottom: 20px; }
    .pdf-link a { display: inline-block; background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; }
    .pdf-link a:hover { background-color: #2980b9; }
    .container { display: flex; gap: 20px; flex-wrap: wrap; }
    .column { flex: 1; min-width: 300px; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    .column h2 { border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top: 0; color: #34495e; }
    .footer { text-align: center; margin-top: 40px; font-size: 0.9em; color: #666; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; }
    @media (max-width: 768px) { .container { flex-direction: column; } }
    """
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>WisdomTree Daily Summary - {today}</title>
        <style>{css}</style>
    </head>
    <body>
        <h1>WisdomTree Daily Summary ({today})</h1>
        <div class="pdf-link">
            <a href="{PDF_URL}" target="_blank">📄 View Original PDF</a>
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
    print("HTML report generated: summaries/index.html")

def send_email(subject, body_markdown, pages_url):
    print("Sending email...")
    if not (SMTP_EMAIL and SMTP_PASSWORD and RECIPIENT_EMAIL):
        print("Skipping email: Credentials not set.")
        return

    msg = MIMEMultipart()
    msg['From'] = SMTP_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = subject

    # Add link to web view
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
    pdf_path = "daily-dashboard.pdf"
    
    try:
        download_pdf(PDF_URL, pdf_path)
    except Exception as e:
        print(f"Error fetching/reading PDF: {e}")
        return

    summary_or = "OpenRouter summary skipped."
    summary_gemini = "Gemini summary skipped."

    if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
        summary_or = summarize_openrouter(pdf_path) # Pass PDF path for Vision
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        summary_gemini = summarize_gemini(pdf_path) # Pass PDF path for Vision
    
    # Save locally
    os.makedirs("summaries", exist_ok=True)
    with open(f"summaries/{today}_openrouter.md", "w", encoding="utf-8") as f:
        f.write(summary_or)
    with open(f"summaries/{today}_gemini.md", "w", encoding="utf-8") as f:
        f.write(summary_gemini)

    # Generate HTML Report
    generate_html(today, summary_or, summary_gemini)

    # Prepare Email
    email_subject = f"WisdomTree Daily Summary - {today}"
    if SUMMARIZE_PROVIDER == "ALL":
        email_subject += " (A/B Test)"
    else:
        email_subject += f" ({SUMMARIZE_PROVIDER} Only)"

    email_body = (
        f"# Daily WisdomTree Summary ({today})\n\n"
        f"Generated with provider: {SUMMARIZE_PROVIDER}\n\n"
        f"---\n\n"
    )
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        email_body += f"## 🤖 Gemini Summary\n\n{summary_gemini}\n\n---\n\n"
    if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
        email_body += f"## 🧠 OpenRouter Summary\n\n{summary_or}\n"
    
    # Determine Pages URL (Best Guess)
    repo_name = GITHUB_REPOSITORY.split("/")[-1] # e.g., daily-wisdomtree
    owner_name = GITHUB_REPOSITORY.split("/")[0] # e.g., jpeirce
    pages_url = f"https://{owner_name}.github.io/{repo_name}/"
    
    send_email(email_subject, email_body, pages_url)

if __name__ == "__main__":
    main()
