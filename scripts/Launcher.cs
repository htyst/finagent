using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

class Launcher : Form {
    Button mock = new Button(), live = new Button(), stop = new Button(), browser = new Button();
    Label status = new Label(); TextBox log = new TextBox();
    Timer timer = new Timer(); Process process; IntPtr job; string mode;
    bool probing, ready, smoke; volatile bool startupConfirmed; DateTime started;
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly HttpClient http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr h, int c, ref JobInfo i, uint s);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] struct Limits { public long a,b; public uint flags; public UIntPtr min,max; public uint count; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct Counters { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct JobInfo { public Limits limits; public Counters counters; public UIntPtr a,b,c,d; }
    Launcher(string initial, bool test) {
        smoke=test; Text="FinAgent 启动器"; Width=760; Height=520; MinimumSize=new System.Drawing.Size(620,400);
        StartPosition=FormStartPosition.CenterScreen;
        var panel=new FlowLayoutPanel { Dock=DockStyle.Top, Height=52, Padding=new Padding(8) };
        mock.Text="启动 Mock"; live.Text="启动 Live"; stop.Text="停止服务"; browser.Text="打开网页";
        foreach(var b in new[]{mock,live,stop,browser}) { b.Width=145; b.Height=32; panel.Controls.Add(b); }
        status.Dock=DockStyle.Top; status.Height=45; status.Padding=new Padding(10); status.Text="请选择模式。Live 使用本地 .env 中的模型配置。";
        log.Dock=DockStyle.Fill; log.Multiline=true; log.ReadOnly=true; log.ScrollBars=ScrollBars.Both; log.WordWrap=false;
        Controls.Add(log); Controls.Add(status); Controls.Add(panel);
        mock.Click+=(s,e)=>Start("mock"); live.Click+=(s,e)=>Start("live");
        stop.Click+=(s,e)=>Stop(); browser.Click+=(s,e)=>OpenBrowser();
        stop.Enabled=false; browser.Enabled=false;
        timer.Interval=700; timer.Tick+=async(s,e)=>await Poll();
        FormClosing+=(s,e)=>{ if(smoke) { Directory.CreateDirectory(Path.Combine(root,"var","logs")); File.WriteAllText(Path.Combine(root,"var","logs","launcher-smoke-"+mode+".log"),status.Text+Environment.NewLine+log.Text); } Stop(); };
        Shown+=(s,e)=>{ if(initial!=null) Start(initial); };
    }
    void Log(string text) {
        if(IsDisposed || !IsHandleCreated) return;
        if(InvokeRequired) { try { BeginInvoke(new Action<string>(Log),text); } catch(InvalidOperationException) {} return; }
        log.AppendText(text+Environment.NewLine);
    }
    void Start(string selected) {
        if(process!=null) return;
        try {
            if(!File.Exists(Path.Combine(root,"scripts","dev.ps1"))) throw new Exception("请将 exe 放在 FinAgent 项目根目录，确保 scripts/dev.ps1 存在。");
            mode=selected; ready=false; startupConfirmed=false; log.Clear();
            job=CreateJobObject(IntPtr.Zero,null);
            var info=new JobInfo(); info.limits.flags=0x2000;
            if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ref info,(uint)Marshal.SizeOf(typeof(JobInfo)))) throw new Exception("无法建立服务进程管理。");
            var psi=new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell\\v1.0\\powershell.exe"),"-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \""+Path.Combine(root,"scripts","launcher-run.ps1")+"\" -Mode "+mode);
            psi.WorkingDirectory=root; psi.UseShellExecute=false; psi.CreateNoWindow=true;
            psi.RedirectStandardOutput=true; psi.RedirectStandardError=true;
            psi.StandardOutputEncoding=Encoding.UTF8; psi.StandardErrorEncoding=Encoding.UTF8;
            process=new Process { StartInfo=psi };
            process.OutputDataReceived+=(s,e)=>{ if(e.Data!=null) { if(e.Data.Contains("http://127.0.0.1:5173")) startupConfirmed=true; Log(e.Data); } };
            process.ErrorDataReceived+=(s,e)=>{ if(e.Data!=null) { if(e.Data.Contains("http://127.0.0.1:5173")) startupConfirmed=true; Log(e.Data); } };
            process.Start();
            if(!AssignProcessToJobObject(job,process.Handle)) { process.Kill(); throw new Exception("无法管理服务子进程，启动已取消。"); }
            process.BeginOutputReadLine(); process.BeginErrorReadLine();
            started=DateTime.UtcNow; mock.Enabled=live.Enabled=false; stop.Enabled=true;
            status.Text="正在启动 "+mode+"，请稍候…"; timer.Start();
        } catch(Exception ex) { Log(ex.Message); Stop(); status.Text="启动失败，请查看下方信息。"; if(smoke) { Environment.ExitCode=1; Close(); } }
    }
    async Task Poll() {
        if(probing || process==null) return;
        probing=true;
        try {
            if(process.HasExited) { int code=process.ExitCode; Stop(); status.Text="服务已退出，请查看日志（退出码 "+code+"）。"; if(smoke) { Environment.ExitCode=1; Close(); } return; }
            if(ready) return;
            try {
                string api=await http.GetStringAsync("http://127.0.0.1:8000/api/demo/info");
                await http.GetStringAsync("http://127.0.0.1:5173");
                if(process==null || process.HasExited) return;
                if(!startupConfirmed || !api.Contains("\""+mode+"\"")) return;
                ready=true; browser.Enabled=true; status.Text=mode+" 已启动：http://127.0.0.1:5173（关闭启动器将停止服务）";
                if(smoke) { Log("SMOKE_OK "+mode); Environment.ExitCode=0; Close(); } else OpenBrowser();
            } catch(HttpRequestException) {} catch(TaskCanceledException) {}
            if(!ready && process!=null && (DateTime.UtcNow-started).TotalSeconds>120) { Stop(); status.Text="启动超时，请查看日志并检查依赖。"; if(smoke) { Environment.ExitCode=1; Close(); } }
        } finally { probing=false; }
    }
    void Stop() {
        timer.Stop(); ready=false; browser.Enabled=false;
        if(job!=IntPtr.Zero) { CloseHandle(job); job=IntPtr.Zero; }
        if(process!=null) { process.Dispose(); process=null; }
        mock.Enabled=live.Enabled=true; stop.Enabled=false; status.Text="服务已停止，可选择模式重新启动。";
    }
    void OpenBrowser() { try { Process.Start(new ProcessStartInfo("http://127.0.0.1:5173") { UseShellExecute=true }); } catch(Exception ex) { Log("打开浏览器失败："+ex.Message); } }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        string name=Path.GetFileNameWithoutExtension(Application.ExecutablePath).ToLowerInvariant();
        string initial=name.EndsWith("-live")?"live":name.EndsWith("-mock")?"mock":null;
        bool test=false;
        foreach(string arg in args) { if(arg=="--live") initial="live"; if(arg=="--mock") initial="mock"; if(arg=="--smoke-test") test=true; }
        Application.Run(new Launcher(initial,test));
    }
}
