import os
import requests
import fitz  # PyMuPDF
import smtplib
import google.generativeai as genai
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

from config import (
    OPENROUTER_API_KEY, AI_STUDIO_API_KEY, SMTP_EMAIL, SMTP_PASSWORD, RECIPIENT_EMAIL,
    SUMMARIZE_PROVIDER, GITHUB_REPOSITORY, PDF_SOURCES, OPENROUTER_MODEL, GEMINI_MODEL,
    RUN_MODE, BENCHMARK_MODELS, NOISE_THRESHOLDS
)
from prompts import (
    EXTRACTION_PROMPT, EXTRACTION_PROMPT_SEC09, EXTRACTION_PROMPT_SEC11,
    BENCHMARK_DATA_SYSTEM_PROMPT, BENCHMARK_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
)
from report_renderer import generate_html, generate_benchmark_html

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
                if len(hist_spx) < 2:
                    print(f"Warning: Insufficient SPX data (only {len(hist_spx)} row) to skip partial bar.")
                    data['sp500_trend_status'] = "Unknown"
                    data['sp500_1mo_change_pct'] = None
                    data['sp500_trend_audit'] = "Insufficient data (single partial row)"
                    return data
                current_idx = -2
            else:
                current_idx = -1
            
            # Check staleness
            current_data_date = hist_spx.index[current_idx].date()
            days_lag = (today_date - current_data_date).days
            
            if days_lag > 7:
                print(f"Warning: SPX data is stale. Last available: {current_data_date} (Lag: {days_lag} days)")
                data['sp500_trend_status'] = "Unknown"
                data['sp500_1mo_change_pct'] = None
                data['sp500_trend_audit'] = f"Data Stale (Lag: {days_lag} days)"
                return data

            # We want strictly 21 trading days ago
            prior_idx = current_idx - 21
            required_len = abs(prior_idx)
            
            if len(hist_spx) >= required_len:
                current_close = hist_spx['Close'].iloc[current_idx]
                prior_close = hist_spx['Close'].iloc[prior_idx]
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

# --- Deterministic Scoring Logic ---

def determine_signal(futures_delta, options_delta, noise_threshold=50000):
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
    
    dom_ratio = opt_abs / max(fut_abs, 1)
    res["dominance_ratio"] = round(dom_ratio, 2)
    res["participation_label"] = "Expanding" if net_delta > 0 else "Contracting"
    
    if max(fut_abs, opt_abs) < noise_threshold:
        res.update({
            "signal_label": "Low Signal / Noise",
            "direction_allowed": False,
            "noise_filtered": True,
            "gate_reason": f"Max delta ({max(fut_abs, opt_abs)}) < Threshold ({noise_threshold})"
        })
        return res
    
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

    def d(val):
        if val is None: return "N/A"
        return f"{val:+}"

    bps_change = extracted_metrics.get('ust10y_change_bps')
    rates_text = f"Signal: {b(rt_sig.get('signal_label', 'Unknown'), rt_sig.get('gate_reason', ''))}"
    if bps_change is not None:
        rates_text += f" | 10Y Move: {bps_change:+.1f} bps (Live)"

    eq_deltas = f"[Fut: <span class=\"numeric\">{d(eq_sig.get('futures_oi_delta'))}</span> | Opt: <span class=\"numeric\">{d(eq_sig.get('options_oi_delta'))}</span>]"
    rt_deltas = f"[Fut: <span class=\"numeric\">{d(rt_sig.get('futures_oi_delta'))}</span> | Opt: <span class=\"numeric\">{d(rt_sig.get('options_oi_delta'))}</span>]"

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
    details = {}
    data = extracted_data or {}
    
    # LIQUIDITY
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

    # VALUATION
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

    # INFLATION
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

    # CREDIT
    try:
        hy_spread = data.get('hy_spread_current')
        if hy_spread is not None:
            if hy_spread < 3.0: stress_score = 2.0
            else: stress_score = 2.0 + ((hy_spread - 3.0) * 1.6)
            scores['Credit Stress'] = round(min(max(stress_score, 0), 10), 1)
            details['Credit Stress'] = f"Calculated (Spread {hy_spread}%)"
        else:
            scores['Credit Stress'] = 5.0
            details['Credit Stress'] = "Default (Missing Spread)"
    except Exception as e:
        print(f"Error calc Credit: {e}")
        scores['Credit Stress'] = 5.0
        details['Credit Stress'] = "Error (Defaulted)"

    # GROWTH
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

    # RISK
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

def process_cme_sec11(sec11_data):
    if not sec11_data: return {}
    raw_products = sec11_data.get("products", {})
    processed = {}
    total_volume = 0
    total_oi = 0
    total_oi_change = 0
    for key, p_data in raw_products.items():
        if not p_data: continue
        vol = parse_int_token(p_data.get("total_volume")) or 0
        oi = parse_int_token(p_data.get("open_interest")) or 0
        oi_chg = parse_int_token(p_data.get("oi_change")) or 0
        processed[key] = {
            "label": p_data.get("row_label", "Unknown").split(" TOTAL")[0].strip(),
            "volume": vol,
            "oi": oi,
            "oi_change": oi_chg
        }
        total_volume += vol
        total_oi += oi
        total_oi_change += oi_chg
    return {
        "products": processed,
        "aggregates": {
            "total_volume": total_volume,
            "total_oi": total_oi,
            "total_oi_change": total_oi_change
        },
        "quality": {
            "notes": sec11_data.get("data_quality_notes", []),
            "is_preliminary": sec11_data.get("is_preliminary", False),
            "bulletin_date": sec11_data.get("bulletin_date")
        }
    }

def process_cme_sec09(raw_data):
    if not raw_data or "cme_section09" not in raw_data:
        return {}
    sec09 = raw_data["cme_section09"]
    totals = sec09.get("totals", {})
    notes = sec09.get("data_quality_notes", [])
    processed_tenors = {}
    missing_tenors = []
    tenor_keys = ["2y", "3y", "5y", "10y", "tn", "30y", "ultra"]
    for k in tenor_keys:
        if k not in totals:
            missing_tenors.append(k)
            continue
        row = totals[k]
        rth = parse_int_token(row.get("rth_volume")) or 0
        globex = parse_int_token(row.get("globex_volume")) or 0
        oi = parse_int_token(row.get("open_interest"))
        change = parse_int_token(row.get("oi_change")) or 0
        processed_tenors[k] = {
            "total_volume": rth + globex,
            "open_interest": oi,
            "oi_change": change
        }
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
    active_cluster = max(cluster_stats, key=lambda k: cluster_stats[k]["abs_oi_change"]) if cluster_stats else "N/A"
    active_tenor = max(processed_tenors, key=lambda k: abs(processed_tenors[k]["oi_change"])) if processed_tenors else "N/A"
    short_abs = cluster_stats.get("Short End", {}).get("abs_oi_change", 0)
    long_abs = cluster_stats.get("Long End", {}).get("abs_oi_change", 0)
    regime = "Mixed"
    if long_abs > short_abs and long_abs > 0: regime = "Long-end dominant"
    elif short_abs > long_abs and short_abs > 0: regime = "Front-end dominant"
    total_abs_delta = sum(abs(t["oi_change"]) for t in processed_tenors.values())
    top2_abs = sum(sorted([abs(t["oi_change"]) for t in processed_tenors.values()], reverse=True)[:2])
    concentration = (top2_abs / total_abs_delta) if total_abs_delta > 0 else 0.0
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

def summarize_openrouter(pdf_paths, ground_truth, event_context, model_override=None):
    target_model = model_override if model_override else OPENROUTER_MODEL
    print(f"Summarizing with OpenRouter ({target_model})...")
    if not OPENROUTER_API_KEY: return "Error: Key missing"
    
    images = []
    if RUN_MODE != "BENCHMARK_JSON":
        if "wisdomtree" in pdf_paths:
            images.extend(pdf_to_images(pdf_paths["wisdomtree"]))
        if "cme_sec01" in pdf_paths:
            cme_images = pdf_to_images(pdf_paths["cme_sec01"])
            images.extend(cme_images[:1])
        if "cme_sec09" in pdf_paths:
            sec09_images = pdf_to_images(pdf_paths["cme_sec09"])
            images.extend(sec09_images[:1])
        if "cme_sec11" in pdf_paths:
            sec11_images = pdf_to_images(pdf_paths["cme_sec11"])
            images.extend(sec11_images[:1])
    
    if RUN_MODE == "BENCHMARK":
        formatted_prompt = BENCHMARK_SYSTEM_PROMPT + f"\n\nEvent Context:\n{json.dumps(event_context, indent=2)}"
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
        formatted_prompt = BENCHMARK_SYSTEM_PROMPT + f"\n\nEvent Context:\n{json.dumps(event_context, indent=2)}"
    elif RUN_MODE == "BENCHMARK_JSON":
        formatted_prompt = BENCHMARK_DATA_SYSTEM_PROMPT + f"\n\nGround Truth Data:\n{json.dumps(ground_truth, indent=2)}\n\nEvent Context:\n{json.dumps(event_context, indent=2)}"
    else:
        formatted_prompt = SUMMARY_SYSTEM_PROMPT.format(
            ground_truth_json=json.dumps(ground_truth, indent=2),
            event_context_json=json.dumps(event_context, indent=2)
        )
    
    content = [formatted_prompt]
    
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
    
    # Pass 1: Adjectives
    adj_pattern = re.compile(r"\b(institutional)\b", re.IGNORECASE)
    if adj_pattern.search(text):
        print("Warning: Banned adjective found. Normalizing...")
        text = adj_pattern.sub("market-participant", text)
        if "Language normalization applied" not in text:
            text += "\n\n*(Note: Language normalization applied to remove attribution)*"

    # Pass 2: Nouns
    noun_pattern = re.compile(r"\b(smart money|whales?|insiders?|institutions?|big players?|professionals?|strong hands?|hedge funds?|asset managers?|dealers?|banks?|allocators?|funds?|big money|real money|pensions?|pension funds?|sovereign|sovereign wealth|macro funds?|levered funds?|CTAs)\b", re.IGNORECASE)
    if noun_pattern.search(text):
        print("Warning: Banned noun found. Normalizing...")
        text = noun_pattern.sub("market participants", text)
        if "Language normalization applied" not in text:
            text += "\n\n*(Note: Language normalization applied to remove attribution)*"
    
    # Normalize Signal Vocabulary
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
        
        # Expanded Directional Vocabulary
        leakage_pattern = re.compile(r"\b(bullish|bearish|conviction|aggressive|rally|selloff|breakout|risk[- ]on|risk[- ]off|bull steepener|bear steepener|short covering|long liquidation|new longs|new shorts|breakdown|melt[- ]up|buying the dip|selling the rip|upside bias|downside bias|tilted? bullish|tilted? bearish|skewed? bullish|skewed? bearish|upside skew|downside skew|risk[- ]on skew|risk[- ]off skew|bull bias|bear bias)\b", re.IGNORECASE)
        
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
        
        text = text.replace("participants flows", "participant flows")
        
        if filter_applied and "Note: Automatic direction filter applied" not in text:
            text += "\n\n*(Note: Automatic direction filter applied to non-directional signal sections)*"

    # Pass 4: Scoreboard Justification Validator
    lines = text.split('\n')
    in_scoreboard = False
    new_lines_pass4 = []
    
    # Constraints Mapping
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
            if len(parts) >= 4:
                dial_name = parts[1]
                justification = parts[3].lower()
                
                forbidden_found = False
                for dial_key, forbidden_list in sb_constraints.items():
                    if dial_key in dial_name:
                        for word in forbidden_list:
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

    # Markdown Hardening
    if text.count("**") % 2 != 0:
        text = text.replace("**", "")

    return text.strip()

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
    sec11_raw = {}
    algo_scores = {}
    
    if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
        # 1. Main Extraction (WisdomTree + CME Vol)
        main_pdfs = {k: v for k, v in pdf_paths.items() if k in ['wisdomtree', 'cme_sec01']}
        extracted_metrics = extract_metrics_gemini(main_pdfs)
        
        # 2. Section 09 Extraction (CME Rates Curve)
        sec09_pdf = {k: v for k, v in pdf_paths.items() if k == 'cme_sec09'}
        if sec09_pdf:
            print("Extracting CME Section 09 (Rates Curve)...")
            sec09_raw = extract_metrics_gemini(sec09_pdf, prompt_override=EXTRACTION_PROMPT_SEC09)

        # 3. Section 11 Extraction (Equity Index)
        sec11_pdf = {k: v for k, v in pdf_paths.items() if k == 'cme_sec11'}
        if sec11_pdf:
            print("Extracting CME Section 11 (Equity Index)...")
            sec11_raw = extract_metrics_gemini(sec11_pdf, prompt_override=EXTRACTION_PROMPT_SEC11)
    
    # Process Curve Data
    cme_rates_curve = process_cme_sec09(sec09_raw)
    cme_equity_flows = process_cme_sec11(sec11_raw)
    
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
        "cme_rates_curve": cme_rates_curve,
        "cme_equity_flows": cme_equity_flows
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
            summary_or = clean_llm_output(summary_or, ground_truth_context.get('cme_signals'))

        if SUMMARIZE_PROVIDER in ["ALL", "GEMINI"]:
            summary_gemini = summarize_gemini(pdf_paths, ground_truth_context, event_context)
            summary_gemini = clean_llm_output(summary_gemini, ground_truth_context.get('cme_signals'))
        
        # Save & Report
        os.makedirs("summaries", exist_ok=True)
        generate_html(today, summary_or, summary_gemini, algo_scores, score_details, extracted_metrics, ground_truth_context.get('cme_signals'), verification_block, event_context, cme_rates_curve, cme_equity_flows)
        
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