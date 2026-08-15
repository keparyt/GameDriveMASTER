Option Explicit

Dim shell
Dim fso
Dim base
Dim pythonw

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(
    WScript.ScriptFullName
)

pythonw = "pythonw.exe"

shell.Run _
    """" & pythonw & """ """ & _
    base & "\launcher.py""", _
    0, _
    False
