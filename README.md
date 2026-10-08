# HH auto bump

Local Playwright bot for the visible HH website. It uses installed Google Chrome
and its dedicated `.state/profile` directory. Passwords, cookies and browser
storage are not extracted; credentials and CAPTCHA are entered manually.

## Installation and normal launch

Python 3.11+ and installed Google Chrome are required. The project owns its
`.venv`; no runtime dependency on another project's environment remains.

```powershell
.\setup.ps1
.\run-scheduled.ps1
```

If automatic Python discovery fails, supply a working interpreter:

```powershell
.\setup.ps1 -Python 'C:\path\to\python.exe'
```

The default launch keeps one visible Chrome context and tab open across cycles.
Closing that window or stopping the console stops the service. Changes to Python
files require restarting the existing process. A second manual invocation reports
that the bot is already running (exit 3); it does not create another browser.

```powershell
.\run-scheduled.ps1 -Mode check       # inspect live card states; no resume bump
.\run-scheduled.ps1 -Mode login       # initial sign-in/account baseline
.\run-scheduled.ps1 -Mode run         # one due cycle, same due rules as service
.\run-scheduled.ps1 -Mode inspect     # save bounded local resume control metadata
.\.venv\Scripts\python.exe hh_bump.py --version
```

Known optional promotions and cookie notices may be dismissed during a check;
checks do not raise resumes or choose paid actions. The expected account is
verified by overlap with locally recorded resume IDs before each actionable
cycle. Existing `inspection.json` is migrated as the baseline. A missing or
mismatched baseline blocks actions; use explicit login to establish it.

## How the service recovers

The same card classifier is used before clicks, for confirmation and for final
verification. Exact offered controls, cooldown labels (including labels that
retain the old data-qa), visibility-restricted cards without a bump control, and
unknown states are distinct. Visibility-restricted cards are skipped only when
HH explicitly shows its exact "Make visible" control; visibility is not changed.
Unknown is never success.
Each send intent is atomically persisted before a click; confirmed and ambiguous
results are saved per resume. An uncertain action is not automatically resent
before its protective interval has expired and HH again offers the action.
Other safely identified cards can still be processed after a local card error.
Account changes, an unexpected host or an unknown blocking dialog stop the cycle.

Successful cycles are separated by at least four hours and one minute. Temporary
failures use bounded short retries; per-resume deadlines still prevent duplicate
sends. Both `run` and `service` use `.state/runtime-state.json`. Old timestamp
files are migrated. Invalid files are preserved with `.invalid-*` names once,
and a durable conservative recovery deadline is saved instead of restarting the
wait at each launch.

Startup performs an account/card health check in the same tab, even when the next
bump is not due. A fully successful check or run records its validation kind and
the digest of all production Python modules, requirements and launcher. A check
is evidence of current live selectors/account, not evidence of a live bump.
Changing production inputs invalidates that release's verification. Tests use
synthetic HTML and isolated browser contexts; they never visit HH.

## Windows Task Scheduler

After a successful current-release check or run:

```powershell
.\install-schedule.ps1             # create when absent
.\install-schedule.ps1 -Update     # migrate the verified existing task
```

The installer verifies the expected account, task folder, executable and action.
It refuses ambiguous task names, other owners, noninteractive logon and elevated
run levels. Updates replace the former repeating trigger with a logon trigger.
Only the service owns resume periodicity; Task Scheduler starts it when the user
logs on and can restart a failed process up to three times at one-minute intervals.
The process has no execution time limit; `IgnoreNew` prevents duplicate launches.
No installation immediately starts a live cycle. Start the service manually once
in the current desktop session. Scheduler failures never wait for keyboard input.

After repeated failures, inspect status and failure artifacts. Automatic process
restarts are bounded; after the supervisor's restart limit, manual intervention
or the next logon is required. Authentication/CAPTCHA or a changed unknown HH
interface can still require attention. User-closing Chrome exits normally and
does not force an immediate reopening.

## Diagnostics and tests

- `.state/events.jsonl`: structured UTF-8 events, run ID, release digest and stage.
- `.state/status.json`: stage, heartbeat, next due time and failure count.
- `.state/console/`: one UTF-8 console log per invocation, consistent in PS 5/7.
- `.state/failures/`: separate local metadata/screenshots for failures; no cookies,
  passwords or browser storage. Screenshots can contain private resume information.

Old `scheduled-console.log` remains historical and is not appended to. State,
profile, diagnostics, temporary test output and `.venv` are excluded from Git.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Get-Content .state\status.json -Encoding UTF8
```

No resume text/visibility or paid services are changed. Conversation export and
cover-letter generation for favorites are planned for a separate next stage.
