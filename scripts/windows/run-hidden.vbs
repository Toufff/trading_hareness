' Runs a command line completely hidden from the first frame, for scheduled
' tasks where "-WindowStyle Hidden" alone is not enough: under an
' Interactive logon, Task Scheduler still briefly flashes a conhost window
' before the hidden style takes effect. WScript.Shell.Run with windowStyle 0
' never creates a visible window at all.
'
' Usage:
'   wscript.exe run-hidden.vbs "<full command line>"
Option Explicit

Dim shell, commandLine
If WScript.Arguments.Count < 1 Then
    WScript.Echo "Usage: wscript.exe run-hidden.vbs ""<full command line>"""
    WScript.Quit 1
End If

commandLine = WScript.Arguments(0)
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run(commandLine, 0, True)
