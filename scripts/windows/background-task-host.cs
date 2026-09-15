using System;
using System.Diagnostics;
using System.Text;

namespace StockRuntime {
    // Compiled /target:winexe. No console is created, including the first frame.
    // Unlike WScript.Shell.Run(...,0), this never delegates a console startup to
    // Windows Terminal before a requested hidden style has taken effect.
    public static class BackgroundTaskHost {
        private static string Quote(string value) {
            var text=new StringBuilder("\""); int slashes=0;
            foreach(char c in value) {
                if(c=='\\') { slashes++; continue; }
                if(c=='"') { text.Append('\\',slashes*2+1); text.Append(c); }
                else { text.Append('\\',slashes); text.Append(c); }
                slashes=0;
            }
            text.Append('\\',slashes*2); text.Append('"'); return text.ToString();
        }
        public static int Main(string[] args) {
            if(args.Length<2) return 125;
            Process child=null;
            try {
                using(var lifetime=new ProcessLifetime(true)) {
                    var command=new StringBuilder("-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ");
                    for(int i=1;i<args.Length;i++) { if(i>1) command.Append(' '); command.Append(Quote(args[i])); }
                    var info=new ProcessStartInfo(args[0],command.ToString());
                    info.UseShellExecute=false;
                    info.CreateNoWindow=true;
                    child=Process.Start(info);
                    lifetime.Attach(child);
                    child.WaitForExit();
                    return child.ExitCode;
                }
            } catch { return 125; }
            finally {
                if(child!=null) { try { if(!child.HasExited) child.Kill(); } catch {} child.Dispose(); }
            }
        }
    }
}
