# 使用与项目要求一致的 Python 3.12 运行时，保证本地环境与容器环境尽量一致。
FROM python:3.12-slim

# 这些环境变量让 Python 在容器中直接输出日志，并把源码目录加入模块搜索路径。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# PDF/OCR 相关依赖会用到这些基础系统库；不安装完整桌面环境，减少镜像体积。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 后续命令都在 /app 内执行，源码和运行数据边界清晰。
WORKDIR /app

# 复制项目声明和源码，随后按项目定义安装所有运行依赖。
COPY pyproject.toml README.md ./
COPY src ./src

# 以包的形式安装项目，确保 `job-agent-web` 命令和 Vue 静态资源都可用。
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

# 应用不以 root 身份运行；运行时的 data 目录由 Compose 挂载到宿主机。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# 直接调用项目定义的 Web 入口；Compose 会传入 .env 和数据目录参数。
CMD ["job-agent-web", "--db", "/app/data/job_agent.db", "--env-file", "/app/.env", "--rag-dir", "/app/data/chroma", "--resume-dir", "/app/data/resumes", "--host", "0.0.0.0", "--port", "8000"]
