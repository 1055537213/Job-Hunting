# ClamAV 文件扫描验收

## 验收目标

文件扫描代码存在并不代表生产链路可用。上线前必须证明以下行为同时成立：

- FreshClam 已取得足够新的病毒库，`clamd` 能通过 TCP 接受扫描请求。
- 正常文件可以完成解析、正文登记和受控下载。
- EICAR 标准测试文件会进入 `quarantined/infected`，不能下载或进入 RAG。
- `clamd` 停机时系统采用 fail-closed：文件进入 `quarantined/error`，而不是绕过扫描。
- `clamd` 恢复后 Web 不需要重启，后续上传可以重新通过扫描。
- 删除正常或隔离文件时，PostgreSQL 元数据、长文本关系和 MinIO 对象同步清理。

EICAR 只是一段无害的行业标准测试字符串，不是真实恶意程序。它只用于证明扫描链路能够
识别病毒库签名，不能代替针对压缩包、畸形文档和大文件的安全测试。

## 隔离演练

Docker Engine 运行时，在项目根目录执行：

```powershell
.\scripts\validate_file_scanning.ps1
```

脚本默认从当前源码构建 `job-hunting-agent:file-scan-acceptance`。已经构建过同一份代码时可复用：

```powershell
.\scripts\validate_file_scanning.ps1 `
  -Image job-hunting-agent:security-scan `
  -SkipBuild
```

`compose.file-scan-test.yaml` 会移除 PostgreSQL、Redis、MinIO 和 Web 的宿主机端口，并由脚本
生成唯一 Compose 项目名和临时凭证。数据库、对象、Redis 和病毒库均使用该项目自己的卷，
演练完成后执行 `down -v`，不会接触普通开发环境或生产命名卷。

默认要求 daily 病毒库构建时间不超过 48 小时。这个时间读取自 `sigtool --info` 的
`Build time`，不是文件修改时间；新卷初始化会改变文件修改时间，不能用它证明病毒库新鲜。
可通过 `-MaximumDefinitionAgeHours` 调整门槛，但生产放宽前必须有明确的网络故障处置策略。

## 报告

报告保存在被 Git 忽略的：

```text
data/file-scan-drills/<run-id>/file-scan-report.json
```

报告记录固定 ClamAV 镜像摘要、引擎版本、daily 数据库版本、签名数量、真实构建时间，以及
正常文件、EICAR、服务故障、服务恢复和清理结果。报告不保存文件正文、对象存储凭证或 `.env`。

## 生产操作

- 生产 Compose 固定使用 ClamAV 1.4 LTS 的最新安全补丁镜像摘要。
- ClamAV 病毒库加载需要显著内存，生产 Compose 为服务预留最高 4 GiB；部署前仍要按服务器
  实际文件体积和并发扫描量做容量测试。
- `/var/lib/clamav` 必须使用持久卷，否则每次重启都需要重新下载完整病毒库。
- `clamdscan --ping 1` 只证明 daemon 可响应；发布验收还必须检查病毒库真实构建时间。
- 扫描服务不可用时不要切换到 `local` 后端。生产配置会拒绝该降级，上传继续进入隔离状态。
- 每次升级 ClamAV、修改文件大小上限、变更对象存储或调整上传流程后重新执行演练。
- 目标服务器首次发布时再次执行并保存报告；本地通过不能证明服务器网络、内存和磁盘满足要求。
