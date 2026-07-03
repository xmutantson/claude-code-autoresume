# autoresume — Claude Code auto-resume on usage-limit reset (Windows)

When a Claude Code session on a Max plan hits a usage limit (the rolling
**session** window or the **weekly** window), the session stalls until the limit
resets. `autoresume` polls the **authoritative usage endpoint** on a **fast fixed
cadence (~30 s)** to see which quota is blocked and exactly **when** it resets,
**arms** the moment a real hit is observed, then **fires the instant the window
clears** (or at the reset time) by **typing a plain-language resume message into
the focused Claude Code window and pressing Enter** — so unattended autonomous
work continues on its own.

Detection uses the same data source as the **Usage Monitor for Claude**
(`GET https://api.anthropic.com/api/oauth/usage`); the detection approach is
adapted from **jens-duttke/usage-monitor-for-claude** (MIT). autoresume is
fully self-sufficient — it polls the endpoint **directly and continuously** and
does **not** depend on the monitor. A legacy **transcript watcher** remains as a
fallback for machines with no readable credentials.

It replaces a blunt predecessor (see "Replaces the old AHK script" below) that
blindly pressed Enter at a fixed 1:15 am.

> **Version 0.3.0** — **fast direct polling + an arm-on-hit / fire-on-reset
> state machine, plus a manual resume-time control.**
> - **Fast direct polling.** autoresume now polls the usage endpoint **itself,
>   every ~30 s** (`--poll-interval`), *regardless* of whether the Usage Monitor
>   app is running. The old ~15-min courtesy backoff is **gone**: with a 15-min
>   blind gap a limit could be **hit and reset inside one gap**, so the reset was
>   missed and dead work never resumed. Continuous fast polling closes that hole.
> - **Arm-on-hit / fire-on-reset.** When a focused limit window is *observed* at
>   ≥ 100 % (a real **HIT**) autoresume **ARMS** and persists `{armed, window,
>   resets_at}` to the state file. While armed it keeps polling and **FIRES** the
>   moment the armed window **clears** (utilization back below the block line) **or**
>   the reset time is reached — then disarms. It fires **only** if a hit was
>   observed: a window sitting at 0 % with **no prior observed hit never fires**.
>   The armed state **survives a restart**, so a reset that lands while the watcher
>   is briefly down is still picked up on the next poll.
> - **Manual resume time.** Set an explicit time in the **GUI** ("Resume at
>   HH:MM" + Set/Clear, with a *manual-only* toggle) or via **`--resume-at`**
>   (`HH:MM | +Nm | ISO`). It fires at that time regardless of API detection —
>   handy when an error message already states the reset (“resets 3:10pm”).
>   Auto-detection stays on as a backstop unless *manual-only* is set.
>
> **Version 0.2.0** — usage-API detection became the **primary** source
> (`--source auto` uses it whenever credentials are readable); the transcript
> watcher is a documented fallback. In `--source usage-api` no transcript text is
> ever read, so the transcript self-contamination false-arm class is impossible.

The injected message tells the agent, in the owner's voice:

> `[AUTOMATED RESUME] A <session|weekly> usage limit was hit and has now reset
> (reset reported: <RESET_TIME>). This is an automated message; the user is away
> and will NOT see or respond to your output. Continue the autonomous work from
> where you left off: consult _research/AUTONOMOUS_WORKLOG.md and the current
> todo list, and keep going. Do not wait for user input.`

---

## How it works

```
poll /api/oauth/usage every ~30s ─▶ blocked? (percent ≥ 100) ─▶ ARM + persist
        ▲                                                              │
        │                        keep polling (fire-on-clear)          ▼
        └── loop ◀ inject msg+Enter ◀ window CLEARED  or  reset time reached ✓
```

1. **Poll the usage endpoint — fast and direct.**
   `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token, on a
   **fixed ~30 s cadence** (`--poll-interval`), **always** — no monitor-aware
   backoff. All credential + network code lives in `usage_api.py`; **the token is
   never logged, printed, or stored.**

2. **Decide blocked.** A quota is **blocked** when its `percent` / `utilization`
   is **≥ 100** — checked across both the `limits[]` array and the top-level
   `five_hour` / `seven_day*` objects. Severity can escalate
   `normal → warning → critical → exhausted`, but **≥ 100 is the definitive
   block**: at e.g. 91 % / *critical* autoresume does **not** arm.

3. **Arm on the observed hit (and persist it).** The first time a window is
   *observed* at ≥ 100 %, autoresume **ARMS**: it records `{window, resets_at,
   fire_at}` to `state.json`. The scheduled `fire_at` is `resets_at` (exact
   ISO 8601 UTC) **+ a small buffer** (default +45 s, for server clock lag).
   Because the arm is persisted, a **restart resumes it** — and a reset that
   landed while the watcher was briefly down still fires on the next poll. When
   several quotas block, the **latest** reset wins. Spend / monthly caps have no
   timed reset → logged "cannot auto-resume" and skipped.

4. **Fire on clear (or at reset time).** While armed, autoresume keeps polling on
   the same fast cadence and **fires the instant the armed window clears** — its
   utilization drops back below the block line (a reset drops the window to ~0) —
   **or** when `fire_at` is reached. It **never resumes into a still-blocked
   account**: if the reset time has passed but the server still reports the window
   ≥ 100 %, it waits and re-checks. Crucially it fires **only if a hit was
   observed** (armed): a window at 0 % with no prior hit **never** fires.

5. **Dedup + inject.** Each reset collapses to a `(KIND, reset-minute)` key; a
   persisted watermark (`state.json`) ensures **exactly one** injection per reset,
   even across restarts. To inject it picks the target window (**foreground-first**,
   see below), verifies it, types the message as literal Unicode keystrokes, and
   presses Enter.

6. **Manual override.** A manual resume time (GUI / `--resume-at`) fires at a set
   wall-clock time regardless of API detection; see "Manual resume time" below.

7. **Log & loop.** Every poll decision and action (or abort, with reason) is
   appended to `autoresume.log`; then it returns to watching for the next limit.

### Why the usage API (not the transcript)

The Anthropic **developer API** returns rate-limit reset info only in response
**headers** for API-key usage — those are **not** the Max-plan subscription
session/weekly limits. But the Claude Code app authenticates with an **OAuth
token** (cached under `~/.claude/.credentials.json`) that *can* query the
subscription usage endpoint `GET /api/oauth/usage` — the same endpoint the Usage
Monitor for Claude uses. That returns live `utilization` / `percent` and exact
`resets_at` timestamps for every quota, so autoresume can detect a block and its
reset **authoritatively**, without scraping transcript text. (The earlier
transcript-text detector remains available as `--source transcript`.)

### Credentials & the usage endpoint

| item | value |
|------|-------|
| endpoint | `GET https://api.anthropic.com/api/oauth/usage` |
| token | `["claudeAiOauth"]["accessToken"]` from `$CLAUDE_CONFIG_DIR/.credentials.json` (else `~/.claude/.credentials.json`) — override with `--cred-path` |
| headers | `Authorization: Bearer <token>`, `Content-Type: application/json`, `User-Agent: claude-code/<CLI version>`, `anthropic-beta: oauth-2025-04-20` |
| errors | `401` auth expired (logged; `--on-auth-expired update` can run `claude update`); `429` rate-limited (respects `Retry-After`, backs off); `5xx` / connection → retry |

The token is read fresh on every request (so a token the CLI refreshes is picked
up), used **only** in the `Authorization` header, and never written anywhere.

### Fast direct polling (no monitor dependency)

autoresume polls the usage endpoint **directly and continuously** at
`--poll-interval` (**default 30 s**), whether or not the **Usage Monitor for
Claude** app is running. The Usage Monitor's presence is only **logged** (for
information); it never changes the cadence. This is a deliberate change from
v0.2.0's ~15-min courtesy backoff, which had a real hole: a limit could be **hit
and reset inside one 15-min blind gap**, so the reset was missed and dead work
never resumed. Polling itself, fast and continuously, is what closes that hole —
combined with the arm-on-hit / fire-on-clear machine above, an observed hit is
followed to its reset within one poll.

### Manual resume time (GUI / `--resume-at`)

Sometimes the reset time is already known from the on-screen error (“resets
3:10pm”). You can schedule a resume for a specific time regardless of API
detection:

- **GUI:** type into the **“Resume at”** box (`HH:MM`, `+Nm`, or an ISO
  timestamp), click **Set**; a status line shows the scheduled time. **Clear**
  cancels it. A **“manual only”** checkbox suppresses auto-detection while set;
  left unchecked, auto-detection stays on as a **backstop**.
- **CLI:** `--resume-at HH:MM|+Nm|ISO` (with optional `--manual-only`). `HH:MM`
  resolves to the next future occurrence; `+Nm` is minutes from now (`+Nh`/`+Ns`
  too).

The GUI and CLI share one **manual-request file** (default
`%TEMP%\autoresume.manual.json`, `--manual-file`) — the same filesystem-IPC
pattern as the kill-switch stop-file — so a manual time is honoured headless and
across restarts, and fires exactly once (deduped like an auto reset).

### Source selection (`--source`)

| `--source` | behaviour |
|------------|-----------|
| `auto` *(default)* | usage-API if credentials are readable, else the transcript fallback |
| `usage-api` | force the usage-API detector (falls back to transcript if no credentials, so an unattended watcher is never left dead) |
| `transcript` | force the legacy transcript watcher (see "Transcript watcher (fallback)" below) |

---

## Injection mechanism & fail-safes

### `--injector uri` — target the EXACT session tab *(default, v0.4.0+)*

One VS Code window can hold **multiple** Claude Code session tabs. The keystroke
injector below focuses a *window* and types into whichever tab is active — so it
can land in the **wrong conversation**. The URI injector fixes this natively.
Claude Code's VS Code extension exposes a deep link
([docs](https://code.claude.com/docs/en/vs-code#launch-a-vs-code-tab-from-other-tools)):

```
vscode://anthropic.claude-code/open?session=<SESSION_ID>&prompt=<URL_ENCODED_MESSAGE>
```

- `session=<ID>` **focuses that exact tab** (opening it in the focused window's
  workspace if it isn't already open) — precise, no tab guessing.
- `prompt=<...>` **pre-fills** the chat input. It is **not** auto-submitted, so
  autoresume sends exactly **one** Enter to submit (reusing the correct-window
  guard, so it never Enters into a non-VS-Code window).

What autoresume does per fire:

1. Derive the target — **session id** = the newest (or tailed) transcript's
   filename minus `.jsonl`; **workspace folder** = `basename` of that session's
   most-recent `cwd` field (the substring that appears in the VS Code window
   title). Override either with `--session-id` / `--workspace-title`.
2. Foreground the correct VS Code window (so the session opens in the right
   workspace — a session id from *another* workspace would start a fresh chat).
3. Fire the URI via `ShellExecuteW(open)` (ctypes; **not** a shell string).
4. Settle ~0.5 s for the tab to focus + pre-fill, then send one guarded Enter.

**Caveats (handled / documented):**

- **Pre-fill is not submitted** by the URI itself → the single Enter submits it.
  If a one-time confirmation (*"open external app?" / "switch apps?"*) intercepts
  that first Enter, pass **`--uri-extra-enter`** to send a second Enter, or
  (recommended) tick **"always allow"** once so the prompt never reappears.
- **The session must belong to the focused window's workspace**, else the
  extension opens a **fresh** conversation — so window targeting still matters
  (step 2). `--workspace-title` forces the disambiguator; `basename(cwd)` is a
  heuristic (it can read a *subfolder* name if the session `cd`'d into one — but
  it usually substring-matches the workspace title anyway, and `session=<id>`
  still focuses the exact tab regardless of which window is foregrounded).
- **Requires a recent extension build** that understands the `session` param. On
  an older build the URI is a no-op (nothing pre-fills); the fire is logged so a
  silent no-op is diagnosable. Fall back with `--injector win32`.
- `--dry-run` (watch) and `inject-now --dry-run` **log the exact URI** without
  firing it — verify the `session=` and url-encoded `prompt=` before going live.

`--injector win32` restores the legacy keystroke path below; `--injector ahk`
uses the AutoHotkey fallback.

### `--injector win32` — legacy keystroke path *(fallback)*

**Mechanism:** pure Win32 `SendInput` via `ctypes` — no third-party packages. It
targets **the window currently in focus** (owner: "whichever is currently in
focus") and types into it. Window pick, in order:

1. `--prefer-title` override — if set and a VS Code window's title matches, use it.
2. The **current foreground window**, if it is a Claude Code / VS Code window
   (`Code.exe`, title contains `Visual Studio Code`).
3. If exactly one VS Code window exists, use it.
4. Otherwise best-effort: the most-recently-active VS Code window, with the
   ambiguity **logged**.

It then brings that window forward (defeating the Windows foreground lock via
`AttachThreadInput` + a zeroed foreground-lock timeout + an ALT tap), **focuses
the Claude Code chat input** (see below), then types the message and Enter.

### Focusing the Claude Code chat input (not the terminal)

The owner runs the **Claude Code VS Code extension** — a chat *panel*, not the
integrated terminal (`claudeCode.useTerminal` is unset → default `false`).
**Foregrounding `Code.exe` does not put keyboard focus into the chat input**, so
after the correct-window guard passes, autoresume sends an explicit focus gesture
before typing. Two selectable methods (`--focus-method`, default `keybind`):

| method | what it does | needs config? |
|--------|--------------|---------------|
| `keybind` *(default)* | SendInput **`Ctrl+Alt+Shift+K`**, a dedicated user keybinding bound to the extension command **`claude-vscode.focus`** | yes — one line in VS Code `keybindings.json` (below) |
| `palette` | SendInput `Ctrl+Shift+P`, type `Claude Code: Focus input`, Enter | no (zero-config fallback) |
| `none` | send no focus gesture (used by the hermetic injection self-test) | — |

The required keybinding (added to
`%APPDATA%\Code\User\keybindings.json`, preserving existing entries):

```json
{ "key": "ctrl+alt+shift+k", "command": "claude-vscode.focus" }
```

It is deployed with **no `when` clause** on purpose: it always focuses the input
and can never toggle to *blur*. `Ctrl+Alt+Shift+K` collides with no OS or VS Code
default binding.

> **Why not the extension's built-in `Ctrl+Esc`?** `Ctrl+Esc` is the Windows
> Start-menu shell hotkey, and the extension binds it as an
> `editorTextFocus`-gated **toggle** — when the input already has focus it fires
> `claude-vscode.blur` instead (the opposite of what we want). A raw `SendInput`
> of `Ctrl+Esc` is therefore unreliable (leaks to Start / toggles to blur). The
> dedicated, ungated keybinding above is deterministic. To switch methods, pass
> `--focus-method palette` (needs no keybindings.json edit) or set the
> `FOCUS_METHOD` constant in `autoresume.py`.

**Fallback:** `inject.ahk` (AutoHotkey v2, the owner's toolchain) does the same
`WinActivate` → `SendText` → `{Enter}`. Run the watcher with `--injector ahk` to
use it. (In testing on this machine the Python `SendInput` path was the more
reliable of the two into hard input surfaces, so it is the default.)

Fail-safes, all implemented:

- **Fire only on an observed hit.** autoresume arms **only** after seeing a window
  at ≥ 100 %. A window at 0 % with no prior observed hit — a benign reset — is
  **never** injected. (Tested: `test_state_machine.py::test_b_benign_reset_never_fires`.)
- **Fire-on-clear / never into a blocked account.** While armed it re-polls on the
  fast cadence and injects the moment the window **clears** (utilization back below
  the block line) or the reset time arrives. If the reset time passes but the
  server still reports the window ≥ 100 %, it **waits and re-checks** — it never
  resumes into a still-blocked account.
- **Persisted arm survives a restart.** The armed `{window, resets_at, fire_at}`
  is written to `state.json`; a restart reloads it, so a reset that lands during a
  brief downtime still fires on the next poll.
- **Correct-window guard.** It types **only** if the *foreground* window is the
  target (right process **and** title). If activation fails or another app is in
  front, it **aborts and logs** — it never sprays a paragraph into the wrong
  window. (A stray Enter was cheap for the old script; a stray paragraph is not.)
- **Exactly once per reset.** The `(KIND, reset-minute)` watermark in `state.json`
  guarantees a single injection per reset, across restarts, ignoring the retry
  storm. A manual resume dedups the same way (`manual|<minute>`).
- **No spam loop.** A global minimum interval between any two injections
  (default 60 s) as a backstop.
- **Reset buffer.** An auto reset fires at reset **+ buffer** (default 45 s), never
  exactly on the reset instant. (A manual `--resume-at` fires at the set time.)
- **Kill switch.** If the stop-file (`%TEMP%\autoresume.stop`) exists, detection
  and logging continue but injection (auto **and** manual) is **held** until the
  file is removed.
- **Spend / monthly caps are never injected** — a billing cap has no timed reset;
  it is logged as "cannot auto-resume" and skipped.
- **Token safety.** All credential + network code is isolated in `usage_api.py`;
  the OAuth token is never logged, printed, or stored.
- **Full logging** of every poll decision (quota, percent, resolved reset) and
  every action (target window title + why, injected/held/aborted-and-why).

State, log, kill-switch, and manual-request locations:

| item | default path |
|------|--------------|
| state (watermark **+ persisted arm**) | `%LOCALAPPDATA%\claude-autoresume\state.json` |
| log | `%LOCALAPPDATA%\claude-autoresume\autoresume.log` |
| kill switch | `%TEMP%\autoresume.stop` |
| manual resume request | `%TEMP%\autoresume.manual.json` |

---

## Requirements

- Windows, Python 3.9+ (developed on 3.13). Standard library only (`urllib`,
  `json`, `datetime`, `ctypes`), plus `tzdata` (already installed) so `zoneinfo`
  has the IANA database on Windows — used only by the transcript fallback.
- A readable Claude OAuth credential at `~/.claude/.credentials.json` (or
  `$CLAUDE_CONFIG_DIR/.credentials.json`) for the primary usage-API source. If
  none is readable, `--source auto` falls back to the transcript watcher.
- Claude Code running as the **VS Code extension** (chat panel — the owner's
  setup). The default **`--injector uri`** targets the exact session tab via the
  extension's deep link and needs **no keybinding**. The `ctrl+alt+shift+k` →
  `claude-vscode.focus` keybinding is only required for the legacy
  **`--injector win32`** path with `--focus-method keybind` (see "Focusing the
  Claude Code chat input" above); otherwise use `--focus-method palette`.
- The **Usage Monitor for Claude** app is **not required** and no longer changes
  autoresume's behaviour (its presence is only logged); autoresume polls the
  endpoint directly. Optional: AutoHotkey v2 (for `--injector ahk`), at
  `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe`.

---

## How to run

Normal use — just start the watcher and leave it running:

```bash
python autoresume.py watch
```

It logs to `%LOCALAPPDATA%\claude-autoresume\autoresume.log` and the console.
Leave the Claude Code / VS Code window focused (as with the old script). To stop
it, Ctrl-C, or create the kill-switch file to pause injection:

```bash
# pause injection (detection keeps logging)
touch "$TEMP/autoresume.stop"     # or: type nul > %TEMP%\autoresume.stop
# resume
rm "$TEMP/autoresume.stop"
```

Useful options (`watch`):

| flag | default | meaning |
|------|---------|---------|
| `--source {auto,usage-api,transcript}` | auto | detection source (see table above) |
| `--buffer N` | 45 | seconds after reset before injecting (auto) |
| `--poll-interval N` (alias `--usage-poll`) | 30 | **direct** usage-endpoint poll cadence (s); also the fire-on-clear re-poll cadence while armed |
| `--resume-at HH:MM\|+Nm\|ISO` | (none) | **manual** resume time — fire at this time regardless of API detection |
| `--manual-only` | off | fire only the manual time; suppress auto-detection while set |
| `--manual-file PATH` | `%TEMP%\autoresume.manual.json` | shared GUI/CLI manual-request file |
| `--monitor-poll N` | 900 | **deprecated / ignored** (no monitor backoff any more) |
| `--confirm-interval N` | 30 | re-poll cadence to confirm a reset before injecting |
| `--confirm-below PCT` | 90 | utilization must drop below this to count as cleared |
| `--on-auth-expired {log,update}` | log | on HTTP 401: log, or run `claude update` once |
| `--cred-path PATH` | auto | override the credentials file path |
| `--injector {uri,win32,ahk}` | **uri** | injection mechanism: `uri` targets the exact session tab via the deep link (default); `win32` = legacy keystroke; `ahk` = AutoHotkey |
| `--session-id ID` | (auto) | uri: force the Claude Code session id (default: derive from the newest/tailed transcript) |
| `--workspace-title SUBSTR` | (auto) | uri: force the VS Code workspace-folder substring used to focus the right window (default: `basename` of the session's `cwd`) |
| `--uri-extra-enter` | off | uri: send a **second** Enter (for setups where a one-time "open external app?" confirmation eats the first) |
| `--focus-method {keybind,palette,none}` | keybind | `win32` injector only: how to focus the Claude Code chat input before typing |
| `--prefer-title SUBSTR` | (none) | override: force this VS Code window as the target |
| `--dry-run` | off | detect + log, but **never** type |
| `--target-proc` / `--target-title` | `Code.exe` / `Visual Studio Code` | window match |
| `--poll N` | 5 | loop tick (s): kill-switch / GUI responsiveness |
| `--watch-dir DIR` | auto-detected project dir | transcript fallback: where to tail |
| `--from-start` | off (tail from EOF) | transcript fallback: re-scan the whole file |

Schedule a manual resume from the CLI (e.g. the error said “resets 3:10pm”):

```bash
python autoresume.py watch --resume-at 15:10          # fire at 3:10pm (auto stays on as backstop)
python autoresume.py watch --resume-at +90m --manual-only   # 90 min from now, manual only
```

Run it once at logon (Task Scheduler → "At log on" → Program `pythonw.exe`,
arguments the full path to `autoresume.py` plus `watch`), or from a terminal.

### Status window (GUI)

`python autoresume.py watch --gui` (or double-click `autoresume-gui.vbs` /
`autoresume_gui.pyw` for a no-console launch) shows a small always-on-top status
window over the **same** watch loop (it does not fork any detection/inject logic).
It shows the current state (WATCHING / PENDING / FIRING / DONE / HELD), a live
**countdown** to the fire time, and the last log line. Controls:

- **Pause / Resume** — toggles the kill-switch stop-file (holds injection).
- **Cancel reset** — drops the current armed reset (won’t fire).
- **Inject now** — fires the pending resume immediately (bypasses the countdown).
- **Resume at `[ HH:MM | +Nm | ISO ]` · Set · Clear** — the **manual resume-time**
  control. **Set** schedules a resume at that wall-clock time (writing the shared
  manual-request file the loop reads); a status line shows the scheduled time.
  **Clear** cancels it. The **“manual only”** checkbox suppresses auto-detection
  while a manual time is set; unchecked, auto-detection stays on as a backstop.

All GUI controls talk to the watch loop the same way the loop already worked:
Pause and the manual time via small files (`autoresume.stop` /
`autoresume.manual.json`), Cancel/Inject-now via in-process request flags — so
the actual injection always stays the loop’s single guarded path.

### Sub-commands for testing

```bash
# Parse limit-hit lines from a transcript/fixture and print KIND + resolved reset
python autoresume.py parse-file tests/fixtures/sample_limit_hits.jsonl

# Show the exact deep-link URI that WOULD fire (no fire) for a session — verify
# the session= id and the url-encoded prompt= before going live
python autoresume.py inject-now --injector uri --dry-run \
  --session-id "<SESSION_UUID>" --workspace-title "my repo" "hello from autoresume"

# Fire the URI live but do NOT submit (pre-fill only): focuses the exact tab
python autoresume.py inject-now --injector uri --no-enter \
  --session-id "<SESSION_UUID>" --workspace-title "my repo" "hello from autoresume"

# Legacy keystroke path (test into e.g. Notepad)
python autoresume.py inject-now --injector win32 "hello from autoresume" \
  --target-title "Notepad" --target-proc Notepad.exe
```

---

## Tests

```bash
python -m pytest tests/ -q      # usage-API + state-machine + GUI-derivation suite
```

The pytest suite (`tests/test_usage_api.py`, `tests/test_state_machine.py`,
`tests/test_gui.py`) covers detection with **mocked** responses — no real
network, no token:

- **89 % / 38 % → arm NONE** (and 91 % / *critical*) — reproduces the false-arm
  as a passing regression.
- **100 % / exhausted → arms**, with `fire_at == resets_at + buffer`.
- **blocked → reset → injects exactly once** (driven through the real
  `run_watch` loop with a fake clock, dry-run).
- **still blocked at fire time → waits / re-checks, never injects.**
- **no credentials → `auto` falls back to the transcript source.**
- **ISO 8601 `resets_at` tz parsing** (offset, microseconds, `Z`, non-UTC).
- **monitor running does NOT slow the poll cadence** (v0.3.0 removed the backoff).

The v0.3.0 **state machine + manual mode** tests (`tests/test_state_machine.py`)
drive a clock-aware mocked usage client through the real loop:

- **(a) hit → reset ARMS then FIRES once** (fires on the observed clear).
- **(b) benign reset (window < 100 % throughout, no hit) NEVER fires.**
- **(c) a GUI-set manual time FIRES at that time** (and not before).
- **(d) arm-state PERSISTS across a simulated restart** — a reset that landed
  during downtime fires on the next poll.
- **`parse_resume_at`** for `HH:MM` / `+Nm` / ISO, and the manual-request-file
  round-trip.

The GUI **manual-resume control** is exercised end-to-end by the opt-in smoke
test (`python tests/test_gui.py --smoke`): it types into the “Resume at” box,
clicks **Set**, and asserts the shared manual-request file is written (and
**Clear** removes it) — the same file the watch loop consumes.

The transcript-fallback / injection harnesses run standalone (not pytest-collected):

```bash
python tests/test_parse.py      # transcript KIND + reset-time + tz math + dedup
python tests/test_inject.py     # end-to-end injection into a throwaway window
python tests/test_guard.py      # correct-window guard fail-safes
```

- `test_parse.py` runs against `tests/fixtures/sample_limit_hits.jsonl`, a
  byte-faithful fixture built from the owner's **real** transcript lines (session
  `12am`, weekly `Jul 3, 1am` and `Jun 12, 1am`, monthly-spend), plus negatives
  (transient overload, a normal assistant line) that must **not** match.
- `test_inject.py` builds a real titled top-level window with a focused child
  `EDIT` control (mirroring VS Code = a titled window whose focused child is the
  terminal), drives the actual guarded injection path, and reads the control's
  text back to prove every character and the submitting Enter landed. A classic
  `EDIT` is used rather than the Win11 Notepad because Notepad's DirectWrite
  RichEdit garbles fast synthetic input and restores stale sessions, making it a
  non-deterministic harness. (Injection into a live Notepad was also exercised
  during development and lands the full message + Enter.)
- `test_guard.py` proves the guard aborts when there is no target window and when
  the foreground window's title/process do not match, and accepts only the
  correct foreground window.

**What can and cannot be validated without a real reset:** the parse (KIND +
reset + tz), the dedup/watermark, the kill switch, the full watch→schedule→fire
pipeline (via `--dry-run`), and the keystroke injection are all tested now. The
only step that only fully validates on the **next real limit reset** is the live
Claude Code chat input accepting the focus gesture + typed line + Enter — but the
keystroke path is the identical `SendInput` path proven by `test_inject.py`, and
the focus command ID / keybinding are verified statically (command exists in the
extension; `keybindings.json` is valid). To smoke-test focus + typing end-to-end
without submitting into the live session, run
`python autoresume.py inject-now "<harmless probe>" --no-enter` while the Claude
Code panel is open, watch it focus and land the probe, then clear it (no Enter).

---

## Replaces the old AHK script

This supersedes the classic fixed-time blind-Enter AutoHotkey v2 hack,
which looped every 20 s and, at exactly `A_Hour=1 && A_Min=15`, sent a single
blind `{Enter}` to whatever window happened to have focus, then exited.

Improvements:

| old script | autoresume |
|------------|------------|
| fires at a hard-coded 1:15 am | fires at the **actual** `resets_at` from the usage API, re-confirmed |
| blind `{Enter}` | a full, plain-language **resume message** + Enter |
| no window targeting | targets the **focused** VS Code window + correct-window guard |
| no idea which limit / whether one was even hit | reads live `utilization`; knows **which** quota is blocked, resumes only recoverable ones |
| could fire when no limit was hit | arms **only** when a quota is ≥ 100 %, deduped once per reset |
| — | kill switch, logging, once-per-reset watermark, fast direct polling, persisted arm-on-hit/fire-on-clear, manual resume time |

You can leave the old `.ahk` disabled/removed once this is running at logon.

---

## Transcript watcher (fallback)

Before the usage API, autoresume detected limits by tailing the Claude Code
session transcript (`%USERPROFILE%\.claude\projects\<project>\*.jsonl`) for the
limit-hit line (`type=="assistant"`, `isApiErrorMessage==true`,
`error=="rate_limit"`, text `You've hit your (session|weekly|monthly spend)
limit … resets <time> (<tz>)`) and resolving the reset in the account timezone
with `zoneinfo`. This path is still available with **`--source transcript`** (and
is what `--source auto` uses when no credential is readable), but it is no longer
the default: matching on transcript **text** can false-arm on quoted/replayed
lines (e.g. fixtures or docs the session itself prints), which is exactly why the
usage-API source — which reads structured `utilization` numbers, never text — is
now primary. In `--source usage-api` **no transcript text is ever read**, so that
false-arm class is impossible.

---

## Credits

- Usage-API detection approach adapted from
  **[jens-duttke/usage-monitor-for-claude](https://github.com/jens-duttke/usage-monitor-for-claude)**
  (MIT) — the endpoint (`GET /api/oauth/usage`), the OAuth-token headers, and the
  error handling / reset-confirm pattern follow its `api.py` isolation.

## License

MIT — see [LICENSE](LICENSE).
