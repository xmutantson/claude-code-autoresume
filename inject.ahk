#Requires AutoHotkey v2.0
; inject.ahk -- fallback injector for autoresume.py (owner's AHK v2 toolchain).
;
; Activates the Claude Code (VS Code) window, verifies it really became active
; (the correct-window guard), types the message literally, then presses Enter.
; Mirrors the old send_enter_115am.ahk Send model, upgraded from a blind Enter
; to text+Enter with a real, verified target.
;
; Usage (autoresume.py calls this automatically with --injector ahk):
;   AutoHotkey64.exe inject.ahk "<message>"
;   AutoHotkey64.exe inject.ahk "<message>" "ahk_exe Code.exe"
;
; Exit codes: 0 ok | 2 no message | 3 no target window | 4 could not activate.

msg := A_Args.Length >= 1 ? A_Args[1] : ""
target := A_Args.Length >= 2 ? A_Args[2] : "ahk_exe Code.exe"

if (msg = "")
    ExitApp(2)

if !WinExist(target)
    ExitApp(3)

WinActivate(target)
if !WinWaitActive(target, , 3)      ; correct-window guard: must actually be active
    ExitApp(4)

Sleep(350)                          ; let the terminal settle / take focus
SendText(msg)                       ; literal text -- no !^+{} interpretation
Sleep(100)
Send("{Enter}")                     ; submit
ExitApp(0)
