' Media_Wall control panel launcher (windowless).
' v2.5.4: starts Media_Wall.py with pythonw.exe in a HIDDEN window,
' so no black console window flashes on startup.
' Invoked by 启动面板.bat (which stays as a compatible entry point).

Option Explicit
Dim shell, fso, dir, pyw, args, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Program directory = folder containing this .vbs
dir = fso.GetParentFolderName(WScript.ScriptFullName)

' Locate a windowless Python (pythonw.exe next to python.exe, else PATH pythonw)
pyw = ""
Dim pythonPath
pythonPath = shell.Exec("where python").StdOut.ReadAll() & vbCrLf
Dim lines, i, line
lines = Split(pythonPath, vbCrLf)
For i = 0 To UBound(lines)
    line = Trim(lines(i))
    If line <> "" Then
        Dim cand
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

' Build command line: "pythonw.exe" "Media_Wall.py" [args...]
args = ""
Dim j
For j = 0 To WScript.Arguments.Count - 1
    args = args & " """ & WScript.Arguments(j) & """"
Next
cmd = """" & pyw & """ """ & dir & "\Media_Wall.py""" & args

' Run with NORMAL window (1 = SW_SHOWNORMAL), don't wait.
' NOTE: window style 0 (SW_HIDE) would hide the PySide6 GUI window itself.
' pythonw.exe has no console anyway, so there is nothing to hide.
shell.Run cmd, 1, False

Set shell = Nothing
Set fso = Nothing
