import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import time
import concurrent.futures
import pytz
import os

# ---------------------------------------------------------------------------
# Page Configuration
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Ghost Inspector Suite Monitor", layout="wide")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SGT = pytz.timezone('Asia/Singapore')
STATUS_PRIORITY = {"RUNNING": 0, "RUNNING (Suite)": 0, "FAILING": 1, "UNKNOWN": 2, "PASSING": 3, "EMPTY": 4}
REMARKS_FILE = "remarks.csv"
CONFIG_FILE  = "user_configs.json"
AUTO_REFRESH_SECONDS = 60

# Map raw status → display label with colored emoji badge
STATUS_DISPLAY = {
    "PASSING":         "🟢 PASSING",
    "FAILING":         "🔴 FAILING",
    "UNKNOWN":         "🟡 UNKNOWN",
    "RUNNING":         "🔵 RUNNING",
    "RUNNING (Suite)": "🔵 RUNNING",
    "EMPTY":           "⚪ EMPTY",
}

def apply_status_display(df: "pd.DataFrame") -> "pd.DataFrame":
    """Replace raw Status values with emoji-prefixed display labels,
    and stringify Last Run to avoid DatetimeColumn + None crash in Streamlit."""
    out = df.copy()
    if "Status" in out.columns:
        out["Status"] = out["Status"].map(lambda v: STATUS_DISPLAY.get(v, v))
    if "Last Run" in out.columns:
        out["Last Run"] = out["Last Run"].apply(
            lambda v: v.astimezone(SGT).strftime("%-d %b %Y, %-I:%M %p") if pd.notna(v) and v is not None else "—"
        )
    return out

# ---------------------------------------------------------------------------
# API helpers  —  all cached to avoid redundant network calls
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_folder_name(api_key: str, folder_id: str) -> str:
    try:
        r = requests.get(
            f"https://api.ghostinspector.com/v1/folders/{folder_id}/",
            params={"apiKey": api_key}, timeout=5
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("name", f"Folder {folder_id}")
    except Exception as e:
        st.warning(f"Could not resolve name for folder `{folder_id}`: {e}")
        return f"Folder {folder_id}"


@st.cache_data(ttl=60, show_spinner=False)
def get_suites_in_folder(api_key: str, folder_id: str) -> list:
    try:
        r = requests.get(
            f"https://api.ghostinspector.com/v1/folders/{folder_id}/suites/",
            params={"apiKey": api_key}, timeout=10
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        st.warning(f"Error fetching suites for folder `{folder_id}`: {e}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def get_tests_in_suite(api_key: str, suite_id: str) -> list:
    try:
        r = requests.get(
            f"https://api.ghostinspector.com/v1/suites/{suite_id}/tests/",
            params={"apiKey": api_key}, timeout=10
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        st.warning(f"Error fetching tests for suite `{suite_id}`: {e}")
        return []


@st.cache_data(ttl=15, show_spinner=False)
def check_suite_running_via_badge(suite_id: str) -> bool:
    try:
        r = requests.get(
            f"https://api.ghostinspector.com/v1/suites/{suite_id}/status-badge",
            timeout=3, allow_redirects=True
        )
        return "running" in r.url.lower() or "running" in r.text.lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Private API — per-test running status via dateExecutionTriggered
# ---------------------------------------------------------------------------

def get_tests_running_status(suite_id: str, cookie: str, referrer: str) -> dict:
    """
    Calls the private GI app API to get per-test dateExecutionTriggered.
    Returns a dict of {test_id: True} for tests that are currently running.
    Falls back to empty dict if headers are missing or call fails.
    """
    if not cookie:
        return {}
    try:
        r = requests.get(
            f"https://app.ghostinspector.com/api/tests",
            params={"suite": suite_id, "count": 500, "page": 1},
            headers={
                "Cookie":  cookie,
                "Referer": referrer or "https://app.ghostinspector.com/",
            },
            timeout=10,
        )
        r.raise_for_status()
        tests = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
        running = {}
        for t in tests:
            triggered_str = t.get("dateExecutionTriggered")
            finished_str  = t.get("dateExecutionFinished")
            if triggered_str and finished_str:
                try:
                    dt_triggered = pd.to_datetime(triggered_str, utc=True)
                    dt_finished  = pd.to_datetime(finished_str,  utc=True)
                    if dt_triggered > dt_finished:
                        running[t["_id"]] = True
                except Exception:
                    pass
        return running
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------

def execute_suite(api_key: str, suite_id: str) -> dict:
    """Trigger a suite run. Returns {success: bool, message: str}.
    
    Uses a short connect timeout (10s) but a generous read timeout (120s)
    since GI's execute endpoint waits until the suite finishes before responding.
    We treat a ReadTimeout as a successful trigger — the suite was accepted.
    """
    try:
        r = requests.post(
            f"https://api.ghostinspector.com/v1/suites/{suite_id}/execute/",
            params={"apiKey": api_key},
            timeout=(10, 120),  # (connect timeout, read timeout)
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "SUCCESS":
            return {"success": True, "message": "Suite triggered successfully."}
        return {"success": False, "message": data.get("message", "Unknown error from API.")}
    except requests.exceptions.ReadTimeout:
        # GI executes the suite synchronously and can take a long time.
        # A read timeout means the request was accepted and is running.
        return {"success": True, "message": "Triggered (response timed out, suite is running)."}
    except requests.exceptions.ConnectTimeout:
        return {"success": False, "message": "Connection timed out — could not reach Ghost Inspector."}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# Data processing  —  pure function, no st.* calls
# ---------------------------------------------------------------------------

def fetch_suite_data(suite: dict, api_key: str, cookie: str = "", referrer: str = "") -> dict:
    """Fetch and process data for a single suite. Thread-safe: no st.* calls."""
    suite_id = suite["_id"]
    suite_name = suite["name"]

    is_running = check_suite_running_via_badge(suite_id)
    tests = get_tests_in_suite(api_key, suite_id)
    relevant = [t for t in tests if not t.get("importOnly", False)]

    # If private headers provided, get accurate per-test running status
    running_test_ids = get_tests_running_status(suite_id, cookie, referrer) if cookie else {}

    # Suite-level status label
    if is_running:
        suite_status = "RUNNING"
    elif not relevant:
        suite_status = "EMPTY"
    elif any(t.get("passing") is False for t in relevant):
        suite_status = "FAILING"
    elif all(t.get("passing") is None for t in relevant):
        suite_status = "UNKNOWN"
    else:
        suite_status = "PASSING"

    rows = []
    metrics = {"total": 0, "passing": 0, "failing": 0, "running": 0}
    latest_run = None

    for test in relevant:
        passing = test.get("passing")

        # Per-test running: use private API date comparison if available,
        # otherwise fall back to suite badge + passing=None heuristic.
        test_id = test["_id"]
        if running_test_ids.get(test_id):
            status = "RUNNING"
            metrics["running"] += 1
        elif not running_test_ids and is_running and passing is None:
            # Fallback: no private API — use badge + null result heuristic
            status = "RUNNING"
            metrics["running"] += 1
        elif passing is None:
            status = "UNKNOWN"
        elif passing:
            status = "PASSING"
            metrics["passing"] += 1
        else:
            status = "FAILING"
            metrics["failing"] += 1

        metrics["total"] += 1

        # Convert execution time to SGT
        raw_time = test.get("dateExecutionFinished") or test.get("dateExecutionStarted")
        exec_date = None
        if raw_time:
            ts = pd.to_datetime(raw_time)
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            exec_date = ts.tz_convert("Asia/Singapore")
            if latest_run is None or exec_date > latest_run:
                latest_run = exec_date

        rows.append({
            "Test Name": test["name"],
            "Suite Name": suite_name,
            "Status": status,
            "Last Run": exec_date,
            "Link": f"https://app.ghostinspector.com/tests/{test['_id']}",
            "Test_ID": test["_id"],
        })

    # Sort: running/failing first, then alpha
    rows.sort(key=lambda x: (STATUS_PRIORITY.get(x["Status"], 4), x["Test Name"]))

    return {
        "data": {
            "name": suite_name,
            "suite_id": suite_id,
            "status_label": suite_status,
            "last_run": latest_run,
            "rows": rows,
        },
        "metrics": metrics,
        "error": None,
    }


def fetch_suite_data_safe(suite: dict, api_key: str, cookie: str = "", referrer: str = "") -> dict:
    """Wrapper that captures exceptions so one bad suite doesn't break the batch."""
    try:
        return fetch_suite_data(suite, api_key, cookie, referrer)
    except Exception as exc:
        return {
            "data": {"name": suite.get("name", suite["_id"]), "status_label": "UNKNOWN", "last_run": None, "rows": []},
            "metrics": {"total": 0, "passing": 0, "failing": 0, "running": 0},
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Remarks board helpers
# ---------------------------------------------------------------------------

def _load_remarks() -> pd.DataFrame:
    if os.path.exists(REMARKS_FILE):
        try:
            return pd.read_csv(REMARKS_FILE)
        except Exception:
            pass
    return pd.DataFrame(columns=["Timestamp", "Author", "Remark"])


def _save_remarks(df: pd.DataFrame) -> None:
    try:
        df.to_csv(REMARKS_FILE, index=False)
    except Exception as e:
        st.warning(f"Could not persist remarks to disk: {e}")


# ---------------------------------------------------------------------------
# Per-API-key config persistence
# ---------------------------------------------------------------------------

def _load_user_config(api_key: str) -> dict:
    """Load saved folder/hidden-suite config for this API key."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                import json
                all_configs = json.load(f)
                return all_configs.get(api_key, {})
    except Exception:
        pass
    return {}


def _save_user_config(api_key: str, folder_ids: str, hidden_ids: str) -> None:
    """Persist folder/hidden-suite config for this API key."""
    try:
        import json
        all_configs = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                all_configs = json.load(f)
        all_configs[api_key] = {
            "monitored_folder_ids": folder_ids,
            "hidden_suite_ids":     hidden_ids,
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(all_configs, f)
    except Exception as e:
        pass  # non-critical — silently skip if file write fails


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_session_state() -> None:
    defaults = {
        "api_key": None,
        "private_cookie": "",
        "private_referrer": "https://app.ghostinspector.com/",
        "pending_login": None,
        "monitored_folder_ids": "",
        "hidden_suite_ids": "",
        "remarks_data": _load_remarks(),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


_init_session_state()

# ---------------------------------------------------------------------------
# Shared tab renderer  —  defined ONCE, outside the loop
# ---------------------------------------------------------------------------

@st.fragment
def render_tab_content(
    fid: str,
    fname: str,
    tab_idx: int,
    view_mode: str,
    hidden_suites: list,
    auto_refresh: bool,
) -> None:
    st.subheader(f"📂 {fname}")
    now_sgt = datetime.now().astimezone(SGT)
    st.caption(f"ID: `{fid}` | Updated: **{now_sgt.strftime('%I:%M:%S %p')} SGT**")

    # Per-view filter inputs
    suite_filter = ""
    status_filter = "All"

    filter_col1, filter_col2 = st.columns([3, 1])
    if view_mode == "Group by Suite":
        suite_filter = filter_col1.text_input(
            "Filter Suites", key=f"filter_suite_{fid}_{tab_idx}", placeholder="Search suites…"
        )
    else:
        status_filter = filter_col1.selectbox(
            "Filter by Status",
            ["All", "FAILING", "RUNNING", "UNKNOWN", "PASSING"],
            key=f"filter_status_{fid}_{tab_idx}",
        )

    # ── Placeholder metrics (shown immediately while data loads) ──────────
    m1, m2, m3, m4 = st.columns(4)
    total_ph    = m1.empty()
    passing_ph  = m2.empty()
    failing_ph  = m3.empty()
    running_ph  = m4.empty()
    for ph, label in [(total_ph, "Total Tests"), (passing_ph, "Passing"),
                      (failing_ph, "Failing"),   (running_ph, "Running")]:
        ph.metric(label, "…")

    st.divider()

    # ── Guard against missing session key (e.g. fragment rerun after logout) ──
    api_key = st.session_state.get("api_key")
    if not api_key:
        st.warning("Session expired. Please log in again.")
        return

    # ── Data fetch ────────────────────────────────────────────────────────
    with st.spinner(f"Fetching suites for {fname}…"):
        suites = get_suites_in_folder(api_key, fid)

    if not suites:
        st.warning(f"No suites found in folder `{fid}`. Check the folder ID.")
        return

    active_suites = [s for s in suites if s["_id"] not in hidden_suites]
    if not active_suites:
        st.warning("All suites in this folder are hidden.")
        return

    progress_bar = st.progress(0)
    status_text  = st.empty()

    processed_suites: list = []
    all_tests_flat:  list = []
    agg = {"total": 0, "passing": 0, "failing": 0, "running": 0}

    max_workers = min(10, len(active_suites))
    _cookie   = st.session_state.get("private_cookie", "")
    _referrer = st.session_state.get("private_referrer", "https://app.ghostinspector.com/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_suite_data_safe, s, api_key, _cookie, _referrer): s
            for s in active_suites
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()

            if result["error"]:
                st.warning(f"⚠️ Suite **{result['data']['name']}** failed to load: {result['error']}")

            processed_suites.append(result["data"])
            all_tests_flat.extend(result["data"]["rows"])
            for k in agg:
                agg[k] += result["metrics"][k]

            progress_bar.progress(i / len(active_suites))
            status_text.text(f"Processed {i}/{len(active_suites)} suites…")

    status_text.empty()
    progress_bar.empty()

    # ── Populate real metrics ─────────────────────────────────────────────
    total_ph.metric("Total Tests", agg["total"])
    passing_ph.metric("Passing", agg["passing"])
    failing_ph.metric("Failing", agg["failing"], delta_color="inverse")
    running_ph.metric("Running", agg["running"])

    # ── VIEW 1: Group by Suite ────────────────────────────────────────────
    if view_mode == "Group by Suite":
        processed_suites.sort(key=lambda x: STATUS_PRIORITY.get(x["status_label"], 5))
        rendered_any = False

        for p_suite in processed_suites:
            if suite_filter and suite_filter.lower() not in p_suite["name"].lower():
                continue
            rendered_any = True

            date_str = (
                f"— Last Run: {p_suite['last_run'].astimezone(SGT).strftime('%-d %b %-I:%M %p')}"
                if p_suite["last_run"] else ""
            )
            label = f"[{p_suite['status_label']}] {p_suite['name']} ({len(p_suite['rows'])} tests) {date_str}"
            expanded = p_suite["status_label"] in ("FAILING", "RUNNING")

            with st.expander(label, expanded=expanded):
                # Per-suite Run button
                run_col, _ = st.columns([1, 5])
                with run_col:
                    if st.button("▶️ Run Suite", key=f"run_{p_suite['suite_id']}"):
                        res = execute_suite(api_key, p_suite["suite_id"])
                        if res["success"]:
                            st.success(f"✅ {p_suite['name']} triggered!")
                        else:
                            st.error(f"❌ {res['message']}")

                if p_suite["rows"]:
                    df = pd.DataFrame(p_suite["rows"]).drop(columns=["Suite Name", "Test_ID"], errors="ignore")
                    st.dataframe(
                        apply_status_display(df),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Link":      st.column_config.LinkColumn("Action", display_text="View Result"),
                            "Last Run":  st.column_config.TextColumn("Last Execution", width="medium"),
                            "Status":    st.column_config.TextColumn("State", width="small"),
                            "Test Name": st.column_config.TextColumn("Test Name", width="large"),
                        },
                    )
                else:
                    st.caption("No active tests found in this suite.")

        if not rendered_any and suite_filter:
            st.info(f"No suites match the filter '{suite_filter}'.")

    # ── VIEW 2: Group by Status ───────────────────────────────────────────
    else:
        if not all_tests_flat:
            st.info("No tests found.")
            return

        df_all = pd.DataFrame(all_tests_flat)
        df_all["_priority"] = df_all["Status"].map(lambda s: STATUS_PRIORITY.get(s, 4))
        df_all = df_all.sort_values(["_priority", "Test Name"]).drop(columns=["_priority"])

        if status_filter != "All":
            df_all = df_all[df_all["Status"].str.contains(status_filter, na=False)]

        with st.expander(f"All Tests ({len(df_all)})", expanded=True):
            st.dataframe(
                apply_status_display(df_all.drop(columns=["Test_ID"], errors="ignore")),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Link":       st.column_config.LinkColumn("Action", display_text="View Result"),
                    "Last Run":   st.column_config.TextColumn("Last Execution", width="medium"),
                    "Status":     st.column_config.TextColumn("State", width="small"),
                    "Suite Name": st.column_config.TextColumn("Suite", width="medium"),
                    "Test Name":  st.column_config.TextColumn("Test Name", width="large"),
                },
            )

    # ── Auto-refresh ──────────────────────────────────────────────────────
    if auto_refresh:
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun(scope="fragment")


# ---------------------------------------------------------------------------
# LOGIN SCREEN
# ---------------------------------------------------------------------------

if not st.session_state["api_key"]:
    st.title("Ghost Inspector Login")
    st.markdown("Enter your API key to begin. Cookie and Referrer are optional but enable accurate per-test running status.")

    with st.form("login_form"):
        user_key = st.text_input("API Key", type="password")

        st.markdown("**Optional — Private API Headers** *(for accurate per-test RUNNING status)*")
        cookie   = st.text_input(
            "Cookie",
            type="password",
            placeholder="Paste your browser Cookie header value…",
            help=(
                "How to get your Cookie:\n\n"
                "1. Open **Chrome** and go to app.ghostinspector.com\n"
                "2. Log in if you haven't already\n"
                "3. Press **F12** to open DevTools → go to the **Network** tab\n"
                "4. Refresh the page, then click any request to **app.ghostinspector.com**\n"
                "5. In the **Request Headers** section, find the **Cookie** field\n"
                "6. Copy the entire value and paste it here\n\n"
                "⚠️ Your cookie expires periodically — re-enter it if RUNNING status stops working."
            ),
        )
        referrer = st.text_input("Referrer", value="https://app.ghostinspector.com/", placeholder="https://app.ghostinspector.com/")

        if st.form_submit_button("Start Session"):
            if not user_key:
                st.error("API Key cannot be empty.")
            elif not cookie:
                # Store credentials in a pending state and show confirmation modal
                st.session_state["pending_login"] = {
                    "api_key":  user_key,
                    "referrer": referrer or "https://app.ghostinspector.com/",
                }
                st.rerun()
            else:
                saved = _load_user_config(user_key)
                st.session_state["api_key"]                = user_key
                st.session_state["private_cookie"]         = cookie
                st.session_state["private_referrer"]       = referrer or "https://app.ghostinspector.com/"
                st.session_state["monitored_folder_ids"]   = saved.get("monitored_folder_ids", "")
                st.session_state["hidden_suite_ids"]       = saved.get("hidden_suite_ids", "")
                st.rerun()

    # ── Confirmation modal when Cookie is missing ─────────────────────────
    if st.session_state.get("pending_login"):
        @st.dialog("⚠️ Limited Running Status")
        def _confirm_no_cookie():
            st.warning(
                "You haven't provided a **Cookie** header.\n\n"
                "Without it, the dashboard cannot accurately detect which individual "
                "tests are currently **RUNNING**. You will still see the suite-level "
                "running indicator, but per-test RUNNING status will not be shown.\n\n"
                "Do you want to continue anyway?"
            )
            col1, col2 = st.columns(2)
            if col1.button("✅ Continue without Cookie", use_container_width=True):
                pending = st.session_state.pop("pending_login")
                saved = _load_user_config(pending["api_key"])
                st.session_state["api_key"]                = pending["api_key"]
                st.session_state["private_cookie"]         = ""
                st.session_state["private_referrer"]       = pending["referrer"]
                st.session_state["monitored_folder_ids"]   = saved.get("monitored_folder_ids", "")
                st.session_state["hidden_suite_ids"]       = saved.get("hidden_suite_ids", "")
                st.rerun()
            if col2.button("🔙 Go Back", use_container_width=True):
                st.session_state.pop("pending_login")
                st.rerun()

        _confirm_no_cookie()

# ---------------------------------------------------------------------------
# DASHBOARD SCREEN
# ---------------------------------------------------------------------------

else:
    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Configuration")

        folder_input = st.text_area(
            "Folder IDs (comma-separated)",
            value=st.session_state["monitored_folder_ids"],
            placeholder="e.g. 64b123, 64b456",
            height=100,
        )
        hidden_suite_input = st.text_area(
            "Hidden Suite IDs (comma-separated)",
            value=st.session_state["hidden_suite_ids"],
            placeholder="e.g. 55e7185cf538...",
            height=100,
        )
        view_mode    = st.radio("View Mode", ["Group by Suite", "Group by Status"])
        auto_refresh = st.checkbox(f"Auto-Refresh ({AUTO_REFRESH_SECONDS}s)", value=False)

        if st.button("✅ Apply & Load", use_container_width=True):
            st.session_state["monitored_folder_ids"] = folder_input
            st.session_state["hidden_suite_ids"]     = hidden_suite_input
            # Persist config for this API key
            _save_user_config(
                st.session_state["api_key"], folder_input, hidden_suite_input
            )
            # Bust cache so fresh data is fetched after config changes
            get_suites_in_folder.clear()
            get_tests_in_suite.clear()
            st.rerun()

        if st.button("🔄 Manual Refresh", use_container_width=True):
            get_suites_in_folder.clear()
            get_tests_in_suite.clear()
            st.rerun()

        st.divider()
        st.markdown("**▶️ Run Suites**")
        if st.button("🚀 Run All Suites", use_container_width=True):
            raw = st.session_state.get("monitored_folder_ids", "")
            _api_key = st.session_state.get("api_key")
            folder_ids_run = [f.strip() for f in raw.split(",") if f.strip()]
            hidden = [s.strip() for s in st.session_state.get("hidden_suite_ids", "").split(",") if s.strip()]
            triggered, failed = [], []

            # Collect all suites first
            all_suites_to_run = []
            for fid in folder_ids_run:
                for suite in get_suites_in_folder(_api_key, fid):
                    if suite["_id"] not in hidden:
                        all_suites_to_run.append(suite)

            total = len(all_suites_to_run)
            run_status = st.empty()
            run_progress = st.progress(0)
            run_status.info(f"⏳ Triggering **{total}** suites in parallel…")

            # Fire all execute calls concurrently — much faster than sequential
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, total)) as executor:
                future_to_suite = {
                    executor.submit(execute_suite, _api_key, suite["_id"]): suite
                    for suite in all_suites_to_run
                }
                for future in concurrent.futures.as_completed(future_to_suite):
                    suite = future_to_suite[future]
                    result = future.result()
                    (triggered if result["success"] else failed).append(suite["name"])
                    completed += 1
                    run_progress.progress(completed / total)
                    run_status.info(f"⏳ Triggered **{completed}/{total}** suites…")

            run_status.empty()
            run_progress.empty()

            if triggered:
                st.success(f"✅ Triggered {len(triggered)} suite(s)")
                for name in sorted(triggered):
                    st.markdown(f"&nbsp;&nbsp;🟢 {name}")
            if failed:
                st.error(f"❌ Failed to trigger {len(failed)} suite(s)")
                for name in sorted(failed):
                    st.markdown(f"&nbsp;&nbsp;🔴 {name}")

        st.divider()
        if st.button("🚪 Logout", use_container_width=True):
            for key in ["api_key", "monitored_folder_ids", "hidden_suite_ids"]:
                st.session_state[key] = "" if key != "api_key" else None
            st.rerun()

    st.title("Suite & Test Monitor")

    # Force all expanders to always render fully opaque and enabled
    st.markdown("""
        <style>
            /* Remove disabled/faded look from all expanders */
            div[data-testid="stExpander"] {
                opacity: 1 !important;
                pointer-events: auto !important;
            }
            div[data-testid="stExpander"] * {
                opacity: 1 !important;
                pointer-events: auto !important;
            }
            /* Ensure expander header text is always fully visible */
            div[data-testid="stExpander"] summary {
                opacity: 1 !important;
                color: inherit !important;
            }
        </style>
    """, unsafe_allow_html=True)

    # ── Notification / Remarks Board ──────────────────────────────────────
    with st.expander("📝 Notification Board / Remarks", expanded=True):
        with st.form("new_remark", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 4, 1])
            author  = c1.text_input("Author", value="QA Team")
            remark  = c2.text_input("Remark", placeholder="e.g. Investigating failures in Suite A…")
            c3.write("")  # vertical spacer to align button with inputs
            post_btn = c3.form_submit_button("Post", use_container_width=True)

            if post_btn and remark:
                ts  = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
                new = pd.DataFrame([{"Timestamp": ts, "Author": author, "Remark": remark}])
                st.session_state["remarks_data"] = pd.concat(
                    [new, st.session_state["remarks_data"]], ignore_index=True
                )
                _save_remarks(st.session_state["remarks_data"])
                st.rerun()

        df_remarks = st.session_state["remarks_data"]
        if not df_remarks.empty:
            col_table, col_clear = st.columns([5, 1])
            with col_table:
                st.dataframe(
                    df_remarks,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Timestamp": st.column_config.TextColumn("Time (SGT)", width="medium"),
                        "Author":    st.column_config.TextColumn("Author",     width="small"),
                        "Remark":    st.column_config.TextColumn("Message",    width="large"),
                    },
                )
            with col_clear:
                if st.button("🗑️ Clear All", use_container_width=True):
                    st.session_state["remarks_data"] = pd.DataFrame(columns=["Timestamp", "Author", "Remark"])
                    _save_remarks(st.session_state["remarks_data"])
                    st.rerun()
        else:
            st.info("No remarks posted yet.")

    st.divider()

    # ── Parse config ──────────────────────────────────────────────────────
    folder_ids = [f.strip() for f in st.session_state["monitored_folder_ids"].split(",") if f.strip()]
    hidden_ids = [s.strip() for s in st.session_state["hidden_suite_ids"].split(",") if s.strip()]

    if not folder_ids:
        st.info("Enter one or more **Folder IDs** in the sidebar and click **Apply & Load**.")
    else:
        # Resolve folder names in parallel
        # Read api_key BEFORE entering threads — st.session_state is not thread-safe
        _api_key = st.session_state.get("api_key")
        with st.spinner("Resolving folder names…"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(folder_ids)) as ex:
                folder_names = list(ex.map(
                    lambda fid: get_folder_name(_api_key, fid),
                    folder_ids,
                ))

        tabs = st.tabs(folder_names)
        for tab_idx, (folder_id, folder_name) in enumerate(zip(folder_ids, folder_names)):
            with tabs[tab_idx]:
                render_tab_content(
                    fid=folder_id,
                    fname=folder_name,
                    tab_idx=tab_idx,
                    view_mode=view_mode,
                    hidden_suites=hidden_ids,
                    auto_refresh=auto_refresh,
                )