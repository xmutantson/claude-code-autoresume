# autoresume — Claude Code auto-resume on usage-limit reset (Windows)

When a Claude Code session on a Max plan hits a usage limit (the rolling
**session** window or the **weekly** window), the session stalls until the limit
resets. `autoresume` polls the **authoritative usage endpoint** to see which
quota is blocked and exactly **when** it resets, waits until the reset (plus a
small buffer), **re-confirms the reset was actually applied**, then **types a
plain-language resume message into the focused Claude Code window and presses
Enter** — so unattended autonomous work continues on its own.

Detection uses the same data source as the **Usage Monitor for Claude**
(`GET https://api.anthropic.com/api/oauth/usage`); the detection approach is
adapted from **jens-duttke/usage-monitor-for-claude** (MIT). autoresume is
self-sufficient (it polls on its own), and **courteous** — if the Usage Monitor
app is already running it backs off its own polling so it does not double-hit the
API. A legacy **transcript watcher** remains as a fallback for machines with no
readable credentials.

It replaces a blunt predecessor (see "Replaces the old AHK script" below) that
blindly pressed Enter at a fixed 1:15 am.

> **Version 0.2.0** — usage-API detection is now the **primary** source
> (`--source auto` uses it whenever credentials are readable). The transcript
> watcher is demoted to a documented fallback. This removes the transcript
> self-contamination false-arm class from the default path entirely: in
> `--source usage-api` no transcript text is ever read.

The injected message tells the agent, in the owner's voice:

> `[AUTOMATED RESUME] A <session|weekly> usage limit was hit and has now reset
> (reset reported: <RESET_TIME>). This is an automated message; the user is away
> and will NOT see or respond to your output. Continue the autonomous work from
> where you left off: consult _research/AUTONOMOUS_WORKLOG.md and the current
> todo list, and keep going. Do not wait for user input.`

---

## How it works

```
poll /api/oauth/usage ─▶ blocked? (percent ≥ 100) ─▶ schedule reset_at + buffer
        ▲                                                          │
        │                                          re-poll to CONFIRM reset
        └──────── loop ◀── inject msg+Enter ◀── (utilization dropped) ✓
```

1. **Poll the usage endpoint.** `GET https://api.anthropic.com/api/oauth/usage`
   with the OAuth token, on a ~150–180 s cadence (backing off to ~15 min while
   the Usage Monitor app is running). All credential + network code lives in
   `usage_api.py`; **the token is never logged, printed, or stored.**

2. **Decide blocked.** A quota is **blocked** when its `percent` / `utilization`
   is **≥ 100** — checked across both the `limits[]` array and the top-level
   `five_hour` / `seven_day*` objects. Severity can escalate
   `normal → warning → critical → exhausted`, but **≥ 100 is the definitive
   block**: at e.g. 91 % / *critical* autoresume does **not** arm. A real HTTP
   429 on the usage GET is also treated as blocked (respecting `Retry-After`).

3. **Schedule.** The quota's `resets_at` (exact ISO 8601 UTC) + a small buffer
   (default +45 s, for server clock lag) is the fire time. When several quotas
   are blocked, the **latest** reset wins (the account is limited until the last
   one clears). Spend / monthly caps have **no timed reset** → logged as "cannot
   auto-resume" and skipped.

4. **Dedup.** Each block collapses to a `(KIND, reset-minute)` key; a persisted
   watermark (`state.json`) ensures **exactly one** injection per reset, even
   across restarts.

5. **Confirm, then inject.** At the fire time autoresume **re-polls** and only
   injects once the quota's utilization has actually dropped below ~90 % (the
   server can apply the reset a little late); if not, it re-checks every
   `--confirm-interval` s until it does. Then it picks the target window
   (**foreground-first**, see below), verifies it, types the message as literal
   Unicode keystrokes, and presses Enter.

6. **Log & loop.** Every poll decision and action (or abort, with reason) is
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

### Monitor-awareness ("use the monitor if it's running, else run on its own")

autoresume is always self-sufficient, but **courteous**: it detects the
`UsageMonitorForClaude.exe` process (via `tasklist`, no third-party deps) and, if
present, backs its own polling off to `--monitor-poll` (~15 min) so the two do
not double-hit the API. When the monitor is not running it polls at
`--usage-poll` (~150–180 s). Either way autoresume needs nothing from the
monitor — it reads the same endpoint directly.

### Source selection (`--source`)

| `--source` | behaviour |
|------------|-----------|
| `auto` *(default)* | usage-API if credentials are readable, else the transcript fallback |
| `usage-api` | force the usage-API detector (falls back to transcript if no credentials, so an unattended watcher is never left dead) |
| `transcript` | force the legacy transcript watcher (see "Transcript watcher (fallback)" below) |

---

## Injection mechanism & fail-safes

**Primary:** pure Win32 `SendInput` via `ctypes` — no third-party packages. It
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

- **Reset re-confirm (usage-API).** At the fire time autoresume re-polls the
  usage endpoint and injects **only** once the quota's utilization has actually
  dropped below ~90 %. If the server has not applied the reset yet it re-checks
  every `--confirm-interval` s — it never resumes into a still-blocked account.
- **Correct-window guard.** It types **only** if the *foreground* window is the
  target (right process **and** title). If activation fails or another app is in
  front, it **aborts and logs** — it never sprays a paragraph into the wrong
  window. (A stray Enter was cheap for the old script; a stray paragraph is not.)
- **Exactly once per reset.** The `(KIND, reset-minute)` watermark in `state.json`
  guarantees a single injection per reset, across restarts, ignoring the retry
  storm.
- **No spam loop.** A global minimum interval between any two injections
  (default 60 s) as a backstop.
- **Reset buffer.** Fires at reset **+ buffer** (default 45 s), never exactly on
  the reset instant.
- **Kill switch.** If the stop-file (`%TEMP%\autoresume.stop`) exists, detection
  and logging continue but injection is **held** until the file is removed.
- **Spend / monthly caps are never injected** — a billing cap has no timed reset;
  it is logged as "cannot auto-resume" and skipped.
- **Token safety.** All credential + network code is isolated in `usage_api.py`;
  the OAuth token is never logged, printed, or stored.
- **Full logging** of every poll decision (quota, percent, resolved reset) and
  every action (target window title + why, injected/held/aborted-and-why).

State, log, and kill-switch locations:

| item | default path |
|------|--------------|
| state (watermark) | `%LOCALAPPDATA%\claude-autoresume\state.json` |
| log | `%LOCALAPPDATA%\claude-autoresume\autoresume.log` |
| kill switch | `%TEMP%\autoresume.stop` |

---

## Requirements

- Windows, Python 3.9+ (developed on 3.13). Standard library only (`urllib`,
  `json`, `datetime`, `ctypes`), plus `tzdata` (already installed) so `zoneinfo`
  has the IANA database on Windows — used only by the transcript fallback.
- A readable Claude OAuth credential at `~/.claude/.credentials.json` (or
  `$CLAUDE_CONFIG_DIR/.credentials.json`) for the primary usage-API source. If
  none is readable, `--source auto` falls back to the transcript watcher.
- Claude Code running as the **VS Code extension** (chat panel — the owner's
  setup). The `ctrl+alt+shift+k` → `claude-vscode.focus` keybinding must be
  present in `keybindings.json` when using the default `--focus-method keybind`
  (see "Focusing the Claude Code chat input" above); otherwise use
  `--focus-method palette`.
- Optional: the **Usage Monitor for Claude** app — if it is running, autoresume
  backs off its own polling (it is not required). Optional: AutoHotkey v2 (for
  `--injector ahk`), at `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe`.

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
| `--buffer N` | 45 | seconds after reset before injecting |
| `--usage-poll N` | 165 | usage-endpoint poll cadence (s) |
| `--monitor-poll N` | 900 | backed-off cadence while the Usage Monitor app runs |
| `--confirm-interval N` | 30 | re-poll cadence to confirm a reset before injecting |
| `--confirm-below PCT` | 90 | utilization must drop below this to confirm a reset |
| `--on-auth-expired {log,update}` | log | on HTTP 401: log, or run `claude update` once |
| `--cred-path PATH` | auto | override the credentials file path |
| `--injector {win32,ahk}` | win32 | keystroke mechanism |
| `--focus-method {keybind,palette,none}` | keybind | how to focus the Claude Code chat input before typing |
| `--prefer-title SUBSTR` | (none) | override: force this VS Code window as the target |
| `--dry-run` | off | detect + log, but **never** type |
| `--target-proc` / `--target-title` | `Code.exe` / `Visual Studio Code` | window match |
| `--poll N` | 5 | loop tick (s): kill-switch / GUI responsiveness |
| `--watch-dir DIR` | auto-detected project dir | transcript fallback: where to tail |
| `--from-start` | off (tail from EOF) | transcript fallback: re-scan the whole file |

Run it once at logon (Task Scheduler → "At log on" → Program `pythonw.exe`,
arguments the full path to `autoresume.py` plus `watch`), or from a terminal.

### Sub-commands for testing

```bash
# Parse limit-hit lines from a transcript/fixture and print KIND + resolved reset
python autoresume.py parse-file tests/fixtures/sample_limit_hits.jsonl

# Inject a message into a window right now (test the keystroke path)
python autoresume.py inject-now "hello from autoresume" --target-title "Notepad" --target-proc Notepad.exe
```

---

## Tests

```bash
python -m pytest tests/ -q      # the pytest-collected suite (usage-API + GUI)
```

The pytest suite (`tests/test_usage_api.py`, `tests/test_gui.py`) covers the
usage-API detection with **mocked** responses — no real network, no token:

- **89 % / 38 % → arm NONE** (and 91 % / *critical*) — reproduces today's
  false-arm as a passing regression.
- **100 % / exhausted → arms**, with `fire_at == resets_at + buffer`.
- **blocked → reset → injects exactly once** (driven through the real
  `run_watch` loop with a fake clock, dry-run).
- **still blocked at fire time → waits / re-checks, never injects.**
- **no credentials → `auto` falls back to the transcript source.**
- **ISO 8601 `resets_at` tz parsing** (offset, microseconds, `Z`, non-UTC).
- **monitor present → poll cadence backs off.**

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
| — | kill switch, logging, once-per-reset watermark, Usage-Monitor-aware polling |

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
