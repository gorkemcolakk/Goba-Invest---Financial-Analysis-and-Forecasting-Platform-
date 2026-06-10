FROM python:3.11-slim

WORKDIR /app

# Sistem bağımlılıkları (Prophet için gerekli)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python bağımlılıkları
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama
COPY . .

# Cache ve log klasörleri
RUN mkdir -p static/cache

EXPOSE 5000

CMD ["python", "app.py"]
