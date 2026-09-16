[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [UInt64]$Hwnd,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [int]$OffsetX = 0,
    [int]$OffsetY = 0,
    [switch]$RightClick
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

if (-not ('BrokerPopupCapture.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace BrokerPopupCapture {
    public static class Native {
        public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT {
            public int X, Y;
            public POINT(int x, int y) { X = x; Y = y; }
        }

        [DllImport("user32.dll")]
        public static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")]
        public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int max);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll")]
        public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")]
        public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
        [DllImport("user32.dll")]
        public static extern IntPtr WindowFromPoint(POINT point);
        [DllImport("user32.dll")]
        public static extern IntPtr ChildWindowFromPointEx(IntPtr parent, POINT point, uint flags);

        public static Dictionary<string, object> Describe(IntPtr hwnd) {
            var cls = new StringBuilder(512);
            var title = new StringBuilder(1024);
            RECT rect;
            uint pid;
            GetClassName(hwnd, cls, cls.Capacity);
            GetWindowText(hwnd, title, title.Capacity);
            GetWindowRect(hwnd, out rect);
            GetWindowThreadProcessId(hwnd, out pid);
            return new Dictionary<string, object> {
                { "hwnd", hwnd.ToInt64() }, { "class", cls.ToString() },
                { "title", title.ToString() }, { "pid", pid },
                { "visible", IsWindowVisible(hwnd) },
                { "left", rect.Left }, { "top", rect.Top },
                { "right", rect.Right }, { "bottom", rect.Bottom }
            };
        }

        public static List<Dictionary<string, object>> EnumerateTopLevel() {
            var rows = new List<Dictionary<string, object>>();
            EnumWindows((hwnd, _) => { rows.Add(Describe(hwnd)); return true; }, IntPtr.Zero);
            return rows;
        }

        public static List<Dictionary<string, object>> EnumerateChildren(IntPtr parent) {
            var rows = new List<Dictionary<string, object>>();
            EnumChildWindows(parent, (hwnd, _) => { rows.Add(Describe(hwnd)); return true; }, IntPtr.Zero);
            return rows;
        }
    }
}
'@
}

$target = [IntPtr]::new([Int64]$Hwnd)
if (-not [BrokerPopupCapture.Native]::IsWindow($target)) {
    throw "Target HWND $Hwnd is not a live window"
}

$rect = New-Object BrokerPopupCapture.Native+RECT
if (-not [BrokerPopupCapture.Native]::GetWindowRect($target, [ref]$rect)) {
    throw "GetWindowRect failed for HWND $Hwnd"
}

$beforeForeground = [BrokerPopupCapture.Native]::GetForegroundWindow().ToInt64()
$beforeTop = [BrokerPopupCapture.Native]::EnumerateTopLevel()
$beforeChildren = [BrokerPopupCapture.Native]::EnumerateChildren($target)
$screenX = $rect.Left + $OffsetX
$screenY = $rect.Top + $OffsetY

if ($RightClick) {
    if ($OffsetX -le 0 -or $OffsetY -le 0 -or
        $screenX -ge $rect.Right -or $screenY -ge $rect.Bottom) {
        throw 'Right-click point must be a positive offset inside the target window'
    }
    [void][BrokerPopupCapture.Native]::SetForegroundWindow($target)
    Start-Sleep -Milliseconds 200
    [void][BrokerPopupCapture.Native]::SetCursorPos($screenX, $screenY)
    [BrokerPopupCapture.Native]::mouse_event(0x0008, 0, 0, 0, [UIntPtr]::Zero)
    [BrokerPopupCapture.Native]::mouse_event(0x0010, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 800
}

$virtual = [System.Windows.Forms.SystemInformation]::VirtualScreen
$output = [IO.Path]::GetFullPath($OutputPath)
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output)) | Out-Null
$bitmap = New-Object System.Drawing.Bitmap($virtual.Width, $virtual.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.CopyFromScreen($virtual.Left, $virtual.Top, 0, 0, $bitmap.Size)
    $bitmap.Save($output, [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

$afterTop = [BrokerPopupCapture.Native]::EnumerateTopLevel()
$afterChildren = [BrokerPopupCapture.Native]::EnumerateChildren($target)
$point = New-Object BrokerPopupCapture.Native+POINT -ArgumentList $screenX, $screenY
$pointWindow = [BrokerPopupCapture.Native]::WindowFromPoint($point)
$clientPoint = New-Object BrokerPopupCapture.Native+POINT -ArgumentList $OffsetX, $OffsetY
$pointChild = [BrokerPopupCapture.Native]::ChildWindowFromPointEx($target, $clientPoint, 0x0001 -bor 0x0002 -bor 0x0004)
$beforeIds = @($beforeTop | ForEach-Object { [Int64]$_['hwnd'] })

[ordered]@{
    target = [BrokerPopupCapture.Native]::Describe($target)
    right_click = [bool]$RightClick
    screen_point = @{ x = $screenX; y = $screenY }
    window_from_point = if ($pointWindow -ne [IntPtr]::Zero) { [BrokerPopupCapture.Native]::Describe($pointWindow) } else { $null }
    child_from_point = if ($pointChild -ne [IntPtr]::Zero) { [BrokerPopupCapture.Native]::Describe($pointChild) } else { $null }
    foreground_before = $beforeForeground
    foreground_after = [BrokerPopupCapture.Native]::GetForegroundWindow().ToInt64()
    new_top_level_windows = @($afterTop | Where-Object { $beforeIds -notcontains [Int64]$_['hwnd'] })
    visible_top_level_windows = @($afterTop | Where-Object { $_['visible'] })
    target_children_before = $beforeChildren
    target_children_after = $afterChildren
    screenshot = $output
} | ConvertTo-Json -Depth 8
