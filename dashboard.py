import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import time
import concurrent.futures
import pytz
import os

# --- Page Configuration ---
st.set_page_config(page_title="Ghost Inspector Suite Monitor", layout="wide")

# --- Styling Helper ---
def color_status(val):
    """
    Pandas Styler function to color the background of the Status cell.
    """
    color = ''
    if val == 'PASSING':
        color = '#d4edda' # Light Green
    elif val == 'FAILING':
        color = '#f8d7da' # Light Red
    elif val == 'UNKNOWN':
        color = '#fff3cd' # Light Yellow
    elif 'RUNNING' in val:
        color = '#cce5ff' # Light Blue
    elif val == 'EMPTY':
        color = '#e2e3e5' # Light Gray
    
    return f'background-color: {color}; color: black; border-radius: 4px;'

# --- API Functions ---

def get_folder_name(api_key, folder_id):
    """Fetch the specific folder details to get its name."""
    url = f"https://api.ghostinspector.com/v1/folders/{folder_id}/"
    params = {'apiKey': api_key}
    try:
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        data = r.json().get('data', {})
        return data.get('name', f"Folder {folder_id}")
    except:
        return f"Folder {folder_id}"

def get_suites_in_folder(api_key, folder_id):
    """Fetch all suites within a specific folder."""
    url = f"https://api.ghostinspector.com/v1/folders/{folder_id}/suites/"
    params = {'apiKey': api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get('data', [])
    except Exception as e:
        print(f"Error fetching Suites for Folder {folder_id}: {e}")
        return []

def get_tests_in_suite(api_key, suite_id):
    """Fetch all tests within a specific suite."""
    url = f"https://api.ghostinspector.com/v1/suites/{suite_id}/tests/"
    params = {'apiKey': api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get('data', [])
    except Exception as e:
        print(f"Error fetching Tests for Suite {suite_id}: {e}")
        return []

def check_suite_running_via_badge(suite_id):
    """
    Checks if a suite is running by hitting the status-badge endpoint.
    """
    url = f"https://api.ghostinspector.com/v1/suites/{suite_id}/status-badge"
    try:
        r = requests.get(url, timeout=3)
        if "running" in r.url.lower():
            return True
        if "running" in r.text.lower():
            return True
        return False
    except:
        return False

def fetch_suite_data(suite, api_key):
    """
    Helper function to fetch and process data for a SINGLE suite.
    This runs in a background thread, so it MUST NOT call st.* functions.
    """
    suite_id = suite['_id']
    suite_name = suite['name']
    
    # 1. Check Suite Status
    is_suite_running = check_suite_running_via_badge(suite_id)
    
    # 2. Fetch Tests
    tests = get_tests_in_suite(api_key, suite_id)
    
    relevant_tests = [t for t in tests if not t.get('importOnly', False)]
    suite_rows = []
    
    # Metrics
    m_total = 0
    m_passing = 0
    m_failing = 0
    m_running = 0
    
    latest_suite_run = None
    
    # Calculate Suite Status Label
    if is_suite_running:
        suite_status_label = "RUNNING"
    elif not relevant_tests:
        suite_status_label = "EMPTY"
    else:
        if any(t.get('passing') is False for t in relevant_tests):
            suite_status_label = "FAILING"
        elif all(t.get('passing') is None for t in relevant_tests):
            suite_status_label = "UNKNOWN"
        else:
            suite_status_label = "PASSING"

    # Process Tests
    for test in tests:
        if test.get('importOnly', False):
            continue

        # Status Logic
        is_passing = test.get('passing', False)
        # Use fast property check instead of slow API call per test
        is_test_executing = test.get('executing', False)
        
        if is_test_executing:
            status = "RUNNING"
            m_running += 1
        elif is_suite_running and test.get('passing') is None:
            # If suite is running and no result, assume pending/running in suite context
            status = "RUNNING (Suite)"
            m_running += 1
        elif test.get('passing') is None:
            status = "UNKNOWN"
        else:
            status = "PASSING" if is_passing else "FAILING"
            if is_passing: m_passing += 1
            else: m_failing += 1

        m_total += 1
        
        # Date Logic (SGT)
        exec_time_str = test.get('dateExecutionFinished') or test.get('dateExecutionStarted')
        exec_date_obj = None
        
        if exec_time_str:
            ts = pd.to_datetime(exec_time_str)
            if ts.tz is None: ts = ts.tz_localize('UTC')
            exec_date_obj = ts.tz_convert('Asia/Singapore')
            
            if latest_suite_run is None or exec_date_obj > latest_suite_run:
                latest_suite_run = exec_date_obj
        
        suite_rows.append({
            "Test Name": test['name'],
            "Suite Name": suite_name, # Added for flat view
            "Status": status,
            "Last Run": exec_date_obj,
            "Link": f"https://app.ghostinspector.com/tests/{test['_id']}",
            "Test_ID": test['_id']
        })
    
    # --- Sort Tests within Suite ---
    test_priority = {
        "RUNNING": 0,
        "RUNNING (Suite)": 0,
        "FAILING": 1,
        "UNKNOWN": 2,
        "PASSING": 3
    }
    suite_rows.sort(key=lambda x: (test_priority.get(x['Status'], 4), x['Test Name']))

    return {
        "data": {
            "name": suite_name,
            "status_label": suite_status_label,
            "last_run": latest_suite_run,
            "rows": suite_rows
        },
        "metrics": {
            "total": m_total,
            "passing": m_passing,
            "failing": m_failing,
            "running": m_running
        }
    }

# --- Session State (Login Gate) ---

if 'api_key' not in st.session_state:
    st.session_state['api_key'] = None

if 'monitored_folder_ids' not in st.session_state:
    st.session_state['monitored_folder_ids'] = ''

if 'hidden_suite_ids' not in st.session_state:
    st.session_state['hidden_suite_ids'] = ''

# --- Main App Logic ---

# 1. LOGIN SCREEN
if not st.session_state['api_key']:
    st.title("Ghost Inspector Login")
    st.markdown("Please enter your API key to start the session.")
    
    with st.form("login_form"):
        user_input_key = st.text_input("API Key", type="password") 
        submitted = st.form_submit_button("Start Session")
        
        if submitted and user_input_key:
            st.session_state['api_key'] = user_input_key
            st.rerun()
        elif submitted and not user_input_key:
            st.error("API Key cannot be empty.")

# 2. DASHBOARD SCREEN
else:
    # --- Sidebar ---
    with st.sidebar:
        st.header("Configuration")
        
        # User inputs ID, but it isn't applied until button click
        folder_input = st.text_area(
            "Folder IDs (comma separated)", 
            value=st.session_state['monitored_folder_ids'],
            placeholder="e.g., 64b123, 64b456",
            height=100
        )
        
        hidden_suite_input = st.text_area(
            "Hidden Suite IDs (comma separated)",
            value=st.session_state['hidden_suite_ids'],
            placeholder="e.g., 55e7185cf538..., 64b123...",
            height=100
        )
        
        # View Mode now part of Configuration
        view_mode = st.radio("View Mode", ["Group by Suite", "Group by Status"])
        
        if st.button("Apply Config & Load"):
            st.session_state['monitored_folder_ids'] = folder_input
            st.session_state['hidden_suite_ids'] = hidden_suite_input
            st.rerun()
        
        st.divider()
        auto_refresh = st.checkbox("Enable Auto-Refresh (60s)", value=False)
        # Note: We implement the delay manually inside the fragment now
        
        if st.button("Manual Refresh"):
            st.rerun()
            
        st.divider()
        if st.button("Logout"):
            st.session_state['api_key'] = None
            st.session_state['monitored_folder_ids'] = ''
            st.session_state['hidden_suite_ids'] = ''
            st.rerun()

    st.title("Suite & Test Monitor")

    # --- NOTIFICATION BOARD ---
    if 'remarks_data' not in st.session_state:
        # Check if CSV exists to load persisted remarks
        if os.path.exists('remarks.csv'):
            try:
                st.session_state['remarks_data'] = pd.read_csv('remarks.csv')
            except Exception:
                st.session_state['remarks_data'] = pd.DataFrame(columns=['Timestamp', 'Author', 'Remark'])
        else:
            st.session_state['remarks_data'] = pd.DataFrame(columns=['Timestamp', 'Author', 'Remark'])

    with st.expander("📝 Notification Board / Remarks", expanded=True):
        # Input Form
        with st.form("new_remark", clear_on_submit=True):
            c1, c2 = st.columns([1, 4])
            author = c1.text_input("Author", value="QA Team")
            remark = c2.text_input("New Remark", placeholder="e.g. Investigating failures in Suite A...")
            submitted = st.form_submit_button("Post")
            
            if submitted and remark:
                # Use SGT for timestamps
                sgt_zone = pytz.timezone('Asia/Singapore')
                timestamp = datetime.now().astimezone(sgt_zone).strftime('%Y-%m-%d %H:%M:%S')
                
                new_row = pd.DataFrame([{
                    'Timestamp': timestamp,
                    'Author': author,
                    'Remark': remark
                }])
                st.session_state['remarks_data'] = pd.concat([new_row, st.session_state['remarks_data']], ignore_index=True)
                # Persist to local file
                st.session_state['remarks_data'].to_csv('remarks.csv', index=False)
                st.rerun()

        # Display Board
        if not st.session_state['remarks_data'].empty:
            st.dataframe(
                st.session_state['remarks_data'],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Timestamp": st.column_config.TextColumn("Time (SGT)", width="medium"),
                    "Author": st.column_config.TextColumn("Author", width="small"),
                    "Remark": st.column_config.TextColumn("Message", width="large"),
                }
            )
        else:
            st.info("No remarks posted yet.")
    
    st.divider()
    
    # Parse IDs
    raw_ids = st.session_state['monitored_folder_ids']
    folder_ids = [fid.strip() for fid in raw_ids.split(',') if fid.strip()]
    
    raw_hidden_ids = st.session_state['hidden_suite_ids']
    hidden_suite_ids_list = [sid.strip() for sid in raw_hidden_ids.split(',') if sid.strip()]

    if not folder_ids:
        st.info("Please enter one or more **Folder IDs** (comma separated) in the sidebar and click **Apply Config & Load**.")
    else:
        folder_names = []
        with st.spinner("Resolving folder names..."):
            for fid in folder_ids:
                name = get_folder_name(st.session_state['api_key'], fid)
                folder_names.append(name)

        tabs = st.tabs(folder_names)

        for t_index, folder_id in enumerate(folder_ids):
            with tabs[t_index]:
                # Dynamic fragment refresh - Manual Timer Implementation
                # We do NOT use run_every here to ensure we wait AFTER execution
                @st.fragment
                def render_tab_content(fid, fname, current_tab_idx, current_view_mode, hidden_suites):
                    
                    st.subheader(f"📂 {fname}")
                    c1, c2 = st.columns([3, 1])
                    
                    sgt_zone = pytz.timezone('Asia/Singapore')
                    now_sgt = datetime.now().astimezone(sgt_zone)
                    c1.caption(f"ID: {fid} | Updated: {now_sgt.strftime('%H:%M:%S')} SGT")
                    
                    # Only show filter in Suite mode
                    suite_filter = ""
                    if current_view_mode == "Group by Suite":
                        suite_filter = st.text_input("Filter Suites", key=f"filter_{fid}_{current_tab_idx}", placeholder="Type to search suites...")

                    # --- Data Fetching ---
                    processed_suites = [] 
                    all_tests_flat = [] # Used for Status View
                    
                    metric_total = 0
                    metric_passing = 0
                    metric_failing = 0
                    metric_running = 0

                    # Note: st.spinner here will show during the fetch
                    with st.spinner(f'Fetching data for {fname}...'):
                        suites = get_suites_in_folder(st.session_state['api_key'], fid)

                    if not suites:
                        st.warning(f"No suites found in folder {fid} (or ID is incorrect).")
                    else:
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        
                        # --- FILTER HIDDEN SUITES ---
                        # Filter out suites that are in the hidden list BEFORE processing
                        active_suites = [s for s in suites if s['_id'] not in hidden_suites]
                        
                        if not active_suites:
                            st.warning("All suites in this folder are hidden or none exist.")
                        else:
                            # PARALLEL FETCHING
                            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                                future_to_suite = {
                                    executor.submit(fetch_suite_data, s, st.session_state['api_key']): s 
                                    for s in active_suites
                                }
                                
                                completed_count = 0
                                for future in concurrent.futures.as_completed(future_to_suite):
                                    try:
                                        result = future.result()
                                        
                                        # Collect Data
                                        processed_suites.append(result['data'])
                                        all_tests_flat.extend(result['data']['rows'])
                                        
                                        # Aggregate Metrics
                                        m = result['metrics']
                                        metric_total += m['total']
                                        metric_passing += m['passing']
                                        metric_failing += m['failing']
                                        metric_running += m['running']
                                        
                                    except Exception as exc:
                                        print(f"Suite processing error: {exc}")
                                    
                                    completed_count += 1
                                    progress_bar.progress(completed_count / len(active_suites))
                                    status_text.text(f"Processed {completed_count}/{len(active_suites)} suites...")
                            
                            status_text.empty()
                            progress_bar.empty()

                            # --- RENDER METRICS ---
                            m1, m2, m3, m4 = st.columns(4)
                            m1.metric("Total Tests", metric_total)
                            m2.metric("Passing", metric_passing)
                            m3.metric("Failing", metric_failing, delta_color="inverse")
                            m4.metric("Running", metric_running)
                            
                            st.divider()

                            # ==========================================
                            # VIEW 1: GROUP BY SUITE (Default)
                            # ==========================================
                            if current_view_mode == "Group by Suite":
                                # Sort Logic
                                status_priority = { "RUNNING": 0, "RUNNING (Suite)": 0, "FAILING": 1, "UNKNOWN": 2, "PASSING": 3, "EMPTY": 4 }
                                processed_suites.sort(key=lambda x: status_priority.get(x['status_label'], 5))

                                has_rendered_any = False
                                
                                for p_suite in processed_suites:
                                    if suite_filter and suite_filter.lower() not in p_suite['name'].lower():
                                        continue
                                        
                                    has_rendered_any = True
                                    default_expanded = p_suite['status_label'] in ['FAILING', 'RUNNING']
                                    
                                    date_str = ""
                                    if p_suite['last_run']:
                                        date_str = f"— Last Run: {p_suite['last_run'].strftime('%d %b %H:%M')}"
                                    
                                    expander_label = f"[{p_suite['status_label']}] {p_suite['name']} ({len(p_suite['rows'])} tests) {date_str}"
                                    
                                    with st.expander(expander_label, expanded=default_expanded):
                                        if p_suite['rows']:
                                            df = pd.DataFrame(p_suite['rows'])
                                            styled_df = df.style.map(color_status, subset=['Status'])
                                            
                                            st.dataframe(
                                                styled_df,
                                                use_container_width=True,
                                                hide_index=True,
                                                column_config={
                                                    "Link": st.column_config.LinkColumn("Action", display_text="View Result"),
                                                    "Last Run": st.column_config.DatetimeColumn("Last Execution", format="D MMM YYYY, h:mm a"),
                                                    "Status": st.column_config.TextColumn("State", width="small"),
                                                    "Test Name": st.column_config.TextColumn("Test Name", width="large"),
                                                    "Suite Name": None, # Hide suite name in suite view
                                                    "Test_ID": None
                                                }
                                            )
                                        else:
                                            st.caption("No active tests found in this suite.")

                                if not has_rendered_any and suite_filter:
                                    st.info(f"No suites match the filter '{suite_filter}'")

                            # ==========================================
                            # VIEW 2: GROUP BY STATUS (Consolidated)
                            # ==========================================
                            else:
                                if not all_tests_flat:
                                    st.info("No tests found to display.")
                                else:
                                    df_all = pd.DataFrame(all_tests_flat)
                                    
                                    # Sort by Status Priority: RUNNING > FAILING > UNKNOWN > PASSING
                                    def get_status_priority(status):
                                        if "RUNNING" in str(status): return 0
                                        if status == "FAILING": return 1
                                        if status == "UNKNOWN": return 2
                                        if status == "PASSING": return 3
                                        return 4
                                    
                                    df_all['priority'] = df_all['Status'].apply(get_status_priority)
                                    df_all = df_all.sort_values(by=['priority', 'Test Name'])
                                    
                                    # Render single consolidated dataframe
                                    with st.expander(f"All Tests ({len(df_all)})", expanded=True):
                                        styled_df = df_all.style.map(color_status, subset=['Status'])
                                        
                                        st.dataframe(
                                            styled_df,
                                            use_container_width=True,
                                            hide_index=True,
                                            column_config={
                                                "Link": st.column_config.LinkColumn("Action", display_text="View Result"),
                                                "Last Run": st.column_config.DatetimeColumn("Last Execution", format="D MMM YYYY, h:mm a"),
                                                "Status": st.column_config.TextColumn("State", width="small"),
                                                "Suite Name": st.column_config.TextColumn("Suite", width="medium"),
                                                "Test Name": st.column_config.TextColumn("Test Name", width="large"),
                                                "Test_ID": None,
                                                "priority": None
                                            }
                                        )
                                    
                    # --- AUTO REFRESH LOGIC (MANUAL DELAY) ---
                    # This logic runs strictly AFTER the content has finished loading and rendering
                    if auto_refresh:
                        time.sleep(60)
                        st.rerun()

                # Call the fragment function with current view mode
                render_tab_content(folder_id, folder_names[t_index], t_index, view_mode, hidden_suite_ids_list)