import os

# API Keys and Environment Variables
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AI_STUDIO_API_KEY = os.getenv("AI_STUDIO_API_KEY")
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SUMMARIZE_PROVIDER = os.getenv("SUMMARIZE_PROVIDER", "ALL").upper() 
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "jpeirce/daily-macro-summary") 
RUN_MODE = os.getenv("RUN_MODE", "PRODUCTION") # Options: PRODUCTION, BENCHMARK, BENCHMARK_JSON

# Model Configuration
OPENROUTER_MODEL = "openai/gpt-5.2" 
GEMINI_MODEL = "gemini-3-pro-preview" 

# Data Sources
PDF_SOURCES = {
    "wisdomtree": "https://www.wisdomtree.com/investments/-/media/us-media-files/documents/resource-library/daily-dashboard.pdf",
    "cme_sec01": "https://www.cmegroup.com/daily_bulletin/current/Section01_Exchange_Overall_Volume_And_Open_Interest.pdf",
    "cme_sec09": "https://www.cmegroup.com/daily_bulletin/current/Section09_Interest_Rate_Futures.pdf",
    "cme_sec11": "https://www.cmegroup.com/daily_bulletin/current/Section11_Equity_And_Index_Futures.pdf"
}

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
