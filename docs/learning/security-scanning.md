# 依赖与容器镜像安全扫描

## 扫描边界

项目把供应链检查拆成两个事实源：

- `pip-audit 2.10.1` 检查 `requirements.lock` 和 `requirements-dev.lock` 中的 Python 包。
- Trivy `0.74.0` 检查最终运行镜像中的 Debian 系统包，并生成完整镜像 SBOM。

前端目前是随 Python 包发布的仓库内静态资源，没有 `package.json` 或 npm 锁文件，因此没有
虚构 npm 审计步骤。以后引入 npm 依赖时，必须先提交锁文件，再增加 `npm audit` 或等价门禁。

Trivy 直接使用固定镜像摘要，不使用可移动的 GitHub Action 标签。Python 基础镜像同样固定
到 Python 3.12.13 的镜像摘要；Docker 构建期间仍会应用 Debian stable/security 更新。

## 本地执行

Docker Engine 运行时，在项目根目录执行：

```powershell
.\scripts\security_scan.ps1
```

脚本默认构建 `job-hunting-agent:security-scan`。已经构建过同一份代码时可以复用镜像：

```powershell
.\scripts\security_scan.ps1 -Image job-hunting-agent:security-scan -SkipBuild
```

Python 审计容器只读挂载两个锁文件，不读取 `.env`。Docker 构建上下文也通过 `.dockerignore`
排除 `.env`、运行数据、Git 元数据和测试缓存。

## 发布门禁

- Python：发现任何已知漏洞即失败，不设置永久忽略列表。
- 容器：存在已有修复版本的 `HIGH` 或 `CRITICAL` Debian 漏洞即失败。
- 尚无发行版修复的系统漏洞保留在完整报告中，但不让所有发布永久处于失败状态；每次上线前
  仍要人工检查可达性、运行架构和 Debian 安全状态。
- Python 包由 `pip-audit` 判定；Trivy 的阻断扫描限定为操作系统包，避免把 Python wheel
  内嵌或上游基础镜像 SBOM 当成实际安装包。CycloneDX SBOM 仍包含镜像全部组件。

本地报告保存在被 Git 忽略的 `data/security-reports/<timestamp>/`：

- `python-dependencies.json`：Python 漏洞清单。
- `container-vulnerabilities.json`：所有 HIGH/CRITICAL 系统漏洞，包括尚无修复版本的记录。
- `image-sbom.cdx.json`：CycloneDX 软件物料清单。
- `security-summary.json`：门禁结果、计数、工具版本和固定镜像摘要。

## GitHub CI

CI 会在测试、Ruff 和前端回归通过后执行同一策略：审计锁文件、使用固定基础镜像构建最终
镜像、生成 Trivy 报告与 SBOM，最后统一判定两个门禁。报告通过固定版本的
`actions/upload-artifact` 上传为 `security-reports`，保留 14 天；即使门禁失败也会尝试上传，
便于定位具体包、CVE、已安装版本和修复版本。

## 漏洞处理

1. Python 漏洞优先升级 `pyproject.toml` 中的约束并重新生成 `requirements.lock`。
2. Debian 漏洞先用 `docker build --pull` 重建；如果安全仓库已有修复，构建中的系统升级会带入。
3. 没有修复版本时记录可达性、受影响平台、缓解措施、负责人和复查日期，不因“当前不可修复”
   就从完整报告中删除。
4. 确需例外时必须按具体漏洞 ID、具体镜像和明确到期时间审批；不得使用包名通配符或永久忽略。
