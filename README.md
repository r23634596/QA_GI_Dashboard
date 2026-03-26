# QA_GI_Dashboard
Latest Update (26/03/2026): 
Returning users skip the name entry step — loaded from saved config
Cookie/referrer fields pre-initialized to prevent race conditions
Cookie expiry detected (403) and warned to user

Dashboard

Folders only load after explicitly clicking Apply & Load
Suite data cached in session state — checkboxes and buttons no longer trigger folder reloads

Test Selection

data_editor with CheckboxColumn for both Group by Suite and Group by Status views
Select All / Clear tertiary buttons above each table
Selections persist across suites via st.session_state["selected_tests"]
Fast multi-selection fixed by reading widget state before rendering
Run Selected Tests button with progress bar

Run All Suites

Execution moved outside sidebar so progress bar actually renders
UI locks during run with warning banner and progress in sidebar
Results stored in session state and shown after rerun

Notification Board

Posting no longer triggers folder reload
Author field disabled (non-editable)
Post button aligned with fields
