FROM python:3.12-slim
WORKDIR /app

# إنشاء مجلد static افتراضياً لتجنب أخطاء النسخ إذا لم يكن موجوداً
RUN mkdir -p static

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py telegram_listener.py make_session.py platform.html ./

# نسخ مجلد static فقط إذا كان متوفراً لتفادي توقف البناء
COPY static* ./static/

# تشغيل السيرفر على المنفذ 8080 ليتوافق مع إعدادات التوجيه في Fly.io
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
