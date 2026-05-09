FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends libatomic1 && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# 使用阿里云镜像源并设置超时
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --timeout 600 \
    --retries 10
COPY . .
RUN python -m prisma py fetch && python -m prisma generate
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]