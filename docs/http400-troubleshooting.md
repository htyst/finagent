# FinAgent 模型 HTTP 400 修复与排查说明

本次修复代码位于 D:\finagent。没有访问真实 DeepSeek、没有修改密钥或主数据库。

## 已修复的问题

1. DomainError继承ValueError，旧捕获分支将HTTP错误包装成P_RESPONSE。现在DomainError原样传递，HTTP400等请求错误不进入计划修复，400只请求一次。
2. 模型请求Schema包含DeepSeek strict模式不支持的minLength、maxLength、minItems、maxItems。发送用Schema移除这些字段，内联非递归引用并将const转换为enum；后端完整Pydantic、业务和依赖检查仍保留。
3. Trace新增model_error，保存http_status、服务端message/code/param/type、request_id、端点路径、模型名和Schema哈希。只选取错误摘要，脱敏密钥、Bearer及已知用户消息，不保存完整请求或错误响应正文。
4. 缺信息允许模型返回steps为空且clarification为有效追问。这样的资产不执行工具，也不生成操作单。没有追问的空计划仍拒绝。这样避免把正常缺信息误报为修复失败。

官方strict文档：https://api-docs.deepseek.com/zh-cn/guides/tool_calls/
官方要求beta端点；不支持字符串长度和数组长度约束。pattern、数值minimum/maximum等仍支持，代码没有把后端规则一起删掉。

当前本机配置为api.deepseek.com、/beta、deepseek-flash，已配置密钥。没有将密钥输出到日志。旧Trace没有服务端错误正文，所以不能保证Schema不兼容就是当时400的唯一原因。

## 使修复生效

在运行后端的PowerShell窗口Ctrl+C停止旧服务，重新执行：

```powershell
cd D:\finagent
.\scripts\start-neuro.ps1 -Clock manual -Mode live -Planner model -Trace on
```

前端不用重启。刷新页面后发起一次新请求，并查看新Trace。已保存的旧Trace不会自动改变。

## 如果仍然HTTP400

检查新Trace的model_error：

| 字段或信息 | 处理方法 |
|---|---|
| message提到schema、tools或parameters | 根据param定位具体Schema节点，核对服务端支持的字段；不要关闭后端校验来绕过 |
| message提到model | 检查MODEL_NAME与当前服务可用模型是否匹配 |
| message提到tool_choice或thinking | 确认使用非思考工具调用及兼容接口，代码发送thinking.type=disabled |
| endpoint_path不正确 | 检查MODEL_BASE_URL；官方strict应为/beta/chat/completions，避免把完整chat/completions路径重复作为base |
| 401或403 | 身份或访问权限问题，检查密钥和网关权限；不是计划修复问题 |
| 402 | 账户额度或余额问题，检查服务账户 |
| 429 | 限流，降低频率后重试 |
| 5xx或连接超时 | 服务或连接问题，查看状态与网络；不是用户银行指令不清楚 |

把model_error中的http_status、message、code、param和request_id提供出来即可继续定位。不要发送.env、Authorization头、API密钥或完整网络请求。

## 验证范围

新增16项模型接口与澄清回归，覆盖六类HTTP错误、单次请求、严格工具调用格式、错误脱敏、Schema兼容转换、端点、真实200坏响应以及澄清不执行。所有HTTP响应使用MockTransport，不产生真实模型调用费用。全量结果以随附后端回归结果JSON为准。


## 最终回归结果

2026年10月14日修订后，全量工程回归180项全部通过，其中原有113项和新增67项。实际输出为180 passed, 5 warnings in 34.53s；真实模型调用0。
