FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends libatomic1 && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# 先强制单独安装 crewai，避免哈希冲突
RUN pip install --no-cache-dir crewai==1.14.2
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m prisma py fetch && python -m prisma generate
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]