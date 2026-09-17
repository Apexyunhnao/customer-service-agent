FROM python:3.11-slim

WORKDIR /app

# 依赖：用生成式锁文件安装（含全部传递依赖），保证镜像可复现
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

# 确保 data 目录和表存在
RUN mkdir -p data && python -c "from db import init_db; init_db()" 2>/dev/null; true

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
