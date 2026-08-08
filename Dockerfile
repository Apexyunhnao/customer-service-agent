FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 确保 data 目录和 db 文件存在
RUN mkdir -p data && python -c "from db import init_db; init_db()" 2>/dev/null; true

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
