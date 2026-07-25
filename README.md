# Job Hunting Agent MVP

这是求职助手 Agent 的第一条本地端到端链路。当前版本保持只读和本地运行：不登录 BOSS 直聘，不自动投递，不自动发送 HR 消息。

## 当前能力

- 用 SQLite 保存候选人结构化档案。
- 保存职位原文，并解析出职位名、城市、薪资、经验、学历、技能等标准化字段。
- 按已确认规则输出职位匹配分数、推荐档位、淘汰原因、风险和简历优化方向。
- 分析候选人提供的本地项目目录，生成待确认项目经历卡片。
- 保存项目经历卡片，并在候选人确认后把它作为后续检索/简历改写材料。
- 对本地已导入职位做批量匹配排序，优先展示未淘汰且分数更高的职位。
- 提供 LLM 适配器边界，并生成证据约束的职位定制简历草稿版本。
- 如果 LLM 输出包含未确认技能或成果数字，系统会丢弃该输出并回退到安全规则草稿。
- 支持从项目 `.env` 读取 DeepSeek/OpenAI-compatible 模型配置；当前 `.env` 使用 `deepseek-v4-pro`。
- 新增标准 LangChain Agent 主链路：`Web/CLI -> JobHuntingAgent -> create_agent -> Tools -> JobHuntingApp`。
- 使用 LangChain 文档/文本切分接口和本地持久化 Chroma 搭建第一版 RAG 知识库。
- RAG 检索结果保留来源 metadata，只作为证据上下文，不替代 SQLite 事实源。
- 支持对话式自动入库：用户发来的资料会被自动判断为结构化档案更新或长文本知识库材料。
- 提供本地 Web 前端，可用聊天页面通过 LangChain Agent 或本地规则模式完成档案创建、资料自动入库、职位文本导入和匹配。

## 运行环境

推荐使用你当前的 LangChain 学习环境：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe --version
```

## 模型配置

真实模型配置放在项目根目录的 `.env`，不要写死在代码里。当前 `.env` 已根据
`E:\program\langchain1.2\.env` 中的 DeepSeek 配置生成，并被 `.gitignore` 忽略。

可以参考 [.env.example](<E:/program/Job-hunting Agent/.env.example>)：

```dotenv
JOB_AGENT_LLM_PROVIDER=deepseek
JOB_AGENT_LLM_MODEL=deepseek-v4-pro
JOB_AGENT_LLM_API_KEY=your-api-key-here
JOB_AGENT_LLM_BASE_URL=https://api.deepseek.com
JOB_AGENT_LLM_TIMEOUT_SECONDS=60
JOB_AGENT_LLM_THINKING=enabled
JOB_AGENT_LLM_REASONING_EFFORT=high

JOB_AGENT_EMBEDDING_PROVIDER=openai_compatible
JOB_AGENT_EMBEDDING_MODEL=text-embedding-3-small
JOB_AGENT_EMBEDDING_API_KEY=your-embedding-api-key-here
JOB_AGENT_EMBEDDING_BASE_URL=https://api.openai.com/v1
JOB_AGENT_EMBEDDING_TIMEOUT_SECONDS=60
JOB_AGENT_EMBEDDING_BATCH_SIZE=64
```

以后要换模型，优先改 `.env` 里的 `JOB_AGENT_LLM_MODEL`、`JOB_AGENT_LLM_BASE_URL`
和 `JOB_AGENT_LLM_API_KEY`，不要改业务代码。

聊天模型和 embedding 模型分开配置更稳妥：很多供应商提供聊天模型，但不一定提供
embedding，或者两者的模型名、计费和接口地址不同。

如果没有配置 `JOB_AGENT_EMBEDDING_*`，当前项目会回退到本地 hash embedding，
这样测试、教学和离线演示仍然能跑通；但语义检索质量会明显弱于真实 embedding。

## 运行测试

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m pytest tests\test_mvp_flow.py -q
```

完整回归测试：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m pytest -q
```

如果环境中还没有 Chroma，可以安装项目依赖：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m pip install -e .
```

## 运行端到端演示

不安装包时，可以这样运行：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['--db', 'data/demo.db', 'demo', '--project', '.'])"
```

演示会做四件事：

1. 创建一个演示候选人档案。
2. 分析当前项目目录，生成项目经历卡片。
3. 导入一条 BOSS 风格职位文本。
4. 输出匹配分数、推荐档位、匹配理由、风险和简历建议。

## 本地网页前端

如果你不想使用 CLI，推荐先启动本地网页前端。页面布局采用“左侧档案栏 + 中央聊天区 + 右侧资料/职位面板”，
日常可以像使用聊天网页一样补充资料。当前网页聊天默认优先走标准 LangChain Agent；如果你取消勾选，
会回退到本地规则兜底模式。

第一次使用前建议安装为可编辑包：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m pip install -e .
```

启动网页：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m job_hunting_agent.web --db data/job_agent.db --env-file .env --rag-dir data/chroma
```

然后打开浏览器访问：

```text
http://127.0.0.1:8000
```

你也可以通过原来的 CLI 启动同一个网页服务：

```powershell
job-agent --db data/job_agent.db --env-file .env --rag-dir data/chroma web
```

网页第一版支持：

- 创建和选择候选人档案。
- 像聊天一样发送资料，并通过 LangChain Agent 工具链或本地规则链自动保存到 SQLite 结构化表或 `long_texts`，再按需同步到 RAG 索引。
- 可选开启“使用 LangChain Agent（需 .env）”。
- 默认开启“自动增量 RAG”，新资料会立刻可检索。
- 粘贴 BOSS 职位文本并查看当前候选人的匹配结果。

这个 Web 服务默认只监听 `127.0.0.1`，适合本机使用。停止服务时，在终端按 `Ctrl+C`。

## 日常使用命令

下面这些命令仍然是本地只读 MVP：职位文本需要你从 BOSS 直聘页面主动复制回来，
系统不会登录、爬取、投递或发送消息。

### 1. 初始化数据库

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['init'])"
```

### 2. 创建候选人档案

你可以先写一个 `profile.json`：

```json
{
  "name": "小林",
  "status": "离职",
  "education": "本科",
  "experience_years": 1.0,
  "skills": {
    "Python": "项目使用",
    "FastAPI": "项目使用",
    "LangChain": "项目使用"
  },
  "preferred_cities": ["杭州", "上海"],
  "salary_floor_k": 10,
  "expected_salary_k": 15,
  "target_directions": ["AI Agent 应用开发", "Python 后端开发"],
  "unacceptable": ["外包", "长期出差"]
}
```

然后导入：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['create-profile', '--from-json', 'profile.json'])"
```

如果不传 `--from-json`，命令会进入交互式问答，逐项询问候选人档案字段。

### 3. 对话式自动入库

你可以像聊天一样把资料发给 Agent。系统会一边生成回复，一边自动判断：

- 学历、经验年限、技能、城市、薪资、目标方向、明确不可接受条件，保存到 SQLite 候选人结构化档案。
- 项目描述、经历叙述、成果材料、HR 对话等长文本，先保存到 SQLite `long_texts`。
- Chroma RAG 只从 `long_texts` 同步索引，不直接替代 SQLite 事实源。

不调用真实 LLM 时，会用保守规则提取明确事实，并保存原文：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['ingest-message', '1', '我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目，负责职位解析和匹配排序。'])"
```

调用 `.env` 中的 DeepSeek V4 Pro 做入库判断：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['ingest-message', '1', '我最近做了 LangChain RAG 项目，目标方向是 AI Agent 应用开发。', '--use-env-llm'])"
```

如果资料很长，建议写到文件里再导入：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['ingest-message', '1', '--message-file', 'materials.txt'])"
```

默认只写入 SQLite 和 `long_texts`。如果你希望这条资料立刻进入 RAG 检索索引，可以加 `--auto-rag`。
这个选项采用增量追加：只索引本次新增的长文本，不会全量重建整个 Chroma 集合。

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['ingest-message', '1', '--message-file', 'materials.txt', '--auto-rag'])"
```

命令输出中的 `rag_update_mode` 会显示本次 RAG 动作，例如 `incremental`。
不加 `--auto-rag` 也没关系，后面手动执行 `rag-rebuild` 会把所有 `long_texts` 一次性全量同步到 Chroma。

如果你想直接体验标准 LangChain Agent，而不是只调用底层“入库”命令，可以使用新的 CLI 聊天入口：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['--db', 'data/job_agent.db', '--env-file', '.env', '--rag-dir', 'data/chroma', 'agent-chat', '1', '请根据我现在的资料总结一下，还缺哪些岗位证据'])"
```

这个命令会走标准链路：

- CLI 接收自然语言；
- `JobHuntingAgent` 调用 LangChain `create_agent`；
- Agent 按需选择工具；
- 工具通过 `JobHuntingApp` 读写 SQLite / long_texts / RAG；
- 最终返回中文回复，并保留对话线程记忆。

### 4. 分析并保存本地项目卡片

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['analyze-project', 'E:\\path\\to\\your_project', '--candidate-id', '1'])"
```

这一步只保存“待确认项目经历卡片”。自动分析发现的技术栈不会直接覆盖候选人档案。

确认项目卡片：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['confirm-project', '1', '--summary', '本人负责职位解析、匹配排序和 FastAPI 接口设计。'])"
```

### 5. 导入职位文本

单个职位：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['import-job', 'job.txt', '--source-url', 'https://www.zhipin.com/job_detail/example.html'])"
```

多个职位可以放在同一个文本文件里，用 `---JOB---` 分隔：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['import-jobs', 'jobs.txt'])"
```

### 6. 查看和匹配职位

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['list-jobs'])"
```

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['match-all', '1'])"
```

`match-all` 会返回职位信息和对应匹配结果；已淘汰职位排在后面，未淘汰职位按分数从高到低排序。

### 7. 构建和检索 RAG 知识库

RAG 第一版使用 LangChain + Chroma，把 SQLite `long_texts` 中的职位描述、候选人技能、
已确认项目摘要等材料同步到本地向量库。SQLite 仍然是事实源，Chroma 只是语义检索索引。

全量重建索引。这个命令适合修复索引、切换 embedding 或怀疑 Chroma 与 SQLite 不一致时使用：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['rag-rebuild'])"
```

检索证据：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['rag-search', 'FastAPI 职位解析 匹配排序'])"
```

默认向量库目录是 `data/chroma`，已经被 `.gitignore` 忽略。你也可以显式指定：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['--rag-dir', 'data/chroma', 'rag-rebuild'])"
```

当前 embedding 使用本地确定性 LangChain `Embeddings` 实现，用于先把 RAG 管线跑通；
后续可以替换成真实 embedding 模型，提高语义检索质量。

### 8. 生成职位定制简历草稿

默认模式不调用真实 LLM，会生成规则版安全草稿：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1'])"
```

检查当前模型配置。这个命令会脱敏输出，不会显示 API Key：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['llm-config'])"
```

检查当前 embedding 配置：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['embedding-config'])"
```

使用 `.env` 中的 DeepSeek V4 Pro 生成草稿：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1', '--use-env-llm'])"
```

如果已经执行过 `rag-rebuild`，可以让草稿生成时使用 RAG 检索证据：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1', '--use-rag', '--rag-query', 'FastAPI 职位解析 匹配排序'])"
```

查看已保存的草稿版本：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['list-resume-drafts', '1', '--job-id', '1'])"
```

也可以继续用静态响应模拟 LLM 输出，观察安全检查是否会丢弃越界内容：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1', '--llm-static-response', '候选人精通 Kubernetes，并将性能提升 50%。'])"
```

如果候选人档案和已确认项目卡片里没有 Kubernetes 或“提升 50%”这样的成果证据，
最终草稿正文不会采用这段 LLM 输出，而会在 `authenticity_risks` 中记录风险。

## 代码注释约定

以后本项目新增或修改代码时，默认要补充注释：

- 每个模块顶部写清楚模块职责。
- 每个公开类和公开函数写 docstring，说明输入、输出和业务边界。
- 复杂规则旁边写简短注释，尤其是硬性淘汰、真实性边界、平台接入边界。
- 不给显而易见的单行代码堆注释，避免注释变成噪音。
- 如果代码以后接入 LLM、向量库或 BOSS 数据适配器，要在注释里标明哪些部分是可替换边界。

## 下一步建议

下一步可以让 LLM 辅助“职位文本标准化”：规则解析器先抽取确定字段，LLM 只补充职责、
要求和不确定项说明，仍然保留规则解析作为兜底。
