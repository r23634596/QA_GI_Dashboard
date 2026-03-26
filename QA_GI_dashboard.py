import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import time
import concurrent.futures
import pytz
import os
import hashlib

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
    Also returns a debug info dict under key "__debug__".
    """
    if not cookie:
        return {"__debug__": "no_cookie"}
    try:
        r = requests.get(
            "https://app.ghostinspector.com/api/tests",
            params={"suite": suite_id, "count": 500, "page": 1},
            headers={
                "Cookie":  cookie,
                "Referer": referrer or "https://app.ghostinspector.com/",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return {"__debug__": f"http_{r.status_code}"}

        body = r.json()
        tests = body if isinstance(body, list) else body.get("data", [])

        if not isinstance(tests, list):
            return {"__debug__": f"unexpected_shape:{type(tests).__name__}"}

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

        running["__debug__"] = f"ok:{len(tests)}_tests_{len([k for k in running if k != '__debug__'])}_running"
        return running
    except Exception as e:
        return {"__debug__": f"exception:{e}"}


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


def execute_test(api_key: str, test_id: str) -> dict:
    """Trigger a single test run. Returns {success: bool, message: str}."""
    try:
        r = requests.post(
            f"https://api.ghostinspector.com/v1/tests/{test_id}/execute/",
            params={"apiKey": api_key},
            timeout=(10, 120),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "SUCCESS":
            return {"success": True, "message": "Test triggered successfully."}
        return {"success": False, "message": data.get("message", "Unknown error from API.")}
    except requests.exceptions.ReadTimeout:
        return {"success": True, "message": "Triggered (test is running)."}
    except requests.exceptions.ConnectTimeout:
        return {"success": False, "message": "Connection timed out."}
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
    cookie_invalid   = False
    if cookie:
        running_test_ids = get_tests_running_status(suite_id, cookie, referrer)
        cookie_invalid   = running_test_ids.pop("__cookie_invalid__", False)
        running_test_ids.pop("__debug__", None)
    else:
        running_test_ids = {}

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
            "name":           suite_name,
            "suite_id":       suite_id,
            "status_label":   suite_status,
            "last_run":       latest_run,
            "rows":           rows,
            "cookie_invalid": cookie_invalid,
        },
        "metrics": metrics,
        "error":   None,
    }


def fetch_suite_data_safe(suite: dict, api_key: str, cookie: str = "", referrer: str = "") -> dict:
    """Wrapper that captures exceptions so one bad suite doesn't break the batch."""
    try:
        return fetch_suite_data(suite, api_key, cookie, referrer)
    except Exception as exc:
        return {
            "data": {"name": suite.get("name", suite["_id"]), "suite_id": suite.get("_id", ""),
                     "status_label": "UNKNOWN", "last_run": None, "rows": [], "cookie_invalid": False},
            "metrics": {"total": 0, "passing": 0, "failing": 0, "running": 0},
            "error":   str(exc),
        }


# ---------------------------------------------------------------------------
# Remarks board helpers
# ---------------------------------------------------------------------------

def _hash_key(api_key: str) -> str:
    """One-way hash of API key — used to tag remarks without storing the raw key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def _load_remarks() -> pd.DataFrame:
    if os.path.exists(REMARKS_FILE):
        try:
            df = pd.read_csv(REMARKS_FILE)
            # Backfill ApiKeyHash column for older remarks that predate this feature
            if "ApiKeyHash" not in df.columns:
                df["ApiKeyHash"] = ""
            return df
        except Exception:
            pass
    return pd.DataFrame(columns=["Timestamp", "Author", "Remark", "ApiKeyHash"])


def _save_remarks(df: pd.DataFrame) -> None:
    try:
        df.to_csv(REMARKS_FILE, index=False)
    except Exception as e:
        st.warning(f"Could not persist remarks to disk: {e}")


# ---------------------------------------------------------------------------
# Per-API-key config persistence
# ---------------------------------------------------------------------------

def _load_user_config(api_key: str) -> dict:
    """Load saved folder/hidden-suite config for this API key.
    Config is keyed by SHA-256 hash — raw API key is never written to disk.
    """
    try:
        import json
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                all_configs = json.load(f)
            # Look up by hash only
            return all_configs.get(_hash_key(api_key), {})
    except Exception:
        pass
    return {}


def _save_user_config(api_key: str, folder_ids: str, hidden_ids: str,
                      username: str = "") -> None:
    """Persist folder/hidden-suite config and username keyed by hashed API key.
    Raw API key is never written to disk.
    """
    try:
        import json
        all_configs = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                all_configs = json.load(f)
        existing = all_configs.get(_hash_key(api_key), {})
        all_configs[_hash_key(api_key)] = {
            "monitored_folder_ids": folder_ids,
            "hidden_suite_ids":     hidden_ids,
            # Only update username if provided, preserve existing otherwise
            "username": username if username else existing.get("username", ""),
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(all_configs, f, indent=2)
    except Exception:
        pass  # non-critical — silently skip if file write fails


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _bump_counter():
    """Increment btn_counter so all keyed buttons get new DOM keys → no spam."""
    st.session_state["btn_counter"] = st.session_state.get("btn_counter", 0) + 1


def _init_session_state() -> None:
    defaults = {
        "api_key": None,
        "private_cookie": "",
        "private_referrer": "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b",
        "pending_login": None,
        "username": "",
        "pending_credentials": None,  # holds api_key/cookie/referrer between steps
        "open_advanced": False,
        "monitored_folder_ids": "",
        "hidden_suite_ids": "",
        "folders_applied": False,
        "running_all": False,
        "_run_all_pending": False,
        "run_all_progress": 0,
        "run_all_status": "",
        "run_all_results": None,
        "selected_tests": {},   # {test_id: test_name}
        "suite_data_cache": {},  # {fid: {processed_suites, all_tests_flat, agg}}
        "remarks_data": _load_remarks(),
        "btn_counter": 0,
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

    _locked = st.session_state.get("running_all", False)
    filter_col1, filter_col2 = st.columns([3, 1])

    if _locked:
        st.info("⏳ **Suite run in progress** — controls are locked to prevent accidental interruption.", icon="🔒")

    if view_mode == "Group by Suite":
        suite_filter = filter_col1.text_input(
            "Filter Suites", key=f"filter_suite_{fid}_{tab_idx}",
            placeholder="Search suites…", disabled=_locked,
        )
    else:
        status_filter = filter_col1.selectbox(
            "Filter by Status",
            ["All", "FAILING", "RUNNING", "UNKNOWN", "PASSING"],
            key=f"filter_status_{fid}_{tab_idx}",
            disabled=_locked,
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

    # ── Data fetch — use cache to avoid reloading on checkbox/button clicks ──
    cache = st.session_state.get("suite_data_cache", {})

    if fid not in cache:
        # No cache — fetch fresh data
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
        all_tests_flat:   list = []
        agg = {"total": 0, "passing": 0, "failing": 0, "running": 0}

        max_workers = min(10, len(active_suites))
        _cookie   = st.session_state.get("private_cookie", "")
        _referrer = st.session_state.get("private_referrer", "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b")
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

        # Store in cache
        st.session_state["suite_data_cache"][fid] = {
            "processed_suites": processed_suites,
            "all_tests_flat":   all_tests_flat,
            "agg":              agg,
        }
    else:
        # Use cached data — no re-fetch on checkbox/button interactions
        processed_suites = cache[fid]["processed_suites"]
        all_tests_flat   = cache[fid]["all_tests_flat"]
        agg              = cache[fid]["agg"]

    # ── Populate real metrics ─────────────────────────────────────────────
    total_ph.metric("Total Tests", agg["total"])
    passing_ph.metric("Passing", agg["passing"])
    failing_ph.metric("Failing", agg["failing"], delta_color="inverse")
    running_ph.metric("Running", agg["running"])

    # ── Cookie expiry warning ────────────────────────────────────────────
    if any(r.get("cookie_invalid") for r in processed_suites):
        st.warning(
            "⚠️ **Cookie Expired or Invalid** — Your Cookie header is no longer accepted by Ghost Inspector. "
            "Per-test RUNNING status will not be accurate. "
            "Please log out and log in again with a fresh Cookie.",
            icon="🍪",
        )

    # ── VIEW 1: Group by Suite ────────────────────────────────────────────
    _bc = st.session_state.get("btn_counter", 0)  # shared by both views
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
                suite_id = p_suite["suite_id"]
                run_result_key = f"run_result_{suite_id}"

                run_col, msg_col = st.columns([1, 5])
                with run_col:
                    if st.button("▶️ Run Suite", key=f"run_{suite_id}_{_bc}"):
                        st.session_state[run_result_key] = None
                        res = execute_suite(api_key, suite_id)
                        st.session_state[run_result_key] = res

                        # ── Log result only after execute returns ──────
                        _key_hash = _hash_key(st.session_state.get("api_key", ""))
                        _ts       = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
                        _username = st.session_state.get("username", "System")
                        _outcome  = "✅ triggered successfully" if res["success"] else f"❌ failed — {res['message']}"
                        _entry = pd.DataFrame([{
                            "Timestamp":  _ts,
                            "Author":     _username,
                            "Remark":     f"▶️ Ran suite: {p_suite['name']} — {_outcome}",
                            "ApiKeyHash": _key_hash,
                        }])
                        st.session_state["remarks_data"] = pd.concat(
                            [_entry, st.session_state["remarks_data"]], ignore_index=True
                        )
                        _save_remarks(st.session_state["remarks_data"])

                # Show persistent result message outside the button column
                with msg_col:
                    res = st.session_state.get(run_result_key)
                    if res:
                        if res["success"]:
                            st.success(f"✅ {p_suite['name']} triggered!")
                        else:
                            st.error(f"❌ {res['message']}")

                if p_suite["rows"]:
                    # ── Select / Clear — rendered FIRST so session state updates before Run Selected ──
                    _n_suite  = len(p_suite["rows"])
                    _sa_col, _sd_col, _sc_col = st.columns([1, 1, 6])
                    if _sa_col.button("Select All", key=f"sa_{suite_id}", use_container_width=True, type="tertiary"):
                        for row in p_suite["rows"]:
                            st.session_state["selected_tests"][row["Test_ID"]] = row["Test Name"]
                    if _sd_col.button("Clear", key=f"sd_{suite_id}", use_container_width=True, type="tertiary"):
                        for row in p_suite["rows"]:
                            st.session_state["selected_tests"].pop(row["Test_ID"], None)
                    # Recompute AFTER buttons may have updated session state
                    _n_sel = sum(1 for r in p_suite["rows"] if r["Test_ID"] in st.session_state["selected_tests"])
                    if _n_sel:
                        _sc_col.caption(f"{_n_sel} of {_n_suite} selected")

                    # ── Run Selected button ───────────────────────────
                    selected_in_suite = {
                        tid: tname for tid, tname in st.session_state["selected_tests"].items()
                        if any(r["Test_ID"] == tid for r in p_suite["rows"])
                    }
                    if selected_in_suite:
                        if st.button(
                            f"▶️ Run {len(selected_in_suite)} Selected Test(s)",
                            key=f"run_sel_{suite_id}_{_bc}",
                            type="primary",
                        ):
                            _key_hash = _hash_key(st.session_state.get("api_key", ""))
                            _username = st.session_state.get("username", "System")
                            sel_triggered, sel_failed = [], []

                            _total_sel = len(selected_in_suite)
                            _sel_status = st.empty()
                            _sel_prog   = st.progress(0)
                            _sel_status.info(f"⏳ Triggering **{_total_sel}** test(s)…")
                            _sel_done = 0
                            with concurrent.futures.ThreadPoolExecutor(
                                max_workers=min(5, _total_sel)
                            ) as ex:
                                fut_map = {
                                    ex.submit(execute_test, api_key, tid): (tid, tname)
                                    for tid, tname in selected_in_suite.items()
                                }
                                for fut in concurrent.futures.as_completed(fut_map):
                                    tid, tname = fut_map[fut]
                                    res = fut.result()
                                    (sel_triggered if res["success"] else sel_failed).append(tname)
                                    _sel_done += 1
                                    _sel_prog.progress(_sel_done / _total_sel)
                                    _sel_status.info(f"⏳ Triggered **{_sel_done}/{_total_sel}** test(s)…")
                            _sel_status.empty()
                            _sel_prog.empty()

                            _ts = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
                            _names = ", ".join(sel_triggered + sel_failed)
                            _outcome = f"✅ {len(sel_triggered)} triggered, ❌ {len(sel_failed)} failed"
                            _entry = pd.DataFrame([{
                                "Timestamp":  _ts,
                                "Author":     _username,
                                "Remark":     f"▶️ Ran {len(selected_in_suite)} test(s) in {p_suite['name']}: {_names} — {_outcome}",
                                "ApiKeyHash": _key_hash,
                            }])
                            st.session_state["remarks_data"] = pd.concat(
                                [_entry, st.session_state["remarks_data"]], ignore_index=True
                            )
                            _save_remarks(st.session_state["remarks_data"])

                            if sel_triggered:
                                st.success(f"✅ Triggered: {', '.join(sel_triggered)}")
                            if sel_failed:
                                st.error(f"❌ Failed: {', '.join(sel_failed)}")

                    # ── data_editor with CheckboxColumn ──────────────
                    df_sel = apply_status_display(
                        pd.DataFrame(p_suite["rows"]).drop(columns=["Suite Name"], errors="ignore")
                    )
                    # Read widget edits FIRST, apply to session state before rendering
                    _tbl_key   = f"tbl_{suite_id}"
                    _tbl_state = st.session_state.get(_tbl_key, {})
                    _edited_rows = _tbl_state.get("edited_rows", {})
                    _test_list   = list(df_sel["Test_ID"])
                    for row_idx_str, changes in _edited_rows.items():
                        if "Select" in changes:
                            try:
                                tid = _test_list[int(row_idx_str)]
                                tname = df_sel.loc[df_sel["Test_ID"] == tid, "Test Name"].iloc[0]
                                if changes["Select"]:
                                    st.session_state["selected_tests"][tid] = tname
                                else:
                                    st.session_state["selected_tests"].pop(tid, None)
                            except (IndexError, KeyError):
                                pass

                    df_sel.insert(0, "Select", [
                        tid in st.session_state["selected_tests"]
                        for tid in df_sel["Test_ID"]
                    ])
                    st.data_editor(
                        df_sel,
                        use_container_width=True,
                        hide_index=True,
                        key=_tbl_key,
                        column_config={
                            "Select":    st.column_config.CheckboxColumn("✓", width="small"),
                            "Link":      st.column_config.LinkColumn("Action", display_text="View"),
                            "Last Run":  st.column_config.TextColumn("Last Execution", width="medium"),
                            "Status":    st.column_config.TextColumn("State", width="small"),
                            "Test Name": st.column_config.TextColumn("Test Name", width="large"),
                            "Test_ID":   None,
                        },
                        disabled=["Test Name", "Status", "Last Run", "Link"],
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
                # ── Select All / Clear — rendered FIRST ───────────────
                _n_all  = len(df_all)
                _lc1, _lc2, _lc3 = st.columns([1, 1, 6])
                if _lc1.button("Select All", key=f"sa_status_{fid}_{tab_idx}", use_container_width=True, type="tertiary"):
                    for _, row in df_all.iterrows():
                        st.session_state["selected_tests"][row["Test_ID"]] = row["Test Name"]
                if _lc2.button("Clear", key=f"sd_status_{fid}_{tab_idx}", use_container_width=True, type="tertiary"):
                    for _, row in df_all.iterrows():
                        st.session_state["selected_tests"].pop(row["Test_ID"], None)
                # Recompute AFTER buttons may have updated session state
                _n_sall = sum(1 for tid in df_all["Test_ID"] if tid in st.session_state["selected_tests"])
                if _n_sall:
                    _lc3.caption(f"{_n_sall} of {_n_all} selected")

                # ── Run Selected button ───────────────────────────────
                selected_in_view = {
                    tid: tname for tid, tname in st.session_state["selected_tests"].items()
                    if any(r["Test_ID"] == tid for r in all_tests_flat)
                }
                if selected_in_view:
                    if st.button(
                        f"▶️ Run {len(selected_in_view)} Selected Test(s)",
                        key=f"run_sel_status_{fid}_{tab_idx}_{_bc}",
                        type="primary",
                    ):
                        _key_hash = _hash_key(st.session_state.get("api_key", ""))
                        _username = st.session_state.get("username", "System")
                        sel_triggered, sel_failed = [], []

                        _total_sel = len(selected_in_view)
                        _sel_status = st.empty()
                        _sel_prog   = st.progress(0)
                        _sel_status.info(f"⏳ Triggering **{_total_sel}** test(s)…")
                        _sel_done = 0
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(5, _total_sel)
                        ) as ex:
                            fut_map = {
                                ex.submit(execute_test, api_key, tid): (tid, tname)
                                for tid, tname in selected_in_view.items()
                            }
                            for fut in concurrent.futures.as_completed(fut_map):
                                tid, tname = fut_map[fut]
                                res = fut.result()
                                (sel_triggered if res["success"] else sel_failed).append(tname)
                                _sel_done += 1
                                _sel_prog.progress(_sel_done / _total_sel)
                                _sel_status.info(f"⏳ Triggered **{_sel_done}/{_total_sel}** test(s)…")
                        _sel_status.empty()
                        _sel_prog.empty()

                        _ts = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
                        _names = ", ".join(sel_triggered + sel_failed)
                        _outcome = f"✅ {len(sel_triggered)} triggered, ❌ {len(sel_failed)} failed"
                        _entry = pd.DataFrame([{
                            "Timestamp":  _ts,
                            "Author":     _username,
                            "Remark":     f"▶️ Ran {len(selected_in_view)} test(s): {_names} — {_outcome}",
                            "ApiKeyHash": _key_hash,
                        }])
                        st.session_state["remarks_data"] = pd.concat(
                            [_entry, st.session_state["remarks_data"]], ignore_index=True
                        )
                        _save_remarks(st.session_state["remarks_data"])

                        if sel_triggered:
                            st.success(f"✅ Triggered: {', '.join(sel_triggered)}")
                        if sel_failed:
                            st.error(f"❌ Failed: {', '.join(sel_failed)}")

                # ── data_editor with CheckboxColumn ──────────────────
                df_display = apply_status_display(df_all.copy())
                # Read widget edits FIRST before rendering
                _tbl_key_s   = f"tbl_status_{fid}_{tab_idx}"
                _tbl_state_s = st.session_state.get(_tbl_key_s, {})
                _edited_rows_s = _tbl_state_s.get("edited_rows", {})
                _test_list_s   = list(df_display["Test_ID"])
                for row_idx_str, changes in _edited_rows_s.items():
                    if "Select" in changes:
                        try:
                            tid = _test_list_s[int(row_idx_str)]
                            tname = df_display.loc[df_display["Test_ID"] == tid, "Test Name"].iloc[0]
                            if changes["Select"]:
                                st.session_state["selected_tests"][tid] = tname
                            else:
                                st.session_state["selected_tests"].pop(tid, None)
                        except (IndexError, KeyError):
                            pass

                df_display.insert(0, "Select", [
                    tid in st.session_state["selected_tests"]
                    for tid in df_display["Test_ID"]
                ])
                st.data_editor(
                    df_display,
                    use_container_width=True,
                    hide_index=True,
                    key=_tbl_key_s,
                    column_config={
                        "Select":     st.column_config.CheckboxColumn("✓", width="small"),
                        "Link":       st.column_config.LinkColumn("Action", display_text="View"),
                        "Last Run":   st.column_config.TextColumn("Last Execution", width="medium"),
                        "Status":     st.column_config.TextColumn("State", width="small"),
                        "Suite Name": st.column_config.TextColumn("Suite", width="medium"),
                        "Test Name":  st.column_config.TextColumn("Test Name", width="large"),
                        "Test_ID":    None,
                    },
                    disabled=["Test Name", "Status", "Last Run", "Link", "Suite Name"],
                )

    # ── Auto-refresh ──────────────────────────────────────────────────────
    if auto_refresh:
        time.sleep(AUTO_REFRESH_SECONDS)
        st.session_state["suite_data_cache"] = {}
        st.rerun()


# ---------------------------------------------------------------------------
# LOGIN SCREEN
# ---------------------------------------------------------------------------

if not st.session_state["api_key"]:
    st.title("Ghost Inspector Login")

    # ── STEP 2: Name entry (only if no saved username for this API key) ──────
    if st.session_state.get("pending_credentials"):
        creds       = st.session_state["pending_credentials"]
        saved       = _load_user_config(creds["api_key"])
        _saved_name = saved.get("username", "")

        if _saved_name:
            # Returning user — skip name step and go straight to dashboard
            st.session_state.pop("pending_credentials")
            st.session_state["api_key"]                = creds["api_key"]
            st.session_state["private_cookie"]         = creds["cookie"]
            st.session_state["private_referrer"]       = creds["referrer"]
            st.session_state["username"]               = _saved_name
            st.session_state["monitored_folder_ids"]   = saved.get("monitored_folder_ids", "")
            st.session_state["hidden_suite_ids"]       = saved.get("hidden_suite_ids", "")
            st.rerun()
        else:
            # New user — ask for their name
            st.markdown("### 👋 One more step — what's your name?")
            st.caption("This will be used to track your actions in the notification board.")
            with st.form("name_form"):
                username = st.text_input("Your Name", placeholder="e.g. Ralph")
                if st.form_submit_button("Enter Dashboard", use_container_width=True):
                    if not username.strip():
                        st.error("Please enter your name.")
                    else:
                        st.session_state.pop("pending_credentials")
                        st.session_state["api_key"]                = creds["api_key"]
                        st.session_state["private_cookie"]         = creds["cookie"]
                        st.session_state["private_referrer"]       = creds["referrer"]
                        st.session_state["username"]               = username.strip()
                        st.session_state["monitored_folder_ids"]   = saved.get("monitored_folder_ids", "")
                        st.session_state["hidden_suite_ids"]       = saved.get("hidden_suite_ids", "")
                        _save_user_config(
                            creds["api_key"],
                            saved.get("monitored_folder_ids", ""),
                            saved.get("hidden_suite_ids", ""),
                            username.strip(),
                        )
                        st.rerun()
            if st.button("← Back to Login", use_container_width=False):
                st.session_state.pop("pending_credentials", None)
                st.rerun()

    # ── STEP 1: API key + optional cookie ────────────────────────────────
    else:
        st.markdown("Enter your API key to begin. Cookie and Referrer are optional but enable accurate per-test running status.")

        # Expander rendered FIRST so session state is populated before form submit
        # Initialise keys so they exist even if user never opens the expander
        if "login_cookie" not in st.session_state:
            st.session_state["login_cookie"] = ""
        if "login_referrer" not in st.session_state:
            st.session_state["login_referrer"] = "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b"

        _open_adv = st.session_state.get("open_advanced", False)
        if _open_adv:
            st.session_state["open_advanced"] = False
        with st.expander("⚙️ Advanced — Private API Headers *(for accurate RUNNING status)*", expanded=_open_adv):
            st.text_input(
                "Cookie",
                type="password",
                placeholder="Paste your browser Cookie header value…",
                key="login_cookie",
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
            st.text_input(
                "Referrer",
                value="https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b",
                key="login_referrer",
            )

        with st.form("login_form"):
            user_key = st.text_input("API Key", type="password")
            if st.form_submit_button("Start Session", use_container_width=True):
                cookie   = st.session_state.get("login_cookie", "")
                referrer = st.session_state.get("login_referrer", "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b")
                if not user_key:
                    st.error("API Key cannot be empty.")
                elif not cookie:
                    # No cookie — show warning first
                    st.session_state["pending_login"] = {
                        "api_key":  user_key,
                        "referrer": referrer or "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b",
                    }
                    st.rerun()
                else:
                    # Cookie provided — go straight to name step
                    st.session_state["pending_credentials"] = {
                        "api_key": user_key,
                        "cookie":  cookie,
                        "referrer": referrer or "https://app.ghostinspector.com/5ee71d7a55c1ab7614145b3b",
                    }
                    st.rerun()

        # ── Cookie warning ────────────────────────────────────────────────
        if st.session_state.get("pending_login"):
            st.warning(
                "⚠️ **Limited Running Status** — You haven't provided a **Cookie** header.\n\n"
                "Without it, the dashboard cannot accurately detect which individual "
                "tests are currently **RUNNING**. You will still see the suite-level "
                "running indicator, but per-test RUNNING status will not be shown.\n\n"
                "Do you want to continue anyway?"
            )
            col1, col2 = st.columns(2)
            if col1.button("✅ Continue without Cookie", use_container_width=True):
                pending = st.session_state.pop("pending_login")
                # Move to name step
                st.session_state["pending_credentials"] = {
                    "api_key":  pending["api_key"],
                    "cookie":   "",
                    "referrer": pending["referrer"],
                }
                st.rerun()
            if col2.button("🔙 Go Back", use_container_width=True):
                st.session_state.pop("pending_login")
                st.session_state["open_advanced"] = True
                st.rerun()

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
        _running_all_sidebar = st.session_state.get("running_all", False)
        view_mode    = st.radio("View Mode", ["Group by Suite", "Group by Status"],
                                disabled=_running_all_sidebar)
        auto_refresh = st.checkbox(f"Auto-Refresh ({AUTO_REFRESH_SECONDS}s)", value=False,
                                   disabled=_running_all_sidebar)

        _bc = st.session_state.get("btn_counter", 0)
        if st.button("✅ Apply & Load", use_container_width=True, key=f"apply_load_{_bc}",
                     disabled=st.session_state.get("running_all", False)):
            st.session_state["monitored_folder_ids"] = folder_input
            st.session_state["hidden_suite_ids"]     = hidden_suite_input
            st.session_state["folders_applied"]      = True
            _save_user_config(
                st.session_state["api_key"], folder_input, hidden_suite_input,
                st.session_state.get("username", ""),
            )
            get_suites_in_folder.clear()
            get_tests_in_suite.clear()
            st.session_state["suite_data_cache"] = {}
            st.rerun()

        if st.button("🔄 Manual Refresh", use_container_width=True, key=f"manual_refresh_{_bc}",
                     disabled=st.session_state.get("running_all", False)):
            st.session_state["folders_applied"] = True
            get_suites_in_folder.clear()
            get_tests_in_suite.clear()
            st.session_state["suite_data_cache"] = {}
            st.rerun()

        st.divider()
        st.markdown("**▶️ Run Suites**")
        # Show results from the last completed run (only when not running)
        _run_results = st.session_state.get("run_all_results")
        if _run_results and not st.session_state.get("running_all", False):
            if _run_results["triggered"]:
                st.success(f"✅ Triggered {len(_run_results['triggered'])} suite(s)")
                for name in _run_results["triggered"]:
                    st.markdown(f"&nbsp;&nbsp;🟢 {name}")
            if _run_results["failed"]:
                st.error(f"❌ Failed {len(_run_results['failed'])} suite(s)")
                for name in _run_results["failed"]:
                    st.markdown(f"&nbsp;&nbsp;🔴 {name}")

        _running_all = st.session_state.get("running_all", False)

        if _running_all:
            st.warning(
                "🔒 **Suite run in progress…** All controls are locked to prevent interruption.",
                icon="⏳"
            )
            _prog = st.session_state.get("run_all_progress", 0)
            _stat = st.session_state.get("run_all_status", "⏳ Preparing…")
            st.progress(_prog / 100 if _prog <= 100 else 1.0)
            st.caption(_stat)

        if st.button("🚀 Run All Suites", use_container_width=True,
                     key=f"run_all_{_bc}", disabled=_running_all):
            # Set flag and rerun FIRST so UI locks before execution starts
            st.session_state["running_all"]     = True
            st.session_state["_run_all_pending"] = True
            st.session_state["run_all_results"]  = None  # clear previous results
            st.session_state["run_all_progress"] = 0
            st.session_state["run_all_status"]   = "⏳ Preparing…"
            _bump_counter()
            st.rerun()

        # Execution happens on the rerun AFTER the UI has locked
        st.divider()
        if st.button("🚪 Logout", use_container_width=True):
            for key in ["api_key", "monitored_folder_ids", "hidden_suite_ids", "username"]:
                st.session_state[key] = "" if key != "api_key" else None
            st.rerun()

    username = st.session_state.get("username", "")
    st.title("Suite & Test Monitor")

    # ── Run All execution — OUTSIDE sidebar so progress rerenders correctly ──
    if st.session_state.get("_run_all_pending"):
        st.session_state.pop("_run_all_pending")
        raw = st.session_state.get("monitored_folder_ids", "")
        _api_key = st.session_state.get("api_key")
        folder_ids_run = [f.strip() for f in raw.split(",") if f.strip()]
        hidden = [s.strip() for s in st.session_state.get("hidden_suite_ids", "").split(",") if s.strip()]
        triggered, failed = [], []
        key_hash = _hash_key(st.session_state.get("api_key", ""))

        def _post_remark(text: str):
            ts = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
            entry = pd.DataFrame([{
                "Timestamp":  ts,
                "Author":     "System",
                "Remark":     text,
                "ApiKeyHash": key_hash,
            }])
            st.session_state["remarks_data"] = pd.concat(
                [entry, st.session_state["remarks_data"]], ignore_index=True
            )
            _save_remarks(st.session_state["remarks_data"])

        all_suites_to_run = []
        for fid in folder_ids_run:
            for suite in get_suites_in_folder(_api_key, fid):
                if suite["_id"] not in hidden:
                    all_suites_to_run.append(suite)

        total = len(all_suites_to_run)
        _post_remark(f"🚀 Started running all suites ({total} total).")

        run_status   = st.empty()
        run_progress = st.progress(0)
        run_status.info(f"⏳ Triggering **{total}** suites in parallel…")

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
                pct = int((completed / total) * 100)
                st.session_state["run_all_progress"] = pct
                run_progress.progress(pct / 100)
                run_status.info(f"⏳ Triggered **{completed}/{total}** suites…")

        run_status.empty()
        run_progress.empty()
        st.session_state["run_all_progress"] = 100
        st.session_state["run_all_status"]   = f"✅ Done — {len(triggered)} triggered, {len(failed)} failed."
        st.session_state["running_all"] = False
        st.session_state["run_all_results"] = {
            "triggered": sorted(triggered),
            "failed":    sorted(failed),
        }
        _bump_counter()
        _post_remark(f"✅ Finished running all suites — {len(triggered)} triggered, {len(failed)} failed.")
        st.rerun()
    if username:
        st.caption(f"👤 Logged in as **{username}**")

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
            /* Disabled-on-click style */
            button.btn-clicked {
                opacity: 0.5 !important;
                pointer-events: none !important;
                cursor: not-allowed !important;
            }
        </style>
        <script>
            // Attach once after DOM is ready, re-attach on Streamlit rerenders
            function attachButtonGuards() {
                // Target ALL buttons regardless of kind attribute
                document.querySelectorAll('button').forEach(btn => {
                    // Skip buttons that are already disabled or are form checkboxes
                    if (btn.dataset.guardAttached) return;
                    if (btn.disabled) return;
                    if (btn.getAttribute('role') === 'checkbox') return;
                    btn.dataset.guardAttached = "1";
                    btn.addEventListener("click", function() {
                        if (this.disabled) return; // already locked
                        // Disable immediately on first click
                        this.classList.add("btn-clicked");
                        this.disabled = true;
                        // Safety re-enable after 30s for long-running ops like Run Selected
                        const self = this;
                        setTimeout(function() {
                            self.classList.remove("btn-clicked");
                            self.disabled = false;
                            delete self.dataset.guardAttached;
                        }, 30000);
                    });
                });
            }

            // Run on load
            attachButtonGuards();

            // Re-run whenever Streamlit mutates the DOM (rerenders)
            const observer = new MutationObserver(function(mutations) {
                // Only reattach if actual nodes were added
                for (const m of mutations) {
                    if (m.addedNodes.length > 0) {
                        attachButtonGuards();
                        break;
                    }
                }
            });
            observer.observe(document.body, { childList: true, subtree: true });
        </script>
    """, unsafe_allow_html=True)

    # ── Notification / Remarks Board ──────────────────────────────────────
    with st.expander("📝 Notification Board / Remarks", expanded=True):
        with st.form("new_remark", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 4, 1])
            author  = st.session_state.get("username", "QA Team")
            c1.text_input("Author", value=author, disabled=True)
            remark  = c2.text_input("Remark", placeholder="e.g. Investigating failures in Suite A…")
            c3.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            post_btn = c3.form_submit_button("Post", use_container_width=True)

            if post_btn and remark:
                ts       = datetime.now().astimezone(SGT).strftime("%Y-%m-%d %I:%M:%S %p")
                key_hash = _hash_key(st.session_state.get("api_key", ""))
                new = pd.DataFrame([{
                    "Timestamp":  ts,
                    "Author":     author,
                    "Remark":     remark,
                    "ApiKeyHash": key_hash,
                }])
                st.session_state["remarks_data"] = pd.concat(
                    [new, st.session_state["remarks_data"]], ignore_index=True
                )
                _save_remarks(st.session_state["remarks_data"])
                # No st.rerun() — remarks display reads from session state directly

        df_remarks = st.session_state["remarks_data"]
        current_hash = _hash_key(st.session_state.get("api_key", ""))

        if not df_remarks.empty:
            # Build scrollable HTML table — max 5 rows visible, scrollable beyond
            ROW_HEIGHT_PX = 40
            MAX_VISIBLE   = 5
            table_height  = min(len(df_remarks), MAX_VISIBLE) * ROW_HEIGHT_PX + ROW_HEIGHT_PX  # +1 for header

            # Header row
            h1, h2, h3, h4 = st.columns([2, 1, 4, 1])
            h1.markdown("**Time (SGT)**")
            h2.markdown("**Author**")
            h3.markdown("**Message**")
            h4.markdown("")

            # Scrollable container via CSS
            scroll_css = f"""
                <style>
                .remarks-scroll {{
                    max-height: {MAX_VISIBLE * ROW_HEIGHT_PX}px;
                    overflow-y: auto;
                    border-top: 1px solid #333;
                }}
                </style>
                <div class="remarks-scroll"></div>
            """
            st.markdown(scroll_css, unsafe_allow_html=True)

            scroll_container = st.container(height=MAX_VISIBLE * ROW_HEIGHT_PX + 20)
            with scroll_container:
                for idx, row in df_remarks.iterrows():
                    is_system = row.get("Author", "") == "System"
                    row_hash  = row.get("ApiKeyHash", "")
                    # Use a hash of the row content as a stable unique key
                    # so deleting one row doesn't leave ghost buttons for others
                    import hashlib as _hl
                    row_uid = _hl.md5(
                        f"{row.get('Timestamp','')}{row.get('Author','')}{row.get('Remark','')}".encode()
                    ).hexdigest()[:10]
                    c1, c2, c3, c4 = st.columns([2, 1, 4, 1])
                    c1.write(row.get("Timestamp", ""))
                    c2.write(row.get("Author", ""))
                    c3.write(row.get("Remark", ""))
                    # System entries are immutable — no delete button
                    if not is_system and row_hash == current_hash:
                        if c4.button("🗑️", key=f"del_{row_uid}", help="Delete your remark"):
                            st.session_state["remarks_data"] = df_remarks.drop(index=idx).reset_index(drop=True)
                            _save_remarks(st.session_state["remarks_data"])
                            st.rerun()
        else:
            st.info("No remarks posted yet.")

    st.divider()

    # ── Parse config ──────────────────────────────────────────────────────
    folder_ids = [f.strip() for f in st.session_state["monitored_folder_ids"].split(",") if f.strip()]
    hidden_ids = [s.strip() for s in st.session_state["hidden_suite_ids"].split(",") if s.strip()]

    if not folder_ids or not st.session_state.get("folders_applied", False):
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