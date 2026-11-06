from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI(title="Share Option App")

# ربط مجلد الملفات الثابتة إن وجد
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    # محاولة قراءة ملف الواجهة platform.html إذا كان موجوداً
    if os.path.exists("platform.html"):
        with open("platform.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>Share Option Platform is Online, but platform.html was not found.</h3>"

@app.get("/health")
def health_check():
    return {
        "status": "online",
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "admin_secret_configured": bool(os.getenv("ADMIN_SECRET"))
    }
