import os
import json
import re
import markdown
from datetime import datetime
from config import PDF_SOURCES, GEMINI_MODEL, OPENROUTER_MODEL

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
        ("DXY", fmt_num(kn.get('dxy_current')), "US Dollar Index (Strength vs Basket)"),
        ("WTI Crude", f"${fmt_num(kn.get('wti_current'))}", "Oil Price (Energy Cost Proxy)"),
        ("HYG", f"${fmt_num(kn.get('hyg_current'))}", "High Yield Bond ETF (Liquidity Proxy)"),
        ("VIX", fmt_num(kn.get('vix_index')), "Market Volatility Index (Fear Gauge)"),
        ("CME Vol", fmt_num(kn.get('cme_total_volume')), "Total Volume across CME Exchange")
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

def render_equity_flows_panel(equity_data):
    if not equity_data or not equity_data.get("products"): return ""
    
    products = equity_data.get("products", {})
    
    # Define display order
    display_order = [
        ("es", "S&P 500", "#2c3e50"), 
        ("nq", "NASDAQ", "#8e44ad"), 
        ("ym", "DOW", "#2980b9"), 
        ("mid", "MID 400", "#7f8c8d"), 
        ("sml", "SML 600", "#7f8c8d")
    ]
    
    rows = ""
    for key, label, color in display_order:
        p = products.get(key)
        if not p: continue
        
        # Color OI Change
        oi_chg = p.get("oi_change", 0)
        oi_color = "#27ae60" if oi_chg > 0 else "#e74c3c" if oi_chg < 0 else "#7f8c8d"
        
        rows += f"""
        <div class="equity-row" style="display: flex; justify-content: space-between; padding: 6px 0; font-size: 0.9em;">
            <div style="font-weight: 600; color: {color};">{label}</div>
            <div style="display: flex; gap: 15px;">
                <span class="vol-label" title="Total Volume">Vol: {fmt_num(p.get('volume'))}</span>
                <span title="Open Interest Change" style="font-weight: bold; color: {oi_color}; min-width: 60px; text-align: right;">{fmt_delta(oi_chg)}</span>
            </div>
        </div>
        """
        
    return f"""
    <div class="rates-curve-panel">
        <div class="curve-header">
            <strong>US Equity Index Flows (CME)</strong>
        </div>
        {rows}
        <div style="margin-top: 8px; font-size: 0.8em; color: #999; text-align: right; font-style: italic;">
            Source: Daily Bulletin Sec. 11
        </div>
    </div>
    """

def render_algo_box(scores, details, cme_signals):
    # Scoreboard
    score_html = "<div class='score-grid'>"
    
    # Enforce consistent display order matching the LLM prompt
    score_order = [
        "Growth Impulse",
        "Inflation Pressure",
        "Liquidity Conditions",
        "Credit Stress",
        "Valuation Risk",
        "Risk Appetite"
    ]
    
    for k in score_order:
        v = scores.get(k, 0.0)
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
        sig_html = "<div class='score-grid' style='margin-top: 20px; border-top: 2px dashed #eee; padding-top: 20px; border-left: none;'>"
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

def inject_score_deltas(html_content, ground_truth_scores):
    if not ground_truth_scores: return html_content
    
    # Dial keys must match extracted metrics keys
    dials = [
        "Growth Impulse", "Inflation Pressure", "Liquidity Conditions", 
        "Credit Stress", "Valuation Risk", "Risk Appetite"
    ]
    
    # Regex to capture the Dial Name cell and the Score cell
    # Markdown tables render as <tr><td>Dial</td><td>Score</td>...</tr>
    # We use a pattern that matches the first two columns.
    pattern = r"(<td>\s*(" + "|".join(dials) + r")\s*</td>\s*<td>\s*)([^<]+)(\s*</td>)"
    
    def replacer(match):
        prefix = match.group(1)
        dial_name = match.group(2)
        score_text = match.group(3)
        suffix = match.group(4)
        
        try:
            # Extract first float
            nums = re.findall(r"[\d\.]+", score_text)
            if not nums: return match.group(0)
            
            llm_score = float(nums[0])
            gt_score = ground_truth_scores.get(dial_name)
            
            if gt_score is not None:
                delta = llm_score - gt_score
                # Color logic: Red if diff > 2, Orange if > 1, Gray otherwise
                color = "gray"
                if abs(delta) >= 2.0: color = "red"
                elif abs(delta) >= 1.0: color = "orange"
                
                # Badge styling matching the rest of the report
                badge = f' <span class="badge badge-{color}" style="font-size:0.7em; vertical-align: middle;" title="Diff from Ground Truth ({gt_score})">{delta:+.1f}</span>'
                return f"{prefix}{score_text}{badge}{suffix}"
        except Exception:
            pass
            
        return match.group(0)

    return re.sub(pattern, replacer, html_content, flags=re.IGNORECASE)

def generate_benchmark_html(today, summaries, ground_truth=None, event_context=None, filename="benchmark.html"):
    print(f"Generating Benchmark HTML report ({filename})...")
    
    # Extract Context
    extracted_metrics = ground_truth.get('extracted_metrics', {}) if ground_truth else {}
    cme_signals = ground_truth.get('cme_signals', {}) if ground_truth else {}
    rates_curve = ground_truth.get('cme_rates_curve', {}) if ground_truth else {}
    equity_flows = ground_truth.get('cme_equity_flows', {}) if ground_truth else {}
    scores = ground_truth.get('calculated_scores', {}) if ground_truth else {}
    score_details = ground_truth.get('score_details', {}) if ground_truth else {}

    # Badges Logic
    generated_time = datetime.now().strftime('%Y-%m-%d %H:%M UTC')
    wt_date = extracted_metrics.get('wisdomtree_as_of_date', 'Unknown')
    cme_date = extracted_metrics.get('cme_bulletin_date', 'Unknown')
    mode_label = 'JSON (extracted by Gemini)' if 'data' in filename else 'Visual (PDFs)'

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
    
    # Render Visual Panels
    rates_html = render_rates_curve_panel(rates_curve)
    equity_flows_html = render_equity_flows_panel(equity_flows)
    
    # Render Algo Box (Ground Truth)
    algo_html = render_algo_box(scores, score_details, cme_signals)

    options = ""
    divs = ""
    
    # Sort models: Gemini Native first, then others
    sorted_models = [GEMINI_MODEL] + [m for m in summaries.keys() if m != GEMINI_MODEL]
    
    for i, model in enumerate(sorted_models):
        content = summaries.get(model, "No content")
        html_content = markdown.markdown(content, extensions=['tables'])
        
        # Inject Score Deltas (LLM vs Ground Truth)
        html_content = inject_score_deltas(html_content, scores)
        
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
    .score-grid { display: flex; flex-wrap: wrap; gap: 0; margin-bottom: 20px; border: 1px solid #e1e4e8; border-radius: 8px; overflow: hidden; background: #fff; justify-content: center; }
    .score-card { flex: 1; min-width: 160px; background: white; padding: 20px; text-align: center; display: flex; flex-direction: column; justify-content: space-between; min-height: 110px; border-right: 1px solid #eee; }
    .score-card:last-child { border-right: none; }
    .score-label { font-size: 0.85em; color: #2c3e50; display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 3.2em; line-height: 1.2; margin-bottom: 10px; font-weight: 600; }
    .score-value { font-size: 1.8em; font-weight: bold; }
    /* Event Callout */
    .event-callout { background: #f4f6f8; border: 1px solid #d1d5da; border-radius: 6px; padding: 12px 20px; margin-bottom: 30px; display: flex; align-items: center; gap: 12px; font-size: 0.9em; color: #444; }
    .event-callout strong { color: #24292e; }
    /* Equity Flows */
    .equity-row { border-bottom: 1px solid #eee; }
    .vol-label { color: #555; }
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
        .controls, .model-content, .column, .algo-box, .score-grid > div, .footer, .key-numbers, .provenance-strip, .toc-sidebar, .signals-panel, .score-card, .rates-curve-panel, .signal-chip { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
        h1, h2, h3, strong { color: #c9d1d9 !important; }
        select { background: #0d1117; color: #c9d1d9; border-color: #30363d; }
        th { background-color: #21262d; color: #c9d1d9; border-color: #30363d; }
        td { color: #c9d1d9; border-color: #30363d; }
        a { color: #58a6ff; }
        .key-numbers, .provenance-strip, .rates-curve-panel, .algo-box, .score-card, .signal-chip { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
        .key-number-value, .score-value, .curve-value { color: #c9d1d9 !important; }
        .key-number-label, .score-label, .curve-label, .provenance-label { color: #8b949e !important; }
        .badge { filter: brightness(0.9); }
        .event-callout { background: #1c2128 !important; border-color: #444c56 !important; color: #c9d1d9 !important; }
        .active-tenor-row { background-color: rgba(56, 139, 253, 0.15) !important; border-left-color: #58a6ff !important; }
        .algo-box details div { background: #161b22 !important; color: #c9d1d9 !important; border-color: #30363d !important; }
        .badge-warning { background: #3e3725; color: #ffca2c; border-color: #534824; }
        .equity-row { border-color: #30363d !important; }
        .vol-label { color: #8b949e !important; }
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
    
    document.addEventListener("DOMContentLoaded", function() {
        const badges = document.querySelectorAll(".timestamp-badge");
        badges.forEach(b => {
            const utc = b.getAttribute("data-utc");
            if (utc) {
                // Ensure explicit UTC parsing
                const date = new Date(utc.replace(" UTC", "Z").replace(" ", "T"));
                b.textContent = "Generated: " + date.toLocaleString();
            }
        });
    });
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
        
        <div style="display: flex; justify-content: center; gap: 15px; margin-bottom: 20px;">
            <span class="badge badge-gray timestamp-badge" data-utc="{generated_time}">Generated: {generated_time}</span>
            <span class="badge badge-blue">Data as of: WT: {wt_date} / CME: {cme_date}</span>
        </div>
        
        <p style="text-align:center; color:#666; margin-top: -10px;">Mode: {mode_label}</p>
        
        {header_html}
        {rates_html}
        {equity_flows_html}
        
        <div class="controls">
            <label for="model-select"><strong>Select Model:</strong></label>
            <select id="model-select" onchange="showModel(this.value)">
                {options}
            </select>
        </div>
        
        {divs}
        
        {algo_html}
        
        <div class="footer">
            <div style="margin-bottom: 20px;">
                <a href="https://github.com/jpeirce/daily-macro-summary" style="color: #3498db; text-decoration: none; font-weight: bold;">View Source Code on GitHub</a>
            </div>
            <div style="margin-bottom: 20px; color: #7f8c8d; font-size: 0.85em; font-style: italic; line-height: 1.4; border-top: 1px solid #eee; padding-top: 20px;">
                This is an independently generated summary of the publicly available WisdomTree Daily Dashboard and CME Data. Not affiliated with, reviewed by, or approved by WisdomTree or CME Group. Third-party sources are not responsible for the accuracy of this summary. No warranties are made regarding completeness, accuracy, or timeliness; data may be delayed or incorrect.
                <br><strong>This content is for informational purposes only and is NOT financial advice.</strong> No fiduciary or advisor-client relationship is formed. This is not an offer or solicitation to buy or sell any security. Trading involves significant risk of loss.
                <br>Use at your own risk; the author disclaims liability for any losses or decisions made based on this content. Consult a qualified financial professional. Past performance is not indicative of future results. Automated extraction and AI analysis may contain errors or misinterpretations.
            </div>
            Generated on {generated_time}
        </div>
    </body>
    </html>
    """
    
    # Save to specific filename
    os.makedirs("summaries", exist_ok=True)
    with open(f"summaries/{filename}", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML report generated and saved to summaries/{filename}")

def generate_html(today, summary_or, summary_gemini, scores, details, extracted_metrics, cme_signals=None, verification_block="", event_context=None, rates_curve=None, equity_flows=None):
    print("Generating HTML report...")
    
    # Prepend Verification Block to the raw text BEFORE markdown conversion
    if verification_block:
        summary_or = verification_block + "\n\n" + summary_or
        summary_gemini = verification_block + "\n\n" + summary_gemini

    # Note: Summaries should be cleaned before passing here
    
    html_or = markdown.markdown(summary_or, extensions=['tables'])
    html_gemini = markdown.markdown(summary_gemini, extensions=['tables'])
    
    # Render Components using Helpers
    provenance_html = render_provenance_strip(extracted_metrics, cme_signals)
    kn_html = render_key_numbers(extracted_metrics)
    signals_panel_html = render_signals_panel(cme_signals)
    rates_curve_html = render_rates_curve_panel(rates_curve)
    equity_flows_html = render_equity_flows_panel(equity_flows)
    algo_box_html = render_algo_box(scores, details, cme_signals)
    event_callout_html = render_event_callout(event_context, rates_curve)

    # Build columns conditionally
    columns_html = ""
    if "Gemini summary skipped" not in summary_gemini:
        columns_html += f"""
            <div class="column">
                <h2>&#129302; Gemini ({GEMINI_MODEL})</h2>
                {signals_panel_html}
                {rates_curve_html}
                {equity_flows_html}
                {html_gemini}
            </div>
        """
    
    if "OpenRouter summary skipped" not in summary_or:
        columns_html += f"""
            <div class="column">
                <h2>&#129504; OpenRouter ({OPENROUTER_MODEL})</h2>
                {signals_panel_html}
                {rates_curve_html}
                {equity_flows_html}
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
    .score-grid { display: flex; flex-wrap: wrap; gap: 0; margin-bottom: 20px; border: 1px solid #e1e4e8; border-radius: 8px; overflow: hidden; background: #fff; justify-content: center; }
    .score-card { flex: 1; min-width: 160px; background: white; padding: 20px; text-align: center; display: flex; flex-direction: column; justify-content: space-between; min-height: 110px; border-right: 1px solid #eee; }
    .score-card:last-child { border-right: none; }
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
    /* Equity Flows */
    .equity-row { border-bottom: 1px solid #eee; }
    .vol-label { color: #555; }

    /* Native Dark Mode */
    @media (prefers-color-scheme: dark) {
        body { background: #0d1117; color: #c9d1d9; }
        .column, .algo-box, .score-grid > div, .footer, .key-numbers, .provenance-strip, .toc-sidebar, .signals-panel, .score-card, .rates-curve-panel, .signal-chip { background: #161b22 !important; border-color: #30363d !important; box-shadow: none !important; }
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
        .equity-row { border-color: #30363d !important; }
        .vol-label { color: #8b949e !important; }
    }
    """
    
    # We can add links to CME pdfs too if desired, but for now just Main
    main_pdf_url = PDF_SOURCES['wisdomtree']
    cme_bulletin_url = "https://www.cmegroup.com/market-data/daily-bulletin.html"
    
    # Extract provenance info
    cme_date_str = extracted_metrics.get('cme_bulletin_date', 'N/A')
    wt_date_str = extracted_metrics.get('wisdomtree_as_of_date', 'N/A')
    
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

    generated_time = datetime.now().strftime('%Y-%m-%d %H:%M UTC')

    script = """
    document.addEventListener("DOMContentLoaded", function() {
        const badges = document.querySelectorAll(".timestamp-badge");
        badges.forEach(b => {
            const utc = b.getAttribute("data-utc");
            if (utc) {
                const date = new Date(utc.replace(" UTC", "Z").replace(" ", "T"));
                b.textContent = "Generated: " + date.toLocaleString();
            }
        });
    });
    """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Macro Summary - {today}</title>
        <style>{css}</style>
        <script>{script}</script>
    </head>
    <body>
        <h1>Daily Macro Summary ({today})</h1>
        
        <div style="display: flex; justify-content: center; gap: 15px; margin-bottom: 20px;">
            <span class="badge badge-gray timestamp-badge" data-utc="{generated_time}">Generated: {generated_time}</span>
            <span class="badge badge-blue">Data as of: WT: {display_wt_date} / CME: {display_cme_date}</span>
        </div>
        
        {provenance_html}

        <div style="text-align: center; margin-bottom: 15px; color: #7f8c8d; font-size: 0.9em; font-style: italic;">
            Independently generated summary. Informational use only&mdash;NOT financial advice. Full disclaimers in footer.
        </div>
        <div class="pdf-link">
            <h3>Inputs</h3>
            <a href="{main_pdf_url}" target="_blank">📄 View WisdomTree PDF</a>
            &nbsp;&nbsp;
            <a href="{cme_bulletin_url}" target="_blank" style="background-color: #2c3e50;">📊 View CME Bulletin{cme_warning_flag}</a>
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

        {algo_box_html}

        {glossary_html}

        <div class="footer">
            <div style="margin-bottom: 20px;">
                <a href="https://github.com/jpeirce/daily-macro-summary" style="color: #3498db; text-decoration: none; font-weight: bold;">View Source Code on GitHub</a>
            </div>
            <div style="margin-bottom: 20px; color: #7f8c8d; font-size: 0.85em; font-style: italic; line-height: 1.4; border-top: 1px solid #eee; padding-top: 20px;">
                This is an independently generated summary of the publicly available WisdomTree Daily Dashboard and CME Data. Not affiliated with, reviewed by, or approved by WisdomTree or CME Group. Third-party sources are not responsible for the accuracy of this summary. No warranties are made regarding completeness, accuracy, or timeliness; data may be delayed or incorrect.
                <br><strong>This content is for informational purposes only and is NOT financial advice.</strong> No fiduciary or advisor-client relationship is formed. This is not an offer or solicitation to buy or sell any security. Trading involves significant risk of loss.
                <br>Use at your own risk; the author disclaims liability for any losses or decisions made based on this content. Consult a qualified financial professional. Past performance is not indicative of future results. Automated extraction and AI analysis may contain errors or misinterpretations.
            </div>
            Generated on {generated_time}
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
