' autoresume-gui.vbs -- double-click launcher for the autoresume status window.
'
' Double-clicking a .vbs runs it under wscript.exe, which has NO console -- so
' there is no cmd-window flash. It then starts the watcher + status window with
' pythonw.exe (the windowless Python) using WScript.Shell.Run with window style 0
' (hidden), so the ONLY window that ever appears is the Tkinter status window.
'
' Robust to a broken .pyw file association (it does not rely on it): it searches
' PATH for pythonw.exe / pyw.exe itself.

Option Explicit

Dim shell, fso, here, script, py, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "autoresume.py")

If Not fso.FileExists(script) Then
    MsgBox "autoresume.py not found next to this launcher:" & vbCrLf & script, _
           48, "autoresume"
    WScript.Quit 1
End If

' Prefer pythonw.exe, then the py-launcher's windowless pyw.exe.
py = FindOnPath(fso, shell, "pythonw.exe")
If py = "" Then py = FindOnPath(fso, shell, "pyw.exe")
If py = "" Then
    ' Last resort: let Run resolve a bare name via PATH.
    py = "pythonw.exe"
End If

cmd = """" & py & """ """ & script & """ watch --gui"

On Error Resume Next
' 0 = hidden window (pythonw has no console anyway); False = do not wait.
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    MsgBox "Could not start the autoresume status window." & vbCrLf & _
           "Tried: " & cmd & vbCrLf & "Error: " & Err.Description, 48, "autoresume"
    WScript.Quit 1
End If
On Error Goto 0

WScript.Quit 0


Function FindOnPath(fso, shell, exeName)
    Dim windir, pathVal, parts, i, cand
    FindOnPath = ""

    ' The py launcher (pyw.exe) usually lives in %WINDIR%.
    windir = shell.ExpandEnvironmentStrings("%WINDIR%")
    If fso.FileExists(fso.BuildPath(windir, exeName)) Then
        FindOnPath = fso.BuildPath(windir, exeName)
        Exit Function
    End If

    pathVal = shell.ExpandEnvironmentStrings("%PATH%")
    parts = Split(pathVal, ";")
    For i = 0 To UBound(parts)
        If Len(Trim(parts(i))) > 0 Then
            cand = fso.BuildPath(Trim(parts(i)), exeName)
            If fso.FileExists(cand) Then
                FindOnPath = cand
                Exit Function
            End If
        End If
    Next
End Function
