using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net.NetworkInformation;
using System.Text;
using System.Threading;

namespace FinAgentRuntime {
    public sealed class ProcessIdentity {
        public int Id, Depth;
        public DateTime StartedUtc;
    }
    public static class ServiceProcesses {
        static readonly Dictionary<int, DateTime> starts = new Dictionary<int, DateTime>();
        public static Process Start(string executable, string arguments, string directory, string output, string error) {
            var info = new ProcessStartInfo(executable, arguments) {
                WorkingDirectory = directory, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            // Direct CreateProcess keeps children in the launcher's Windows job.
            var process = Process.Start(info);
            starts[process.Id] = process.StartTime.ToUniversalTime();
            Pump(process.StandardOutput, output);
            Pump(process.StandardError, error);
            return process;
        }
        static void Pump(StreamReader reader, string path) {
            var thread = new Thread(() => {
                try {
                    using (var writer = new StreamWriter(path, false, new UTF8Encoding(false))) {
                        writer.AutoFlush = true;
                        string line;
                        while ((line = reader.ReadLine()) != null) writer.WriteLine(line);
                    }
                } catch (IOException) { } catch (ObjectDisposedException) { }
            });
            thread.IsBackground = true;
            thread.Start();
        }
        public static List<ProcessIdentity> Capture(int rootId, DateTime startedUtc) {
            var result = new List<ProcessIdentity>();
            result.Add(new ProcessIdentity { Id = rootId, StartedUtc = startedUtc, Depth = 0 });
            var parents = new Dictionary<int, int>();
            using (var search = new ManagementObjectSearcher("SELECT ProcessId, ParentProcessId FROM Win32_Process"))
            using (var rows = search.Get()) {
                foreach (ManagementObject row in rows) {
                    using (row) parents[Convert.ToInt32(row["ProcessId"])] = Convert.ToInt32(row["ParentProcessId"]);
                }
            }
            var depths = new Dictionary<int, int>(); depths[rootId] = 0;
            bool found = true;
            while (found) {
                found = false;
                foreach (var pair in parents) {
                    int depth;
                    if (depths.ContainsKey(pair.Key) || !depths.TryGetValue(pair.Value, out depth)) continue;
                    depths[pair.Key] = depth + 1; found = true;
                    try {
                        using (var p = Process.GetProcessById(pair.Key)) {
                            var time = p.StartTime.ToUniversalTime();
                            if (time >= startedUtc) result.Add(new ProcessIdentity { Id = pair.Key, StartedUtc = time, Depth = depth + 1 });
                        }
                    } catch (ArgumentException) { } catch (InvalidOperationException) { } catch (System.ComponentModel.Win32Exception) { }
                }
            }
            result.Sort((a,b) => b.Depth.CompareTo(a.Depth));
            return result;
        }
        public static bool Alive(ProcessIdentity identity) {
            try {
                using (var p = Process.GetProcessById(identity.Id))
                    return !p.HasExited && p.StartTime.ToUniversalTime() == identity.StartedUtc;
            } catch (ArgumentException) { return false; } catch (InvalidOperationException) { return false; }
        }
        public static void KillKnown(IEnumerable<ProcessIdentity> identities) {
            foreach (var identity in identities) {
                try {
                    using (var p = Process.GetProcessById(identity.Id)) {
                        if (!p.HasExited && p.StartTime.ToUniversalTime() == identity.StartedUtc) {
                            p.Kill(); p.WaitForExit(3000);
                        }
                    }
                } catch (ArgumentException) { } catch (InvalidOperationException) { } catch (System.ComponentModel.Win32Exception) { }
            }
        }
        public static bool StopTree(Process process) {
            if (process == null) return true;
            DateTime started;
            if (!starts.TryGetValue(process.Id, out started)) {
                try { started = process.StartTime.ToUniversalTime(); }
                catch (InvalidOperationException) { return true; }
            }
            var tree = Capture(process.Id, started);
            KillKnown(tree);
            foreach (var item in tree) if (Alive(item)) return false;
            return true;
        }
        public static int[] ListeningPorts() {
            var ports = new List<int>();
            foreach (var endpoint in IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners())
                if ((endpoint.Port == 8000 || endpoint.Port == 5173) && !ports.Contains(endpoint.Port)) ports.Add(endpoint.Port);
            ports.Sort(); return ports.ToArray();
        }
    }
}
