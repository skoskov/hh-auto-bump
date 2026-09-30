# HH auto bump

Local Playwright browser, based on the existing profi-bot approach.
Uses a separate Chrome profile in `.state/profile`; no HH developer application,
API keys, browser extension, cookie extraction or password in source code.

Runtime currently reused from `../profi-bot/.venv/Scripts/python.exe`.
Run `hh_bump.py login` once to sign in yourself in the dedicated browser window.
The profile is saved locally and must not be shared. If `run` finds an expired
session, it keeps headed Chrome open on HH's phone/email login page for up to
30 minutes. Sign in manually in that window; once the resume cards return, the
same run continues only if at least one resume identifier matches the prior local
`inspection.json`. A different account is logged as `auth_account_mismatch` and
the run exits safely. The event log also records `auth_required`, `auth_restored`,
or `auth_timeout` (with `reason=browser_closed` if Chrome closes). Credentials are
never automated.

Commands: `login` (manual sign-in), `inspect` (local DOM summary), `check`
(read-only check of each eligible control), `run` (raise eligible resumes).
Only the observed `resume-update-button` control with exact normalized caption
is clicked, inside an ancestor with exactly one resume link. Each disappearance
is checked, then the page is reloaded to confirm no eligible buttons remain.
Successful runs are separated by at least four hours and one minute.
No text, visibility settings or paid services are changed.

Run `powershell -ExecutionPolicy Bypass -File .\install-schedule.ps1` to create
the Windows task after a successful live run. It requires an interactive Windows
login and resumes a missed trigger when the computer becomes available. To apply
an updated script/settings to the existing task, run
`powershell -ExecutionPolicy Bypass -File .\install-schedule.ps1 -Update` from
this directory. The update path changes the existing task in place only after
confirming its sole action is the expected Python executable and this
`hh_bump.py run` script; it does not create a replacement or duplicate. If that
check fails, inspect the task in Task Scheduler before taking further action.
Turn it off using Task Scheduler: `HH-Resume-AutoBump` -> Disable.
Errors and counts are in `.state/events.jsonl`; login/captcha requires manual
attention. Do not share the profile directory. Updates to HH may require
updating the DOM selectors. Python currently depends on the Profi-bot venv.

Implementation note: model-route is available and its policy was checked.
The 2026-09-25 auth-recovery implementation used the configured fix role and
received an independent static review. `model-route verify` returned UNVERIFIED
because the spawned session metadata did not contain the required `agent_path`;
the routing verifier failure is recorded rather than treated as a pass.
The reported stable-target click issue was fixed using the original element handle.
Initial read-only verification found 14 eligible controls for 14 resumes.
Live verification on 2026-09-09 confirmed all 14 raised across recovery runs,
with zero eligible controls after reload. The optional Setka promotion is declined
using its observed 'Потом' button; no cross-posting is accepted.
