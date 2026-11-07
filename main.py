from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os

app = FastAPI(title="Share Option App")

# Mount static files directory if it exists
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Pydantic models for authentication
class UserAuth(BaseModel):
    email: str | None = None
    phone: str | None = None
    password: str
    fullName: str | None = None

@app.get("/", response_class=HTMLResponse)
def read_root():
    if os.path.exists("platform.html"):
        with open("platform.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>Share Option Platform is Online!</h3>"

@app.get("/health")
def health_check():
    return {
        "status": "online",
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "admin_secret_configured": bool(os.getenv("ADMIN_SECRET"))
    }

# Authentication & Registration API Endpoints
@app.post("/api/register")
@app.post("/api/auth/register")
async def register(user: UserAuth):
    return {
        "status": "success",
        "message": "Account created successfully for Real Trading!",
        "user": {"email": user.email, "name": user.fullName}
    }

@app.post("/api/login")
@app.post("/api/auth/login")
async def login(user: UserAuth):
    return {
        "status": "success",
        "token": "live-real-account-jwt-token",
        "user": {"email": user.email}
    }
