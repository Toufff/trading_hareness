' Runs a command completely hidden from the first frame, for scheduled
' tasks where "-WindowStyle Hidden" alone is not enough: under an
' Interactive logon, Task Scheduler still briefly flashes a conhost window
' before the hidden style takes effect. WScript.Shell.Run with windowStyle 0
' never creates a visible window at all.
'
' Usage:
'   wscript.exe run-hidden.vbs <executable> [argument ...]
'
' Every argument is taken as one token of the command to run and is re-quoted
' here when it contains a space. Passing the whole command line as a single
' pre-quoted argument does not work: the caller's inner quotes are consumed by
' the Windows command-line parser before WScript sees them, WScript.Arguments(0)
' then holds a fragment such as "C:\Program", and Shell.Run raises a runtime
' error that wscript.exe reports through a modal dialog - which, in a scheduled
' task, hangs the task until its execution time limit expires.
Option Explicit

Dim shell, commandLine, i, argument, result

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

commandLine = ""
For i = 0 To WScript.Arguments.Count - 1
    argument = WScript.Arguments(i)
    If InStr(argument, " ") > 0 Then
        argument = """" & argument & """"
    End If
    If i = 0 Then
        commandLine = argument
    Else
        commandLine = commandLine & " " & argument
    End If
Next

Set shell = CreateObject("WScript.Shell")
' Never display a modal error dialog, even if a caller forgot //B.
On Error Resume Next
result = shell.Run(commandLine, 0, True)
If Err.Number <> 0 Then WScript.Quit 125
On Error GoTo 0
WScript.Quit result
