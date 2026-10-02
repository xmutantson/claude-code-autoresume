#Requires AutoHotkey v2.0
; Legacy fallback disabled: it cannot verify the configured workspace,
; Claude session, or chat input. Do not activate windows or send keystrokes.
; Use autoresume.py with its configured URI injector instead.
; Exit code 5: disabled adapter; no input was sent.
ExitApp(5)
