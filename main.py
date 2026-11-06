from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os

app = FastAPI(title="Share Option App")

# معالج عام لالتقاط وعرض أي خطأ برمجي يحدث في الخادم بوضوح تام (Debug Mode)
@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    import traceback
    error_trace = traceback.format_exc()
    print(error_trace)  # لطباعة الخطأ في سجلات Fly.io
    return JSONResponse(
        status_code=500,
        content={
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": error_trace.splitlines()
        }
    )

@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Share Option API is running successfully!",
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "admin_secret_configured": bool(os.getenv("ADMIN_SECRET"))
    }

# إذا أردت إضافة مساراتك الحالية، يمكنك وضعها هنا أو استيرادها
