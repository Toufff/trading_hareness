using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;

internal static class BrokerWindowCapture
{
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc f, IntPtr p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);
    delegate bool EnumProc(IntPtr h, IntPtr p);
    struct RECT { public int L,T,R,B; }
    static string Text(IntPtr h, bool cls) { var s=new StringBuilder(256); if(cls) GetClassName(h,s,s.Capacity); else GetWindowText(h,s,s.Capacity); return s.ToString(); }
    static object Describe(IntPtr h) { uint pid; GetWindowThreadProcessId(h,out pid); RECT r; GetWindowRect(h,out r); return new { hwnd=h.ToInt64(), pid, title=Text(h,false), @class=Text(h,true), visible=IsWindowVisible(h), rect=new {left=r.L,top=r.T,right=r.R,bottom=r.B,width=r.R-r.L,height=r.B-r.T} }; }
    static void Main(string[] a) {
        Console.OutputEncoding = Encoding.UTF8;
        if (a.Length>0 && a[0]=="--foreground") { Console.WriteLine(GetForegroundWindow().ToInt64()); return; }
        if (a.Length>1 && a[0]=="--show") { long sn; if(!long.TryParse(a[1],out sn)) Environment.Exit(2); var sh=(IntPtr)sn; RECT sr; if(!GetWindowRect(sh,out sr)) Environment.Exit(3); ShowWindow(sh,4); if(!SetWindowPos(sh,IntPtr.Zero,sr.L,sr.T,sr.R-sr.L,sr.B-sr.T,0x0014)||!IsWindowVisible(sh)) Environment.Exit(3); return; }
        if (a.Length>3 && a[0]=="--resize") { long rn=0; int rw=0,rh=0; if(!long.TryParse(a[1],out rn)||!int.TryParse(a[2],out rw)||!int.TryParse(a[3],out rh)) Environment.Exit(2); RECT rr; if(!GetWindowRect((IntPtr)rn,out rr)||!SetWindowPos((IntPtr)rn,IntPtr.Zero,rr.L,rr.T,rw,rh,0x0014)) Environment.Exit(3); RECT vr; if(!GetWindowRect((IntPtr)rn,out vr)||vr.R-vr.L!=rw||vr.B-vr.T!=rh) Environment.Exit(4); return; }
        if (a.Length>0 && a[0]=="--list") { var xs=new System.Collections.Generic.List<string>(); EnumWindows((h,p)=>{ xs.Add(Json(h)); EnumChildWindows(h,(c,q)=>{xs.Add(Json(c));return true;},IntPtr.Zero); return true;},IntPtr.Zero); Console.WriteLine("["+String.Join(",",xs)+"]"); return; }
        long n=0; if (a.Length<3 || a[0]!="--capture" || !long.TryParse(a[1],out n)) Environment.Exit(2);
        var hwnd=(IntPtr)n; RECT r; if(!GetWindowRect(hwnd,out r) || r.R<=r.L || r.B<=r.T) Environment.Exit(3);
        using(var b=new Bitmap(r.R-r.L,r.B-r.T,PixelFormat.Format32bppArgb)) using(var g=Graphics.FromImage(b)) {
            var ok=PrintWindow(hwnd,g.GetHdc(),2); g.ReleaseHdc(); if(!ok) Environment.Exit(4);
            long sum=0; for(int x=0;x<b.Width;x+=Math.Max(1,b.Width/20)) for(int y=0;y<b.Height;y+=Math.Max(1,b.Height/20)) { var c=b.GetPixel(x,y); sum += c.R+c.G+c.B; }
            if(sum==0) Environment.Exit(5); b.Save(a[2],ImageFormat.Png);
        }
    }
    static string Esc(string s) { return (s??"").Replace("\\","\\\\").Replace("\"","\\\"").Replace("\r","\\r").Replace("\n","\\n"); }
    static string Json(IntPtr h) { return new JavaScriptSerializer().Serialize(Describe(h)); }
}
