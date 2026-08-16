# 固定 Python 3.12.13 补丁版本，使本地 Conda 环境与容器运行时保持一致。
# 网络受限时仍可通过 BASE_IMAGE 切换镜像源，但覆盖值必须保持相同的 Python 版本。
ARG BASE_IMAGE=python:3.12.13-slim
FROM ${BASE_IMAGE}

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

# Alembic 脚本属于运行时迁移资产，必须随镜像保留，但不包含任何 .env 密钥。
COPY pyproject.toml README.md alembic.ini ./
COPY requirements.lock ./
COPY alembic ./alembic
COPY src ./src

# 先安装锁定的运行时依赖，再以无依赖模式安装项目本身；这样不会在构建时重新解析
# pyproject.toml 中的宽泛版本范围，Web、Worker 和迁移容器可以共享同一套版本。
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps .

# 应用不以 root 身份运行；运行时文件正文由对象存储服务保存。
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# 直接调用项目定义的 Web 入口；Compose 注入 PostgreSQL 和对象存储配置。
CMD ["job-agent-web", "--env-file", "/app/.env", "--host", "0.0.0.0", "--port", "8000"]
