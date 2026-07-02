# autoresume — Claude Code auto-resume on usage-limit reset (Windows)

When a Claude Code session on a Max plan hits a usage limit (the rolling
**session** window or the **weekly** window), the session stalls until the limit
resets. `autoresume` watches the live session transcript for the limit-hit,
figures out **which** limit was hit and **when** it resets, waits until the reset
(plus a small buffer), then **types a plain-language resume message into the
Claude Code window and presses Enter** — so unattended autonomous work continues
on its own.

It replaces a blunt predecessor (see "Replaces the old AHK script" below) that
blindly pressed Enter at a fixed 1:15 am.

The injected message tells the agent, in the owner's voice:

> `[AUTOMATED RESUME] A <session|weekly> usage limit was hit and has now reset
> (reset reported: <RESET_TIME>). This is an automated message; the user is away
> and will NOT see or respond to your output. Continue the autonomous work from
> where you left off: consult _research/AUTONOMOUS_WORKLOG.md and the current
> todo list, and keep going. Do not wait for user input.`

---

## How it works

```
watch transcript ─▶ detect limit-hit ─▶ parse KIND + reset ─▶ resolve reset (tz)
        ▲                                                              │
        └───────────── loop ◀── inject msg+Enter ◀── wait until reset+buffer
```

1. **Watch.** Tails the newest, actively-growing `*.jsonl` in the Claude Code
   project transcript directory (under `%USERPROFILE%\.claude\projects\`; by
   default the most recently active project is auto-detected — override with
   `--watch-dir`), seeking to EOF so the large history is ignored. If a newer
   session file appears, it switches to it.

2. **Detect.** Each new line is matched against the limit-hit predicate
   (`type=="assistant"` **and** `isApiErrorMessage==true` **and**
   `error=="rate_limit"` **and** the text starts `You've hit your (session|weekly|
   monthly spend) limit`). Keying on `rate_limit`/HTTP 429 alone is **not** enough
   — transient overload (`"Server is temporarily limiting requests …"`,
   `"529 Overloaded"`) shares that; the text prefix disambiguates.

3. **Parse KIND + reset.** The three real shapes:
   | KIND | example text (after `resets `) | meaning |
   |------|-------------------------------|---------|
   | `session` | `12am (America/Los_Angeles)` | time-only → next occurrence |
   | `weekly`  | `Jul 3, 1am (America/Los_Angeles)` | date+time, year inferred |
   | `monthly spend` | *(none — `raise it at claude.ai/settings/usage`)* | billing cap; **cannot** auto-resume — logged and skipped |

   The timezone in parens is the **account's** tz (may differ from the machine
   clock). The reset is resolved in that tz with `zoneinfo` and converted to the
   machine-local instant; session times roll to the next future occurrence,
   weekly dates infer the nearest future year.

4. **Dedup.** A single limit emits many identical retry lines. Each hit collapses
   to a `(KIND, reset-minute)` key; a persisted watermark (`state.json`) ensures
   **exactly one** injection per reset, even across restarts, and the historical
   backlog is ignored.

5. **Wait, then inject.** Sleeps until `reset + buffer` (default +45 s; the reset
   text is minute-granular and the server clock can lag). Then it finds the
   Claude Code (VS Code) window, brings it to the foreground, **verifies the
   foreground window really is the target** (correct-window guard), types the
   message as literal Unicode keystrokes, and presses Enter.

6. **Log & loop.** Every detection and action (or abort, with reason) is appended
   to `autoresume.log`; then it returns to watching for the next limit.

### Detection is transcript-based on purpose

The Anthropic **developer API** returns rate-limit reset info only in response
**headers** (`anthropic-ratelimit-*-reset`, `retry-after`) for API-key usage —
those are **not** the Max-plan subscription session/weekly limits, and there is
no documented developer-API endpoint for the subscription limits. The reliable
signal is the limit-hit **message** Claude Code writes to its transcript. (A live
OAuth token and rate-limit *tier* exist under `~/.claude`, but no usage counters
or reset timestamps are cached locally, so there is nothing to poll.)

---

## Injection mechanism & fail-safes

**Primary:** pure Win32 `SendInput` via `ctypes` — no third-party packages. It
finds the VS Code window (`Code.exe`, title contains `Visual Studio Code`,
preferring the workspace named by `--prefer-title`), brings it forward (defeating the
Windows foreground lock via `AttachThreadInput` + a zeroed foreground-lock
timeout + an ALT tap), **focuses the Claude Code chat input** (see below), then
types the message and Enter into it.

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
  the reset minute.
- **Kill switch.** If the stop-file (`%TEMP%\autoresume.stop`) exists, detection
  and logging continue but injection is **held** until the file is removed.
- **Monthly-spend is never injected** — it is a billing cap with no reset; it is
  logged as "cannot auto-resume" and skipped.
- **Full logging** of every detection (KIND, raw text, resolved reset) and every
  action (activated window title, injected/held/aborted-and-why).

State, log, and kill-switch locations:

| item | default path |
|------|--------------|
| state (watermark) | `%LOCALAPPDATA%\claude-autoresume\state.json` |
| log | `%LOCALAPPDATA%\claude-autoresume\autoresume.log` |
| kill switch | `%TEMP%\autoresume.stop` |

---

## Requirements

- Windows, Python 3.9+ (developed on 3.13). Standard library only, plus `tzdata`
  (already installed) so `zoneinfo` has the IANA database on Windows.
- Claude Code running as the **VS Code extension** (chat panel — the owner's
  setup). The `ctrl+alt+shift+k` → `claude-vscode.focus` keybinding must be
  present in `keybindings.json` when using the default `--focus-method keybind`
  (see "Focusing the Claude Code chat input" above); otherwise use
  `--focus-method palette`.
- Optional: AutoHotkey v2 (for `--injector ahk`), at
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
| `--watch-dir DIR` | auto-detected most-recent project dir | where to tail |
| `--buffer N` | 45 | seconds after reset before injecting |
| `--poll N` | 5 | transcript poll cadence (s) |
| `--injector {win32,ahk}` | win32 | keystroke mechanism |
| `--focus-method {keybind,palette,none}` | keybind | how to focus the Claude Code chat input before typing |
| `--dry-run` | off | detect + log, but **never** type |
| `--from-start` | off (tail from EOF) | re-scan the whole transcript |
| `--target-proc` / `--target-title` | `Code.exe` / `Visual Studio Code` | window match |

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
python tests/test_parse.py      # KIND + reset-time + tz math + dedup + message
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
| fires at a hard-coded 1:15 am | fires at the **actual** reset time parsed from the limit message |
| blind `{Enter}` | a full, plain-language **resume message** + Enter |
| no window targeting | **correct-window guard** (right process + title, or abort) |
| no idea which limit / whether one was even hit | detects **session vs weekly vs monthly-spend**, only resumes recoverable ones |
| could fire when no limit was hit | fires **only** on a real, deduped limit-hit |
| — | kill switch, logging, once-per-reset watermark |

You can leave the old `.ahk` disabled/removed once this is running at logon.

---

## License

MIT — see [LICENSE](LICENSE).
