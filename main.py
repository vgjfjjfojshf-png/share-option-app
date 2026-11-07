import os
import time
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(
    title="Share Option Platform Engine",
    description="Production-grade API for real-account signaling.",
    version="2.1.0"
)

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- In-Memory Data Store ---
db_users = {}

# --- Pydantic Schemas ---
class UserRegister(BaseModel):
    fullName: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

# --- Simple Secure Hashing (No external libraries needed) ---
def hash_password(password: str) -> str:
    salt = "share-option-secure-salt-9812"
    return hashlib.sha256((password + salt).encode('utf-8')).hexdigest()

# --- Routes & API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("platform.html"):
        with open("platform.html", "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h2>Error: platform.html not found.</h2>", status_code=404)

@app.get("/api/health")
async def health_check():
    return {"status": "online", "timestamp": int(time.time())}

@app.post("/api/auth/register")
@app.post("/api/register")
async def register(user_in: UserRegister):
    email_clean = user_in.email.lower().strip()
    if email_clean in db_users:
        raise HTTPException(status_code=400, detail="Account with this email already exists.")
    
    hashed_pwd = hash_password(user_in.password)
    user_record = {
        "fullName": user_in.fullName,
        "email": email_clean,
        "password": hashed_pwd,
        "status": "active_real",
        "balance": 1000.00,
        "strategy": "PASSIVE (5%-10%)"
    }
    db_users[email_clean] = user_record
    
    return {
        "status": "success",
        "access_token": f"token-live-{email_clean}",
        "user": {
            "fullName": user_record["fullName"],
            "email": user_record["email"],
            "status": user_record["status"]
        }
    }

@app.post("/api/auth/login")
@app.post("/api/login")
async def login(credentials: UserLogin):
    email_clean = credentials.email.lower().strip()
    user = db_users.get(email_clean)
    
    if not user or user["password"] != hash_password(credentials.password):
        raise HTTPException(status_code=401, detail="Incorrect email address or password.")
    
    return {
        "status": "success",
        "access_token": f"token-live-{email_clean}",
        "user": {
            "fullName": user["fullName"],
            "email": user["email"],
            "status": user["status"]
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
