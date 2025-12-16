import os
import requests
import fitz  # PyMuPDF
import smtplib
import google.generativeai as genai
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

PDF_URL = "https://www.wisdomtree.com/investments/-/media/us-media-files/documents/resource-library/daily-dashboard.pdf"
OPENROUTER_MODEL = "openai/gpt-5.2" # or gpt-4o, etc.
GEMINI_MODEL = "gemini-3-pro-preview" # Changed to gemini-experimental

def download_pdf(url, filename):
    print(f"Downloading PDF from {url}...")
    response = requests.get(url)
    response.raise_for_status()
    with open(filename, "wb") as f:
        f.write(response.content)
    print("Download complete.")

def extract_text(pdf_path):
    print(f"Extracting text from {pdf_path}...")
    doc = fitz.open(pdf_path)
    text = "\n".join([page.get_text() for page in doc])
    print(f"Extracted {len(text)} characters.")
    return text

def summarize_openrouter(text):
    print(f"Summarizing with OpenRouter ({OPENROUTER_MODEL})...")
    if not OPENROUTER_API_KEY:
        return "Error: OPENROUTER_API_KEY not set."
        
    prompt = (
        "You are a financial analyst AI. Summarize the key data and market signals from the following "
        "Daily Dashboard. The summary should be ~800 words, include markdown formatting, and highlight "
        "macro trends, valuation signals, sentiment shifts, and market breadth.\n\n" + text
    )
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/jpeirce/daily-wisdomtree",
        "X-Title": "WisdomTree Daily Summary",
        "Content-Type": "application/json"
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}]
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

def summarize_gemini(text):
    print(f"Summarizing with Gemini ({GEMINI_MODEL})...")
    if not AI_STUDIO_API_KEY:
        return "Error: AI_STUDIO_API_KEY not set."

    genai.configure(api_key=AI_STUDIO_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    prompt = (
        "You are a financial analyst AI. Summarize the key data and market signals from the following "
        "Daily Dashboard text. The summary should be ~800 words, include markdown formatting, and highlight "
        "macro trends, valuation signals, sentiment shifts, and market breadth. "
        "Structure it clearly with headers.\n\n" + text
    )
    
    retries = 3
    for i in range(retries):
        try:
            response = model.generate_content(prompt)
            return response.text
        except genai.types.BlockedPromptException as e:
            print(f"Gemini Blocked Prompt Error: {e}")
            return f"Gemini Blocked Prompt Error: {e}"
        except Exception as e:
            if "429" in str(e): # Specific check for rate limit errors in the exception string
                print(f"Gemini Rate Limit Error (429): {e}")
                if i < retries - 1:
                    wait_time = (2 ** i) * 5 # Exponential backoff: 5, 10, 20 seconds
                    print(f"Retrying Gemini in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    return "Gemini Error: Rate limit exceeded (429) after multiple retries. Please check your quota."
            else:
                print(f"Gemini Error: {e}")
                return f"Gemini Error: {e}"
    return "Gemini Error: Unknown error after retries." # Should not be reached

def send_email(subject, body_markdown):
    print("Sending email...")
    if not (SMTP_EMAIL and SMTP_PASSWORD and RECIPIENT_EMAIL):
        print("Skipping email: Credentials not set.")
        return

    msg = MIMEMultipart()
    msg['From'] = SMTP_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = subject

    msg.attach(MIMEText(body_markdown, 'plain'))

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
        raw_text = extract_text(pdf_path)
    except Exception as e:
        print(f"Error fetching/reading PDF: {e}")
        return

    summary_or = "OpenRouter summary skipped."
    summary_gemini = "Gemini summary skipped."

    if SUMMARIZE_PROVIDER in ["ALL", "OPENROUTER"]:
        summary_or = summarize_openrouter(raw_text)
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        summary_gemini = summarize_gemini(raw_text)
    
    # Save locally
    os.makedirs("summaries", exist_ok=True)
    with open(f"summaries/{today}_openrouter.md", "w", encoding="utf-8") as f:
        f.write(summary_or)
    with open(f"summaries/{today}_gemini.md", "w", encoding="utf-8") as f:
        f.write(summary_gemini)

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
    
    send_email(email_subject, email_body)

if __name__ == "__main__":
    main()