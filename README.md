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
```

以后要换模型，优先改 `.env` 里的 `JOB_AGENT_LLM_MODEL`、`JOB_AGENT_LLM_BASE_URL`
和 `JOB_AGENT_LLM_API_KEY`，不要改业务代码。

## 运行测试

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m pytest tests\test_mvp_flow.py -q
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

### 3. 分析并保存本地项目卡片

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['analyze-project', 'E:\\path\\to\\your_project', '--candidate-id', '1'])"
```

这一步只保存“待确认项目经历卡片”。自动分析发现的技术栈不会直接覆盖候选人档案。

确认项目卡片：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['confirm-project', '1', '--summary', '本人负责职位解析、匹配排序和 FastAPI 接口设计。'])"
```

### 4. 导入职位文本

单个职位：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['import-job', 'job.txt', '--source-url', 'https://www.zhipin.com/job_detail/example.html'])"
```

多个职位可以放在同一个文本文件里，用 `---JOB---` 分隔：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['import-jobs', 'jobs.txt'])"
```

### 5. 查看和匹配职位

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['list-jobs'])"
```

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['match-all', '1'])"
```

`match-all` 会返回职位信息和对应匹配结果；已淘汰职位排在后面，未淘汰职位按分数从高到低排序。

### 6. 生成职位定制简历草稿

默认模式不调用真实 LLM，会生成规则版安全草稿：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1'])"
```

检查当前模型配置。这个命令会脱敏输出，不会显示 API Key：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['llm-config'])"
```

使用 `.env` 中的 DeepSeek V4 Pro 生成草稿：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -c "import sys; sys.path.insert(0, 'src'); from job_hunting_agent.cli import main; main(['draft-resume', '1', '1', '--use-env-llm'])"
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
