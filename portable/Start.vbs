Option Explicit
Dim fso, shell, root, python, launcher, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
python = fso.BuildPath(root, "runtime\pythonw.exe")
launcher = fso.BuildPath(root, "app\desktop_launcher.py")
If Not fso.FileExists(python) Or Not fso.FileExists(launcher) Then
  MsgBox "Please extract the entire ZIP first, then open Start.vbs from the extracted folder.", vbCritical, "Football Intelligence AI"
  WScript.Quit 1
End If
shell.CurrentDirectory = root
command = Chr(34) & python & Chr(34) & " -I -X utf8 -B " & Chr(34) & launcher & Chr(34)
shell.Run command, 1, False
