using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using FinAgentRuntime;

class Launcher : Form {
    Button mock=new Button(), live=new Button(), stop=new Button(), browser=new Button();
    Label status=new Label(); TextBox log=new TextBox();
    Timer timer=new Timer(), earlyTimer=new Timer(); Process process; IntPtr job; string mode, smokeAction;
    System.Threading.EventWaitHandle startGate, stopSignal;
    List<ProcessIdentity> owned=new List<ProcessIdentity>();
    bool probing, ready, smoke, stopping, lastStopVerified; volatile bool startupConfirmed;
    int generation, smokePass, managerId; DateTime started, managerStarted;
    readonly string root=AppDomain.CurrentDomain.BaseDirectory;
    readonly HttpClient http=new HttpClient { Timeout=TimeSpan.FromSeconds(2) };
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a,string n);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr h,int c,ref JobInfo i,uint s);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j,IntPtr p);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr j,uint exitCode);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] struct Limits { public long a,b; public uint flags; public UIntPtr min,max; public uint count; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct Counters { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct JobInfo { public Limits limits; public Counters counters; public UIntPtr a,b,c,d; }
    Launcher(string initial,string test) {
        smoke=test!=null; smokeAction=test; Text="FinAgent 启动器"; Width=760; Height=520; MinimumSize=new System.Drawing.Size(620,400);
        StartPosition=FormStartPosition.CenterScreen;
        if(smoke) { ShowInTaskbar=false; Opacity=0; }
        var panel=new FlowLayoutPanel { Dock=DockStyle.Top,Height=52,Padding=new Padding(8) };
        mock.Text="启动 Mock"; live.Text="启动 Live"; stop.Text="停止服务"; browser.Text="打开网页";
        foreach(var b in new[]{mock,live,stop,browser}) { b.Width=145; b.Height=32; panel.Controls.Add(b); }
        status.Dock=DockStyle.Top; status.Height=45; status.Padding=new Padding(10); status.Text="请选择模式。Live 使用本地 .env 中的模型配置。";
        log.Dock=DockStyle.Fill; log.Multiline=true; log.ReadOnly=true; log.ScrollBars=ScrollBars.Both; log.WordWrap=false;
        Controls.Add(log); Controls.Add(status); Controls.Add(panel);
        mock.Click+=(s,e)=>Start("mock"); live.Click+=(s,e)=>Start("live");
        stop.Click+=(s,e)=>Stop(); browser.Click+=(s,e)=>OpenBrowser();
        stop.Enabled=browser.Enabled=false;
        timer.Interval=700; timer.Tick+=async(s,e)=>await Poll();
        earlyTimer.Interval=200; earlyTimer.Tick+=(s,e)=>{ earlyTimer.Stop(); stop.PerformClick(); Environment.ExitCode=lastStopVerified?0:1; Close(); };
        FormClosing+=(s,e)=>{
            if(!Stop()) Environment.ExitCode=1;
            if(smoke) {
                Directory.CreateDirectory(Path.Combine(root,"var","logs"));
                File.WriteAllText(Path.Combine(root,"var","logs","launcher-smoke-"+smokeAction+".log"),status.Text+Environment.NewLine+log.Text);
            }
        };
        Shown+=(s,e)=>{ if(initial!=null) Start(initial); };
    }
    void Log(string text) {
        if(IsDisposed||!IsHandleCreated) return;
        if(InvokeRequired) { try { BeginInvoke(new Action<string>(Log),text); } catch(InvalidOperationException) {} return; }
        log.AppendText(text+Environment.NewLine);
    }
    void Start(string selected) {
        if(process!=null||stopping) return;
        try {
            if(ServiceProcesses.ListeningPorts().Length!=0) throw new Exception("8000/5173 端口已被占用，未启动本项目。请先停止原服务。");
            if(!File.Exists(Path.Combine(root,"scripts","dev.ps1"))) throw new Exception("请将 exe 放在 FinAgent 项目根目录，确保 scripts/dev.ps1 存在。");
            mode=selected; generation++; ready=false; startupConfirmed=false; owned.Clear(); log.Clear();
            string instance=Guid.NewGuid().ToString("N");
            startGate=new System.Threading.EventWaitHandle(false,System.Threading.EventResetMode.ManualReset,"Local\\FinAgentStart-"+instance);
            stopSignal=new System.Threading.EventWaitHandle(false,System.Threading.EventResetMode.ManualReset,"Local\\FinAgentStop-"+instance);
            job=CreateJobObject(IntPtr.Zero,null); var info=new JobInfo(); info.limits.flags=0x2000;
            if(job==IntPtr.Zero||!SetInformationJobObject(job,9,ref info,(uint)Marshal.SizeOf(typeof(JobInfo)))) throw new Exception("无法建立服务进程管理。");
            var psi=new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell\\v1.0\\powershell.exe"),"-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \""+Path.Combine(root,"scripts","launcher-run.ps1")+"\" -Mode "+mode);
            psi.WorkingDirectory=root; psi.UseShellExecute=false; psi.CreateNoWindow=true;
            psi.RedirectStandardOutput=psi.RedirectStandardError=true;
            psi.StandardOutputEncoding=psi.StandardErrorEncoding=Encoding.UTF8;
            psi.EnvironmentVariables["FINAGENT_START_EVENT"]="Local\\FinAgentStart-"+instance;
            psi.EnvironmentVariables["FINAGENT_STOP_EVENT"]="Local\\FinAgentStop-"+instance;
            process=new Process { StartInfo=psi };
            process.OutputDataReceived+=(s,e)=>{ if(e.Data!=null) { if(e.Data.Contains("http://127.0.0.1:5173")) startupConfirmed=true; Log(e.Data); } };
            process.ErrorDataReceived+=(s,e)=>{ if(e.Data!=null) Log(e.Data); };
            process.Start(); managerId=process.Id; managerStarted=process.StartTime.ToUniversalTime();
            if(!AssignProcessToJobObject(job,process.Handle)) throw new Exception("无法管理服务子进程，启动已取消。");
            process.BeginOutputReadLine(); process.BeginErrorReadLine(); startGate.Set();
            started=DateTime.UtcNow; mock.Enabled=live.Enabled=false; stop.Enabled=true;
            status.Text="正在启动 "+mode+"，请稍候…"; timer.Start();
            if(smokeAction=="early") earlyTimer.Start();
        } catch(Exception ex) { Log(ex.Message); Stop(); status.Text="启动失败："+ex.Message; if(smoke) { Environment.ExitCode=1; Close(); } }
    }
    async Task Poll() {
        if(probing||process==null||stopping) return;
        probing=true; int expected=generation; Process current=process;
        try {
            if(current.HasExited) { int code=current.ExitCode; Stop(); Log("管理进程退出码 "+code); if(smoke) { Environment.ExitCode=1; Close(); } return; }
            if(ready) return;
            try {
                string api=await http.GetStringAsync("http://127.0.0.1:8000/api/demo/info");
                await http.GetStringAsync("http://127.0.0.1:5173");
                if(expected!=generation||process!=current||current.HasExited||stopping) return;
                if(!startupConfirmed||!api.Contains("\""+mode+"\"")) return;
                ready=true; browser.Enabled=true; status.Text=mode+" 已启动：http://127.0.0.1:5173（停止或关闭启动器将结束服务）";
                if(smoke) {
                    Log("SMOKE_READY "+mode);
                    if(smokeAction=="close") { Close(); return; }
                    stop.PerformClick();
                    if(!lastStopVerified) { Environment.ExitCode=1; Close(); return; }
                    if(smokeAction=="restart"&&smokePass++==0) { Start(mode=="mock"?"live":"mock"); return; }
                    Log("SMOKE_OK"); Environment.ExitCode=0; Close();
                } else OpenBrowser();
            } catch(HttpRequestException) {} catch(TaskCanceledException) {}
            if(expected==generation&&!ready&&process!=null&&(DateTime.UtcNow-started).TotalSeconds>120) { Stop(); status.Text="启动超时，请查看日志并检查依赖。"; if(smoke) { Environment.ExitCode=1; Close(); } }
        } finally { probing=false; }
    }
    bool Stop() {
        if(stopping) return lastStopVerified;
        stopping=true; generation++; timer.Stop(); earlyTimer.Stop(); ready=false; browser.Enabled=false;
        mock.Enabled=live.Enabled=stop.Enabled=false; status.Text="正在停止服务并核验端口…"; status.Refresh();
        try {
            if(process!=null) {
                try { owned.AddRange(ServiceProcesses.Capture(managerId,managerStarted)); } catch(Exception ex) { Log("读取子进程失败："+ex.Message); }
                if(stopSignal!=null) stopSignal.Set();
                try { if(!process.HasExited) process.WaitForExit(3000); } catch(InvalidOperationException) {}
                try { owned.AddRange(ServiceProcesses.Capture(managerId,managerStarted)); } catch(Exception ex) { Log("复核子进程失败："+ex.Message); }
            }
            if(job!=IntPtr.Zero) { if(!TerminateJobObject(job,1)) Log("进程组终止返回："+Marshal.GetLastWin32Error()); CloseHandle(job); job=IntPtr.Zero; }
            ServiceProcesses.KillKnown(owned);
            DateTime until=DateTime.UtcNow.AddSeconds(4); bool alive; int[] ports;
            do {
                alive=false; foreach(var identity in owned) if(ServiceProcesses.Alive(identity)) alive=true;
                ports=ServiceProcesses.ListeningPorts();
                if(!alive&&ports.Length==0) break;
                System.Threading.Thread.Sleep(100);
            } while(DateTime.UtcNow<until);
            lastStopVerified=!alive&&ports.Length==0;
            if(lastStopVerified) { owned.Clear(); status.Text="服务已停止，8000/5173 端口已释放。"; Log("STOP_VERIFIED ports=free"); }
            else { status.Text=alive?"停止未完成，仍有本次子进程运行；可再次点击停止。":"本次进程已结束，但 8000/5173 仍被其他进程占用；未显示为已停止。"; Log(status.Text); }
            if(process!=null) { process.Dispose(); process=null; }
            if(startGate!=null) { startGate.Dispose(); startGate=null; }
            if(stopSignal!=null) { stopSignal.Dispose(); stopSignal=null; }
            mock.Enabled=live.Enabled=!alive; stop.Enabled=alive;
            return lastStopVerified;
        } catch(Exception ex) { lastStopVerified=false; Log("停止失败："+ex.Message); status.Text="停止失败，请查看日志；未确认端口释放。"; stop.Enabled=true; return false; }
        finally { stopping=false; }
    }
    void OpenBrowser() { if(!ready)return; try { Process.Start(new ProcessStartInfo("http://127.0.0.1:5173") { UseShellExecute=true }); } catch(Exception ex) { Log("打开浏览器失败："+ex.Message); } }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        string name=Path.GetFileNameWithoutExtension(Application.ExecutablePath).ToLowerInvariant();
        string initial=name.EndsWith("-live")?"live":name.EndsWith("-mock")?"mock":null,test=null;
        foreach(string arg in args) { if(arg=="--live")initial="live"; if(arg=="--mock")initial="mock"; if(arg=="--smoke-test")test="button"; if(arg=="--smoke-close")test="close"; if(arg=="--smoke-restart")test="restart"; if(arg=="--smoke-early")test="early"; }
        Application.Run(new Launcher(initial,test));
    }
}
