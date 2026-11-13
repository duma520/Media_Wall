' Media_Wall watchdog launcher (windowless).
' v2.7.0: starts mediawall watchdog (watchdog.py) with pythonw.exe in a HIDDEN
' window so no black console flashes. Place a shortcut to this file in the
' Windows Startup folder (shell:startup) for auto-start on login.

Option Explicit
Dim shell, fso, dir, pyw, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)

' Locate pythonw.exe (next to python.exe first, then PATH)
pyw = ""
Dim pythonPath, lines, i, line, cand
pythonPath = shell.Exec("where python").StdOut.ReadAll() & vbCrLf
lines = Split(pythonPath, vbCrLf)
For i = 0 To UBound(lines)
    line = Trim(lines(i))
    If line <> "" Then
        cand = Left(line, InStrRev(line, "\")) & "pythonw.exe"
        If fso.FileExists(cand) Then
            pyw = cand
            Exit For
        End If
    End If
Next
If pyw = "" Then
    On Error Resume Next
    pyw = shell.Exec("where pythonw").StdOut.ReadAll()
    On Error GoTo 0
    pyw = Trim(Split(pyw, vbCrLf)(0))
End If
If pyw = "" Then
    MsgBox "Media_Wall: Python not found. Install Python 3.10+ or add it to PATH.", vbExclamation, "Media_Wall"
    WScript.Quit 1
End If

' Check if watchdog is already running on port 8786; if so, do nothing.
Dim alive
alive = False
On Error Resume Next
Dim http
Set http = CreateObject("MSXML2.XMLHTTP")
http.open "GET", "http://127.0.0.1:8786/api/status", False
http.send
If http.status = 200 Then alive = True
On Error GoTo 0

If Not alive Then
    ' Prefer compiled exe (--watchdog hidden mode) when present; else pythonw watchdog.py
    Dim exe, cmd
    exe = dir & "\Media_Wall.exe"
    If fso.FileExists(exe) Then
        cmd = """" & exe & """ --watchdog"
    Else
        cmd = """" & pyw & """ """ & dir & "\watchdog.py"""
    End If
    shell.Run cmd, 1, False   ' SW_SHOWNORMAL (no console anyway)
End If

Set shell = Nothing
Set fso = Nothing
