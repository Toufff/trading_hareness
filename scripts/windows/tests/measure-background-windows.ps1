param([int]$Seconds = 120, [Parameter(Mandatory)][string]$OutputPath, [string]$ReadyPath = '')
$ErrorActionPreference = 'Stop'
# Read-only WinEvent hooks: never focus, show, hide, move or capture a window.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class BackgroundWindowAudit {
  public sealed class Row { public string at, kind, process, windowClass; public int pid; public long hwnd; public bool visible; }
  private delegate void EventProc(IntPtr hook,uint evt,IntPtr hwnd,int obj,int child,uint thread,uint time);
  [StructLayout(LayoutKind.Sequential)] private struct Msg { public IntPtr hwnd; public uint message; public UIntPtr wParam; public IntPtr lParam; public uint time; public int x,y; public uint extra; }
  [DllImport("user32.dll")] private static extern IntPtr SetWinEventHook(uint min,uint max,IntPtr module,EventProc fn,uint pid,uint thread,uint flags);
  [DllImport("user32.dll")] private static extern bool UnhookWinEvent(IntPtr hook);
  [DllImport("user32.dll")] private static extern bool PeekMessage(out Msg msg,IntPtr hwnd,uint min,uint max,uint remove);
  [DllImport("user32.dll")] private static extern bool TranslateMessage(ref Msg msg);
  [DllImport("user32.dll")] private static extern IntPtr DispatchMessage(ref Msg msg);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd,StringBuilder text,int count);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
  public static Row[] Run(int seconds,string ready) {
    var rows = new List<Row>();
    EventProc callback = (hook,evt,hwnd,obj,child,thread,time) => {
      if(hwnd==IntPtr.Zero || (evt==0x8002 && obj!=0)) return;
      uint id; GetWindowThreadProcessId(hwnd,out id);
      var cls=new StringBuilder(256); GetClassName(hwnd,cls,256);
      string name="exited"; try { using(var p=Process.GetProcessById((int)id)) name=p.ProcessName; } catch {}
      bool console=cls.ToString().IndexOf("Console",StringComparison.OrdinalIgnoreCase)>=0 || cls.ToString().IndexOf("CASCADIA",StringComparison.OrdinalIgnoreCase)>=0;
      bool worker=name=="pwsh" || name=="powershell" || name=="ssh" || name=="wscript" || name=="stock-background-host" || name=="WindowsTerminal" || name=="OpenConsole";
      if(console || worker) rows.Add(new Row {at=DateTimeOffset.Now.ToString("o"),kind=evt==3?"foreground":"show",process=name,pid=(int)id,hwnd=hwnd.ToInt64(),windowClass=cls.ToString(),visible=IsWindowVisible(hwnd)});
    };
    var show=SetWinEventHook(0x8002,0x8002,IntPtr.Zero,callback,0,0,0);
    var focus=SetWinEventHook(3,3,IntPtr.Zero,callback,0,0,0);
    if(show==IntPtr.Zero || focus==IntPtr.Zero) throw new Exception("Window audit hooks could not be installed");
    try {
      if(!String.IsNullOrEmpty(ready)) System.IO.File.WriteAllText(ready,DateTimeOffset.Now.ToString("o"));
      var until=DateTime.UtcNow.AddSeconds(seconds);
      while(DateTime.UtcNow<until && (String.IsNullOrEmpty(ready) || !System.IO.File.Exists(ready+".stop"))) { Msg msg; while(PeekMessage(out msg,IntPtr.Zero,0,0,1)) { TranslateMessage(ref msg); DispatchMessage(ref msg); } Thread.Sleep(5); }
    } finally { UnhookWinEvent(show); UnhookWinEvent(focus); GC.KeepAlive(callback); }
    return rows.ToArray();
  }
}
'@
$started = [DateTimeOffset]::Now
$events = @([BackgroundWindowAudit]::Run($Seconds, $ReadyPath))
$result = [ordered]@{started_at=$started.ToString('o');ended_at=[DateTimeOffset]::Now.ToString('o');seconds=$Seconds;hook='EVENT_OBJECT_SHOW+EVENT_SYSTEM_FOREGROUND';events=$events;console_events=$events.Count}
[IO.File]::WriteAllText($OutputPath, ($result|ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{evidence=$OutputPath;seconds=$Seconds;console_events=$events.Count}
