using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace StockRuntime {
    // The kernel reaps the child tree even when Task Scheduler force-kills the
    // supervisor (when neither PowerShell finally nor a polling loop can run).
    public sealed class ProcessLifetime : IDisposable {
        [StructLayout(LayoutKind.Sequential)] private struct BasicLimit {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinWorkingSet, MaxWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] private struct IoCounters {
            public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimit {
            public BasicLimit Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern SafeFileHandle CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetInformationJobObject(SafeFileHandle job, int infoClass, ref ExtendedLimit info, uint size);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);
        private SafeFileHandle handle;
        public ProcessLifetime(bool allowChildBreakaway = false) {
            handle=CreateJobObject(IntPtr.Zero,null);
            if(handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            var limit=new ExtendedLimit();
            limit.Basic.Flags=0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            // The GUI task host owns its script, not independent services such
            // as PostgreSQL started by pg_ctl. Each supervised service has its
            // own non-breakaway job; owner-bound services additionally poll owner.
            if(allowChildBreakaway) limit.Basic.Flags|=0x1000;
            if(!SetInformationJobObject(handle,9,ref limit,(uint)Marshal.SizeOf<ExtendedLimit>())) {
                int error=Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(error);
            }
        }
        public void Attach(Process process) {
            if(!AssignProcessToJobObject(handle,process.Handle)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        public void Dispose() { handle.Dispose(); }
    }
}
