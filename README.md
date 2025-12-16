# Daily Macro Summary Bot

This project is an automated system designed to fetch, summarize, and deliver daily market insights from WisdomTree's "Daily Dashboard" PDF. It leverages large language models (LLMs) from both OpenRouter (GPT) and Google AI Studio (Gemini) for comparative analysis, generates a styled HTML report, deploys it to GitHub Pages, and sends a daily email notification.

## Features

*   **Automated PDF Processing:** Downloads the latest WisdomTree Daily Dashboard PDF.
*   **Intelligent Summarization:** Extracts key financial data and generates strategic market outlooks using configurable LLMs.
*   **Dual LLM Support:** Simultaneously generates summaries using both OpenRouter (e.g., GPT models) and Google AI Studio (Gemini models) for A/B testing and comparison.
*   **Customizable Prompt:** Uses a "Macro Strategist" persona with specific formatting and content constraints for relevant analysis.
*   **HTML Report Generation:** Creates a user-friendly, styled HTML page (`index.html`) displaying both LLM summaries side-by-side.
*   **GitHub Pages Deployment:** Automatically publishes the latest HTML report to a public GitHub Pages site for easy web access.
*   **Email Delivery:** Sends a daily email with both summaries and a direct link to the web report.
*   **Cost Management:** Includes logic to estimate and prevent excessive API costs for OpenRouter (Gemini costs are managed via billing account).
*   **Rate Limit Handling:** Implements retry logic for Gemini API calls to gracefully handle transient rate limiting.

## Setup

This repository is **public**.

### 1. GitHub Secrets

All API keys and email credentials must be added as GitHub Secrets to your repository.
Use the GitHub CLI (`gh`) for convenience:

```bash
# OpenRouter API Key (e.g., sk-or-v1-...)
gh secret set OPENROUTER_API_KEY --body "your-openrouter-key-here"

# Google AI Studio API Key (e.g., AIzaSy...)
gh secret set AI_STUDIO_API_KEY --body "your-ai-studio-key-here"

# Your Gmail address (sender)
gh secret set SMTP_EMAIL --body "your.email@gmail.com"

# Your Gmail App Password (NOT your regular Gmail password!)
# Generate one here: https://myaccount.google.com/apppasswords
gh secret set SMTP_PASSWORD --body "your-app-password-here"

# Recipient email address for the summaries
gh secret set RECIPIENT_EMAIL --body "recipient@example.com"
```
**Important for Gemini:** Ensure your Google AI Studio project has a billing account linked to avoid strict rate limits (as `gemini-3-pro-preview` may not be included in the free tier by default).

### 2. GitHub Pages Configuration

For the HTML report to be accessible via GitHub Pages:
1.  Go to your repository on GitHub.com.
2.  Navigate to **Settings > Pages**.
3.  Under **Build and deployment**, select **Deploy from a branch**.
4.  Under **Branch**, select `gh-pages` and `/ (root)`.
5.  Click **Save**.

The page will be live at `https://[YOUR_USERNAME].github.io/daily-macro-summary/` (e.g., `https://jpeirce.github.io/daily-macro-summary/`).

## Configuration

The `scripts/fetch_and_summarize.py` script respects the following environment variables:

*   `SUMMARIZE_PROVIDER`: Controls which LLM provider(s) to use for summarization.
    *   `ALL` (default): Runs both OpenRouter and Gemini.
    *   `OPENROUTER`: Runs only OpenRouter.
    *   `GEMINI`: Runs only Gemini.
    *   `NONE`: Skips all LLM summarization.
*   `GITHUB_REPOSITORY`: Automatically detected in GitHub Actions (e.g., `owner/repo`). Used to construct the GitHub Pages URL.

## Running Locally (for testing)

1.  Clone the repository: `git clone https://github.com/jpeirce/daily-macro-summary.git`
2.  Navigate into the directory: `cd daily-macro-summary`
3.  Install dependencies: `pip install -r requirements.txt`
4.  Set environment variables (replace with your actual keys and emails):
    ```bash
    # Linux/macOS
    export OPENROUTER_API_KEY="your_openrouter_key"
    export AI_STUDIO_API_KEY="your_ai_studio_key"
    export SMTP_EMAIL="your.email@gmail.com"
    export SMTP_PASSWORD="your_app_password"
    export RECIPIENT_EMAIL="recipient@example.com"
    export SUMMARIZE_PROVIDER="ALL" # Or GEMINI, OPENROUTER, NONE
    export GITHUB_REPOSITORY="your_username/daily-macro-summary"

    # Windows (PowerShell)
    $env:OPENROUTER_API_KEY="your_openrouter_key"
    $env:AI_STUDIO_API_KEY="your_ai_studio_key"
    $env:SMTP_EMAIL="your.email@gmail.com"
    $env:SMTP_PASSWORD="your_app_password"
    $env:RECIPIENT_EMAIL="recipient@example.com"
    $env:SUMMARIZE_PROVIDER="ALL"
    $env:GITHUB_REPOSITORY="your_username/daily-macro-summary"
    ```
5.  Run the script: `python scripts/fetch_and_summarize.py`

## GitHub Actions Automation

The workflow is configured to run daily:
*   **Schedule:** Monday-Friday at 15:00 UTC (8:00 AM Mountain Time).
*   **Workflow File:** `.github/workflows/summary.yml`
*   **Prompt:** The detailed LLM prompt is embedded directly in `scripts/fetch_and_summarize.py` as `SYSTEM_PROMPT`.

## Contributing

Feel free to open issues or pull requests for improvements!