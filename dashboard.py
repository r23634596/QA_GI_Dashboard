import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import time
import concurrent.futures

# --- Page Configuration ---
st.set_page_config(page_title="Ghost Inspector Suite Monitor", page_icon="👻", layout="wide")

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
    except requests.exceptions.RequestException as e:
        st.error(f"❌ Error fetching Suites for Folder {folder_id}: {e}")
        return []

def get_tests_in_suite(api_key, suite_id):
    """Fetch all tests within a specific suite."""
    url = f"https://api.ghostinspector.com/v1/suites/{suite_id}/tests/"
    params = {'apiKey': api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get('data', [])
    except requests.exceptions.RequestException as e:
        st.error(f"❌ Error fetching Tests for Suite {suite_id}: {e}")
        return []

def check_suite_running_via_badge(suite_id):
    """
    Checks if a suite is running by hitting the status-badge endpoint.
    Badges usually return an SVG or redirect to an image like 'running.svg'.
    """
    url = f"https://api.ghostinspector.com/v1/suites/{suite_id}/status-badge"
    try:
        # We don't strictly need the API key for public badges, but headers might help.
        # We check response content or final URL for 'running'.
        r = requests.get(url, timeout=3)
        
        # Check 1: Does the final URL contain 'running'?
        if "running" in r.url.lower():
            return True
            
        # Check 2: Does the SVG text content contain 'Running'?
        if "running" in r.text.lower():
            return True
            
        return False
    except:
        return False

def fetch_suite_data(suite, api_key):
    """
    Helper function to fetch and process data for a SINGLE suite.
    This is designed to run in parallel threads.
    """
    suite_id = suite['_id']
    suite_name = suite['name']
    
    # 1. Check Suite Status (Badge Check)
    is_suite_running = check_suite_running_via_badge(suite_id)
    
    # 2. Fetch Tests
    tests = get_tests_in_suite(api_key, suite_id)
    
    relevant_tests = [t for t in tests if not t.get('importOnly', False)]
    suite_rows = []
    
    # Metrics local to this suite
    m_total = 0
    m_passing = 0
    m_failing = 0
    m_running = 0
    
    # Calculate Suite Status Label
    if is_suite_running:
        suite_status_label = "RUNNING"
        suite_icon = "🏃"
    elif not relevant_tests:
        suite_status_label = "EMPTY"
        suite_icon = "⚪"
    else:
        if any(t.get('passing') is False for t in relevant_tests):
            suite_status_label = "FAILING"
            suite_icon = "❌"
        elif all(t.get('passing') is None for t in relevant_tests):
            suite_status_label = "UNKNOWN"
            suite_icon = "❓"
        else:
            suite_status_label = "PASSING"
            suite_icon = "✅"

    # Process Tests
    for test in tests:
        if test.get('importOnly', False):
            continue

        # Determine Test Status
        is_passing = test.get('passing', False)
        is_test_executing = test.get('executing', False)
        
        if is_test_executing:
            status = "RUNNING"
            m_running += 1
        elif is_suite_running and test.get('passing') is None:
            status = "RUNNING (Suite)"
            m_running += 1
        elif test.get('passing') is None:
            status = "UNKNOWN"
        else:
            status = "PASSING" if is_passing else "FAILING"
            if is_passing: m_passing += 1
            else: m_failing += 1

        m_total += 1
        exec_time = test.get('dateExecutionFinished') or test.get('dateExecutionStarted')
        
        suite_rows.append({
            "Test Name": test['name'],
            "Status": status,
            "Last Run": pd.to_datetime(exec_time) if exec_time else None,
            "Link": f"https://app.ghostinspector.com/tests/{test['_id']}",
            "Test_ID": test['_id']
        })
    
    return {
        "data": {
            "name": suite_name,
            "status_label": suite_status_label,
            "icon": suite_icon,
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

# --- Main App Logic ---

# 1. LOGIN SCREEN
if not st.session_state['api_key']:
    st.title("👻 Ghost Inspector Login")
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
        st.header("⚙️ Configuration")
        
        # User inputs ID, but it isn't applied until button click
        folder_input = st.text_area(
            "Folder IDs (comma separated)", 
            value=st.session_state['monitored_folder_ids'],
            placeholder="e.g., 64b123, 64b456",
            height=100
        )
        
        if st.button("Load Dashboard"):
            st.session_state['monitored_folder_ids'] = folder_input
            st.rerun()
        
        st.divider()
        # This checkbox now controls the refresh interval of the fragments
        auto_refresh = st.checkbox("Enable Auto-Refresh (60s)", value=False)
        refresh_interval = 60 if auto_refresh else None
        
        if st.button("Manual Refresh"):
            st.rerun()
            
        st.divider()
        if st.button("Logout"):
            st.session_state['api_key'] = None
            st.session_state['monitored_folder_ids'] = ''
            st.rerun()

    st.title("👻 Suite & Test Monitor")
    
    # Parse Folder IDs
    raw_ids = st.session_state['monitored_folder_ids']
    folder_ids = [fid.strip() for fid in raw_ids.split(',') if fid.strip()]

    if not folder_ids:
        st.info("👈 Please enter one or more **Folder IDs** (comma separated) in the sidebar and click **Load Dashboard**.")
    else:
        # Pre-fetch folder names for the tabs
        folder_names = []
        with st.spinner("Resolving folder names..."):
            for fid in folder_ids:
                name = get_folder_name(st.session_state['api_key'], fid)
                folder_names.append(name)

        # Create Tabs using the fetched Names
        tabs = st.tabs(folder_names)

        # Loop through each folder ID and its corresponding tab
        for t_index, folder_id in enumerate(folder_ids):
            with tabs[t_index]:
                # Each tab is an independent Fragment
                @st.fragment(run_every=refresh_interval)
                def render_tab_content(fid, fname, current_tab_idx):
                    
                    # --- Header & Filter ---
                    st.subheader(f"📂 {fname}")
                    c1, c2 = st.columns([3, 1])
                    c1.caption(f"ID: {fid} | Updated: {datetime.now().strftime('%H:%M:%S')}")
                    
                    # Filter Input
                    suite_filter = st.text_input("🔍 Filter Suites", key=f"filter_{fid}_{current_tab_idx}", placeholder="Type to search suites...")

                    # --- Data Fetching ---
                    processed_suites = [] 
                    
                    # Global Metrics for this folder
                    metric_total = 0
                    metric_passing = 0
                    metric_failing = 0
                    metric_running = 0

                    with st.spinner(f'Fetching data for {fname}...'):
                        suites = get_suites_in_folder(st.session_state['api_key'], fid)

                    if not suites:
                        st.warning(f"No suites found in folder {fid} (or ID is incorrect).")
                    else:
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        
                        # PARALLEL FETCHING
                        # Use ThreadPoolExecutor to fetch multiple suites at once
                        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                            future_to_suite = {
                                executor.submit(fetch_suite_data, s, st.session_state['api_key']): s 
                                for s in suites
                            }
                            
                            completed_count = 0
                            for future in concurrent.futures.as_completed(future_to_suite):
                                try:
                                    result = future.result()
                                    
                                    # Collect Suite Data
                                    processed_suites.append(result['data'])
                                    
                                    # Aggregate Metrics
                                    m = result['metrics']
                                    metric_total += m['total']
                                    metric_passing += m['passing']
                                    metric_failing += m['failing']
                                    metric_running += m['running']
                                    
                                except Exception as exc:
                                    print(f"Suite processing generated an exception: {exc}")
                                
                                completed_count += 1
                                progress_bar.progress(completed_count / len(suites))
                                status_text.text(f"Processed {completed_count}/{len(suites)} suites...")
                        
                        status_text.empty()
                        progress_bar.empty()

                        # --- SORTING ---
                        # Priority: RUNNING (0) > FAILING (1) > UNKNOWN (2) > PASSING (3) > EMPTY (4)
                        status_priority = {
                            "RUNNING": 0,
                            "RUNNING (Suite)": 0,
                            "FAILING": 1,
                            "UNKNOWN": 2,
                            "PASSING": 3,
                            "EMPTY": 4
                        }
                        # Sort the suites based on the priority map
                        processed_suites.sort(key=lambda x: status_priority.get(x['status_label'], 5))

                        # --- RENDER METRICS ---
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Total Tests", metric_total)
                        m2.metric("Passing", metric_passing)
                        m3.metric("Failing", metric_failing, delta_color="inverse")
                        m4.metric("Running", metric_running)
                        
                        st.divider()

                        # --- RENDER ACCORDIONS ---
                        has_rendered_any = False
                        
                        for p_suite in processed_suites:
                            # Apply User Filter
                            if suite_filter and suite_filter.lower() not in p_suite['name'].lower():
                                continue
                                
                            has_rendered_any = True
                            
                            # Determine if expander should be open by default (e.g. if failing or running)
                            default_expanded = p_suite['status_label'] in ['FAILING', 'RUNNING']
                            
                            # Expander Title with Icon AND Explicit Text Status
                            expander_label = f"{p_suite['icon']} [{p_suite['status_label']}] {p_suite['name']}  ({len(p_suite['rows'])} tests)"
                            
                            with st.expander(expander_label, expanded=default_expanded):
                                if p_suite['rows']:
                                    df = pd.DataFrame(p_suite['rows'])
                                    
                                    # Style the Status Column
                                    styled_df = df.style.map(color_status, subset=['Status'])
                                    
                                    st.dataframe(
                                        styled_df,
                                        use_container_width=True,
                                        hide_index=True,
                                        column_config={
                                            "Link": st.column_config.LinkColumn(
                                                "Action", 
                                                display_text="View Result" 
                                            ),
                                            "Last Run": st.column_config.DatetimeColumn(
                                                "Last Execution",
                                                format="D MMM YYYY, h:mm a"
                                            ),
                                            "Status": st.column_config.TextColumn(
                                                "State",
                                                width="small"
                                            ),
                                            "Test Name": st.column_config.TextColumn(
                                                "Test Name",
                                                width="large"
                                            ),
                                            "Test_ID": None
                                        }
                                    )
                                else:
                                    st.caption("No active tests found in this suite.")

                        if not has_rendered_any and suite_filter:
                            st.info(f"No suites match the filter '{suite_filter}'")

                # Call the fragment function
                render_tab_content(folder_id, folder_names[t_index], t_index)