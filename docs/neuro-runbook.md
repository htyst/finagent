# FinAgent 新版运行说明

代码已在 D:\finagent 实现。最终后端164项测试全通过，前端类型检查与生产构建通过；真实模型调用和对照实验未实施。

## 原项目启动

```powershell
cd D:\finagent
.\scripts\start-neuro.ps1 -Clock manual -Mode mock -Planner model -Trace on
```

mock沿用旧模板，但可创建周期指令。使用现有.env进入新版模型计划通道：

```powershell
.\scripts\start-neuro.ps1 -Clock manual -Mode live -Planner model -Trace on
```

旧 live 通道为 `-Planner legacy`；真实时钟为 `-Clock real`。不要为了使用新版而重置原数据库。服务启动只增加5张新表。结束自己已有的后台进程以避免端口占用，前端沿用现有 Vite 启动方式。

## 可直接演示的输入

- 新版live：查一下余额，然后从活期转100元到储蓄账户。
- 新版live：如果本月餐饮超过500元，就从活期转100元到储蓄账户。
- mock或live：每月1号从活期转300元到张三尾号3821。
- 查询定期转账；暂停定期转账；恢复定期转账；取消定期转账。

张三有两个预置收款人，必须指定尾号或按追问选择。创建长期指令与每月本期单均需页面确认。聊天中的“确认”不会执行转账。

对话区域的调试面板可推进一天、查看Trace、确认长期指令和领取本期操作单。推进较多天可通过受保护的 `POST /api/debug/clock/advance {"days":32}`；real模式禁用调试日历。

## 代码包迁移到其他电脑

不复制当前机器的虚拟环境。在新的隔离目录解压源码后，用Python3.12创建.venv并按backend/requirements.lock安装依赖，前端运行npm ci。只在新建的演示库需要初始化数据，原有项目不要执行reset。模型密钥通过自己的.env配置，代码包未携带密钥或数据库。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock
cd frontend
npm ci
```

完整架构、技术细节、工程输出与未实施的实验建议见docs/neuro-report.md。
