FROM python:3.13-slim

ARG OCS_SCRIPT_VERSION=4.15.3

# 从官方 uv 镜像复制 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# ============================================================
# OCS Script Install
# ============================================================

RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/ocsjs/ocsjs/releases/download/${OCS_SCRIPT_VERSION}/ocs.user.js', '/app/ocs.user.js')"

RUN python -c "from pathlib import Path; p=Path('/app/ocs.user.js'); s=p.read_text(encoding='utf-8'); line='// @connect      ocs-llm-answer'; p.write_text(s if line in s else s.replace('// ==/UserScript==', line + '\n// ==/UserScript==', 1), encoding='utf-8')"

# 先只复制依赖描述，利用 Docker cache
COPY pyproject.toml uv.lock ./

# 创建项目虚拟环境并安装锁定依赖
RUN uv sync --frozen --no-dev --no-install-project

# 再复制代码
COPY . .

# 如果当前项目本身也需要作为 package 安装，可再执行一次
RUN uv sync --frozen --no-dev

EXPOSE 5000

CMD ["/app/.venv/bin/python", "main.py"]