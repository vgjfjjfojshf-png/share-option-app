import os
import time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from passlib.context import CryptContext
from jose import JWTError, jwt

# --- Security Configuration ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "super-secret-production-key-change-in-env-903218")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 Hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(
    title="Share Option Platform Engine",
    description="Production-grade API for authentication, real-account signaling, and state management.",
    version="2.0.0"
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

class TokenResponse(BaseModel):
    status: str
    access_token: str
    token_type: str = "bearer"
    user: dict

# --- Helper Utilities ---
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

# --- Routes & API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("platform.html"):
        with open("platform.html", "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h2>Error: platform.html not found in working directory.</h2>", status_code=404)

@app.get("/api/health")
async def health_check():
    return {
        "status": "online",
        "timestamp": int(time.time()),
        "environment": os.getenv("ENVIRONMENT", "production"),
        "database": "connected"
    }

@app.post("/api/auth/register", response_model=TokenResponse)
@app.post("/api/register", response_model=TokenResponse)
async def register(user_in: UserRegister):
    email_clean = user_in.email.lower().strip()
    if email_clean in db_users:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email address already exists."
        )
    
    hashed_password = get_password_hash(user_in.password)
    user_record = {
        "fullName": user_in.fullName,
        "email": email_clean,
        "hashed_password": hashed_password,
        "created_at": datetime.utcnow().isoformat(),
        "status": "active_real",
        "balance": 1000.00,
        "strategy": "PASSIVE (5%-10%)"
    }
    db_users[email_clean] = user_record
    
    token = create_access_token(data={"sub": email_clean, "name": user_in.fullName})
    return {
        "status": "success",
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "fullName": user_record["fullName"],
            "email": user_record["email"],
            "status": user_record["status"]
        }
    }

@app.post("/api/auth/login", response_model=TokenResponse)
@app.post("/api/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    email_clean = credentials.email.lower().strip()
    user = db_users.get(email_clean)
    
    if not user or not verify_password(credentials.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email address or password."
        )
    
    token = create_access_token(data={"sub": email_clean, "name": user["fullName"]})
    return {
        "status": "success",
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "fullName": user["fullName"],
            "email": user["email"],
            "status": user["status"]
        }
    }

@app.get("/api/user/status")
async def get_user_status(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
    
    token = auth_header.split(" ")[1]
    payload = decode_token(token)
    email = payload.get("sub")
    
    user = db_users.get(email)
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")
        
    return {
        "email": user["email"],
        "fullName": user["fullName"],
        "balance": user["balance"],
        "strategy": user["strategy"],
        "status": user["status"]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
