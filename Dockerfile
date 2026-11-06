FROM python:3.12-slim
WORKDIR /app

# إنشاء مجلد static افتراضي وخاوٍ لتفادي أي خطأ في النسخ
RUN mkdir -p static

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py telegram_listener.py make_session.py platform.html ./

# نسخ مجلد static فقط إذا كان متوفراً ولن يفشل البناء إن غاب
COPY static* ./static/

# Hosts set $PORT; --forwarded-allow-ips makes https detection (secure cookies) work behind their proxy
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
