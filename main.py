"""
Share Option – signal engine & management backend (FastAPI + async SQLAlchemy).

Run:  ADMIN_SECRET=change-me uvicorn main:app --host 0.0.0.0 --port 8000
Env:  DATABASE_URL, ADMIN_SECRET (required), SIGNAL_TZ, POCKET_MASTER_ID,
      USDT_TRC20_ADDRESS, AUTO_CONFIRM_PAYMENTS (dev only), CORS_ORIGINS
"""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (DateTime, ForeignKey, Integer, Numeric, String,
                        UniqueConstraint, func, or_, select)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

warnings.filterwarnings("ignore", message=".*Decimal.*SQLite.*")  # use PostgreSQL in prod
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("share_option")

# ───────────────────────── Configuration ─────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./share_option.db")
if DATABASE_URL.startswith(("postgres://", "postgresql://")):  # hosts hand out plain postgres:// URLs
    DATABASE_URL = (DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
                    .replace("postgresql://", "postgresql+asyncpg://", 1).replace("sslmode=", "ssl="))
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
if not ADMIN_SECRET:
    raise RuntimeError("ADMIN_SECRET environment variable is required")
SIGNAL_TZ = ZoneInfo(os.getenv("SIGNAL_TZ", "UTC"))       # timezone used by the channel for "16:55"
POCKET_MASTER_ID = os.getenv("POCKET_MASTER_ID", "SET_MASTER_COPY_TRADE_ID")
USDT_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "SET_YOUR_TRC20_ADDRESS")
AUTO_CONFIRM = os.getenv("AUTO_CONFIRM_PAYMENTS", "false").lower() == "true"  # DEV ONLY: trusts the client
ONCHAIN_FLAG = os.getenv("ONCHAIN_VERIFY", "true").lower() == "true"


def onchain_enabled() -> bool:
    return ONCHAIN_FLAG and USDT_ADDRESS.startswith("T") and len(USDT_ADDRESS) == 34
CORS_ORIGINS = [o for o in os.getenv("CORS_ORIGINS", "*").split(",") if o]

# Your referral / direct registration links per broker (clients who sign up through these are yours)
BROKER_LINKS = {"POCKET_OPTION": os.getenv("LINK_POCKET_OPTION", "https://pocketoption.com/"),
                "QUOTEX": os.getenv("LINK_QUOTEX", "https://qxbroker.com/"),
                "IQ_OPTION": os.getenv("LINK_IQ_OPTION", "https://iqoption.com/")}

# Broker trading-terminal pages opened from the live signal window (verify these URLs for your region)
BROKER_TRADE_LINKS = {"POCKET_OPTION": os.getenv("TRADE_POCKET_OPTION", "https://pocketoption.com/en/cabinet/"),
                      "QUOTEX": os.getenv("TRADE_QUOTEX", "https://qxbroker.com/en/trade"),
                      "IQ_OPTION": os.getenv("TRADE_IQ_OPTION", "https://iqoption.com/traderoom")}
REQUIRE_VERIFIED_BROKER = os.getenv("REQUIRE_VERIFIED_BROKER", "true").lower() == "true"

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")  # enables "Continue with Google"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
RATE_LIMIT_DISABLED = os.getenv("RATE_LIMIT_DISABLED", "false").lower() == "true"  # tests only
COOKIE = "so_session"
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")  # optional, raises TronGrid rate limits
AUTO_CONFIRM_MAX = Decimal(os.getenv("AUTO_CONFIRM_MAX", "200"))  # larger on-chain payments wait for admin review
DEFAULT_PAYOUT_PCT = Decimal(os.getenv("DEFAULT_PAYOUT_PCT", "85"))  # payout used when results are auto-settled
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT on TRON (TRC-20)

DEFAULT_LINKS, DEFAULT_TRADE = dict(BROKER_LINKS), dict(BROKER_TRADE_LINKS)
WHATSAPP_URL = os.getenv("WHATSAPP_URL", "")   # support links are editable live from the admin Settings panel
TELEGRAM_URL = os.getenv("TELEGRAM_URL", "")

DEBT_LIMIT = Decimal("50.00")
EXEC_WINDOW = 60  # seconds after entry time during which a signal can still be executed
D = Decimal
PLANS = {
    # fee = commission on profit, loss_credit = share of verified losses credited against fees
    "PASSIVE":     {"fee": D("0.20"), "loss_credit": D("0.20")},
    "COMPOUNDING": {"fee": D("0.40"), "loss_credit": D("0.40")},
    "POWER_POOL":  {"fee": D("0.30"), "loss_credit": D("0.30")},  # pool rate not specified: assumed 30%
}
LADDER = (D("0.10"), D("0.20"), D("0.30"))  # Compounding stake steps: win_streak % 3
Plan = Literal["PASSIVE", "COMPOUNDING", "POWER_POOL"]
Broker = Literal["POCKET_OPTION", "QUOTEX", "IQ_OPTION"]


def q(x) -> Decimal:
    return D(x).quantize(D("0.01"), ROUND_HALF_UP)


def utcnow() -> datetime:  # naive UTC (SQLite-friendly)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


# ───────────────────────── Database ─────────────────────────
engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
Money = Numeric(18, 2)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), unique=True, index=True, nullable=True)
    google_sub: Mapped[Optional[str]] = mapped_column(String(40), unique=True, nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    terms_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    broker: Mapped[str] = mapped_column(String(20), default="POCKET_OPTION")
    plan: Mapped[str] = mapped_column(String(20), default="PASSIVE")
    balance: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    high_water_mark: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    unpaid_fees: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    loss_credit: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    share_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=D("0.00"))
    stake_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=D("5.00"))  # Passive: 5-10
    win_streak: Mapped[int] = mapped_column(Integer, default=0)
    account_status: Mapped[str] = mapped_column(String(12), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PoolState(Base):
    __tablename__ = "pool_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    master_balance: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("channel_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_text: Mapped[str] = mapped_column(String(2000))
    pair: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(4))
    expiry_min: Mapped[int] = mapped_column(Integer)
    instant: Mapped[bool] = mapped_column(default=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("user_id", "signal_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    stake: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(8), default="OPEN")  # OPEN | WIN | LOSS
    pnl: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    fee_charged: Mapped[Decimal] = mapped_column(Money, default=D("0.00"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    txid: Mapped[str] = mapped_column(String(80), unique=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    network: Mapped[str] = mapped_column(String(10), default="TRC20")
    from_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | CONFIRMED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Setting(Base):
    """Admin-editable site settings (links, wallet, master ID). Environment values are the defaults."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(300))


class BrokerAccount(Base):
    """A client's trading account at a broker: who they are, real vs demo, and whether they came via our link."""
    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("broker", "broker_uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    broker: Mapped[str] = mapped_column(String(20))
    broker_uid: Mapped[str] = mapped_column(String(40))
    account_type: Mapped[str] = mapped_column(String(5), default="REAL")  # REAL | DEMO
    plan: Mapped[str] = mapped_column(String(20), default="PASSIVE")
    via_link: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | VERIFIED | REJECTED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


async def get_db():
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def tx(db: AsyncSession):
    """Commit on success, rollback on any failure; unexpected errors become HTTP 500."""
    try:
        yield
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        log.exception("Transaction failed")
        raise HTTPException(500, "Internal error")


async def get_pool(db: AsyncSession) -> PoolState:
    pool = await db.get(PoolState, 1)
    if not pool:
        pool = PoolState(id=1, master_balance=D("0.00"))
        db.add(pool)
        await db.flush()
    return pool


async def broker_verified(db: AsyncSession, u: User) -> bool:
    """Signal access requires a VERIFIED broker account for the user's broker (when enforced)."""
    if not REQUIRE_VERIFIED_BROKER:
        return True
    return bool(await db.scalar(select(BrokerAccount.id).where(
        BrokerAccount.user_id == u.id, BrokerAccount.broker == u.broker, BrokerAccount.status == "VERIFIED")))


# ───────────────────────── Auth ─────────────────────────
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        cand = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(cand.hex(), h)
    except Exception:
        return False


_attempts: dict[str, list[float]] = {}


def rate_limit(key: str, limit: int, window: int) -> None:
    """Tiny in-memory limiter (per process). Use Redis / a reverse proxy limiter if you scale out."""
    if RATE_LIMIT_DISABLED:
        return
    now = time.monotonic()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many attempts. Please try again in a few minutes.")
    hits.append(now)
    _attempts[key] = hits


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: AsyncSession, user: User, request: Request, response: Response) -> None:
    """Creates a server-side session and sets an httpOnly cookie (no keys or tokens exposed to page scripts)."""
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(user_id=user.id, token_hash=_th(token), expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


async def get_user(request: Request, authorization: Optional[str] = Header(None),
                   db: AsyncSession = Depends(get_db)) -> User:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    token = token or request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in")
    sess = await db.scalar(select(AuthSession).where(
        AuthSession.token_hash == _th(token), AuthSession.expires_at > utcnow()))
    user = await db.get(User, sess.user_id) if sess else None
    if not user:
        raise HTTPException(401, "Session expired. Please sign in again.")
    return user


async def require_admin(x_admin_secret: str = Header(...)):
    if not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(403, "Forbidden")


# ───────────────────────── Signal parser ─────────────────────────
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "BTC", "ETH", "XAU",
              "XAG", "TRY", "SGD", "HKD", "CNH", "MXN", "NOK", "SEK", "ZAR", "BRL", "INR"}
_PAIR = re.compile(r"(?<![A-Z])([A-Z]{3})\s*[/\-]?\s*([A-Z]{3})(?![A-Z])")
_OTC = re.compile(r"(?<![A-Z])OTC(?![A-Z])")
_DIR = re.compile(r"(?<![A-Z])(?:(CALL|BUY|HIGHER|UP)|(PUT|SELL|LOWER|DOWN))(?![A-Z])|(🟢|⬆|📈)|(🔴|⬇|📉)")
_EXP = [re.compile(r"(?<![\d:])(\d{1,2})\s*(?:MINUTES?|MINS?|M)(?![A-Z])"),
        re.compile(r"(?<![A-Z])M(\d{1,2})(?!\d)"),
        re.compile(r"EXPIR(?:Y|ATION)?\s*[:=]?\s*(\d{1,2})(?!\d)")]
_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_NOW = re.compile(r"(?<![A-Z])(NOW|INSTANT|IMMEDIATE)(?![A-Z])")


@dataclass
class Parsed:
    pair: str
    direction: str  # CALL | PUT
    expiry_min: int
    instant: bool
    entry_time: datetime  # naive UTC


def parse_signal(text: str) -> tuple[Optional[Parsed], Optional[str]]:
    """Returns (parsed, None) or (None, reason)."""
    up = text.upper()
    pair = next((f"{a}/{b}" for a, b in _PAIR.findall(up) if a in CURRENCIES and b in CURRENCIES), None)
    if not pair:
        return None, "no_pair"
    if _OTC.search(up):
        pair += " OTC"

    m = _DIR.search(up)
    if not m:
        return None, "no_direction"
    direction = "CALL" if (m.group(1) or m.group(3)) else "PUT"

    expiry = None
    for rx in _EXP:
        for val in rx.findall(up):
            if int(val) in (1, 5, 15):
                expiry = int(val)
                break
        if expiry:
            break
    if not expiry:
        return None, "no_expiry"

    now = datetime.now(SIGNAL_TZ)
    t = _TIME.search(up)
    if t and not _NOW.search(up):
        cand = now.replace(hour=int(t.group(1)), minute=int(t.group(2)), second=0, microsecond=0)
        delta = cand - now
        if delta < timedelta(hours=-12):
            cand += timedelta(days=1)
        elif delta < timedelta(seconds=-EXEC_WINDOW):
            return None, "stale_entry_time"
        entry, instant = cand.astimezone(timezone.utc).replace(tzinfo=None), False
    else:
        entry, instant = utcnow(), True
    return Parsed(pair, direction, expiry, instant, entry), None


_RES_WORD = re.compile(r"(?<![A-Z])(WIN|WON|PROFIT|LOSS|LOST|LOSE)(?![A-Z])")


def parse_result(text: str) -> Optional[tuple[str, str]]:
    """Detects channel result messages such as 'EUR/USD ✅ WIN' / 'GBP/JPY OTC ❌ LOSS'. Returns (pair, WIN|LOSS).
    A message with a trade direction only counts as a result if it has both a result word and a ✅/❌ mark."""
    up = text.upper()
    pair = next((f"{a}/{b}" for a, b in _PAIR.findall(up) if a in CURRENCIES and b in CURRENCIES), None)
    if not pair:
        return None
    if _OTC.search(up):
        pair += " OTC"
    word, mark = _RES_WORD.search(up), ("✅" in up or "❌" in up)
    if word is None and not mark:
        return None
    if _DIR.search(up) and not (word and mark):
        return None
    win = word.group(1) in ("WIN", "WON", "PROFIT") if word else "✅" in up
    return pair, "WIN" if win else "LOSS"


# ───────────────────────── Business engine ─────────────────────────
def user_value(u: User, pool: PoolState) -> Decimal:
    if u.plan == "POWER_POOL":
        return q(pool.master_balance * u.share_pct / 100)
    return u.balance


def compute_stake(u: User, pool: PoolState) -> tuple[Decimal, Decimal]:
    """Returns (stake, stake_pct_of_capital)."""
    base = user_value(u, pool)
    if u.plan == "PASSIVE":  # user-chosen fixed stake between 5% and 10%
        pct = min(max(u.stake_pct, D("5")), D("10")) / 100
    elif u.plan == "COMPOUNDING":  # 10% → 20% → 30%, then back to 10% of the *current* balance; a loss resets to 10%
        pct = LADDER[u.win_streak % 3]
    else:
        pct = D("0.05")
    return min(q(base * pct), base), pct


def evaluate_status(u: User) -> None:
    if u.unpaid_fees >= DEBT_LIMIT:
        u.account_status = "SUSPENDED"
    elif u.account_status == "SUSPENDED":
        u.account_status = "ACTIVE"


def apply_settlement(u: User, pnl: Decimal, pool: PoolState, demo: bool = False) -> Decimal:
    """Applies a verified trade result: balance, fee accrual, loss credit, suspension."""
    cfg, fee = PLANS[u.plan], D("0.00")
    if u.plan == "POWER_POOL":
        pool.master_balance = q(pool.master_balance + pnl)
    else:
        u.balance = q(u.balance + pnl)
    if pnl > 0:
        if not demo:  # demo accounts hold no real profit, so no commission
            fee = q(pnl * cfg["fee"])
            u.unpaid_fees = q(u.unpaid_fees + fee)
        u.win_streak += 1
    elif pnl < 0:
        if not demo:
            u.loss_credit = q(u.loss_credit + (-pnl) * cfg["loss_credit"])
        u.win_streak = 0
    offset = min(u.loss_credit, u.unpaid_fees)  # loss credit offsets outstanding commission
    u.loss_credit, u.unpaid_fees = q(u.loss_credit - offset), q(u.unpaid_fees - offset)
    u.high_water_mark = max(u.high_water_mark, user_value(u, pool))
    evaluate_status(u)
    return fee


def broker_payload(u: User, s: Signal, stake: Decimal) -> dict:
    base = {"asset": s.pair, "direction": s.direction, "expiry_seconds": s.expiry_min * 60,
            "stake": float(stake), "entry_time": iso(s.entry_time), "signal_id": s.id}
    if u.broker == "POCKET_OPTION":
        return {"type": "COPY_TRADE", "broker": "POCKET_OPTION",
                "master_copy_trade_id": POCKET_MASTER_ID, **base}
    return {"type": "WEBHOOK_PUSH", "broker": u.broker,
            "notification": {"title": f"{s.direction} {s.pair}",
                             "body": f"Stake ${stake} · {s.expiry_min}m · "
                                     f"{'NOW' if s.instant else s.entry_time.strftime('%H:%M')} UTC"},
            **base}


def status_view(u: User, pool: PoolState, verified: bool = True) -> dict:
    stake, pct = compute_stake(u, pool)
    return {"user_id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "broker": u.broker, "plan": u.plan,
            "fee_rate": float(PLANS[u.plan]["fee"]), "balance": float(user_value(u, pool)),
            "high_water_mark": float(u.high_water_mark), "unpaid_fees": float(u.unpaid_fees),
            "loss_credit": float(u.loss_credit), "debt_limit": float(DEBT_LIMIT),
            "debt_pct": float(min(u.unpaid_fees / DEBT_LIMIT * 100, D(100))),
            "account_status": u.account_status, "can_trade": u.account_status == "ACTIVE" and verified, "broker_verified": verified,
            "block_reason": "SUSPENDED" if u.account_status != "ACTIVE" else (None if verified else "BROKER_NOT_VERIFIED"),
            "share_pct": float(u.share_pct), "stake_pct": float(u.stake_pct), "pool_balance": float(pool.master_balance),
            "next_stake": float(stake), "next_stake_pct": float(pct * 100),
            "deposit_address": USDT_ADDRESS, "network": "TRC20"}


async def full_status(db: AsyncSession, u: User) -> dict:
    return status_view(u, await get_pool(db), await broker_verified(db, u))


def signal_view(s: Signal, stake: Decimal, executed: bool) -> dict:
    return {"id": s.id, "pair": s.pair, "direction": s.direction, "expiry_min": s.expiry_min,
            "instant": s.instant, "entry_time": iso(s.entry_time),
            "expires_at": iso(s.entry_time + timedelta(seconds=EXEC_WINDOW)),
            "stake": float(stake), "executed": executed}


# ───────────────────────── App & schemas ─────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        await get_pool(db)
        await db.commit()
    await load_settings()
    watcher = asyncio.create_task(payment_watcher()) if ONCHAIN_FLAG else None
    yield
    if watcher:
        watcher.cancel()
    await engine.dispose()


app = FastAPI(title="Share Option", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: Optional[str] = Field(default=None, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: Optional[str] = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")  # E.164, e.g. +212612345678
    password: str = Field(min_length=8, max_length=128)
    accept_terms: bool = False


class LoginIn(BaseModel):
    identifier: str = Field(min_length=3, max_length=120)  # email or +phone
    password: str = Field(min_length=1, max_length=128)


class GoogleIn(BaseModel):
    credential: str = Field(min_length=20, max_length=4096)


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    channel_id: Optional[str] = None
    message_id: Optional[int] = None


class PlanIn(BaseModel):
    plan: Plan
    stake_pct: Optional[Decimal] = Field(default=None, ge=5, le=10, description="Passive plan stake, 5-10%")


class SettleIn(BaseModel):
    amount: Decimal = Field(gt=0, le=100000)
    txid: str = Field(min_length=16, max_length=80)
    from_address: Optional[str] = Field(default=None, pattern=r"^T[1-9A-HJ-NP-Za-km-z]{33}$")  # sender wallet, enables auto-verify
    network: Literal["TRC20"] = "TRC20"


class ResultIn(BaseModel):
    outcome: Literal["WIN", "LOSS"]
    payout_pct: Decimal = Field(default=D("85"), ge=1, le=100)


class AdjustIn(BaseModel):
    balance: Optional[Decimal] = Field(default=None, ge=0)
    share_pct: Optional[Decimal] = Field(default=None, ge=0, le=100)
    broker: Optional[Broker] = None


class PoolIn(BaseModel):
    master_balance: Decimal = Field(ge=0)


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_to_hex20(addr: str) -> str:
    n = 0
    for ch in addr:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes(25, "big")
    if hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] != raw[21:]:
        raise ValueError("bad address checksum")
    return raw[1:21].hex()


def _addr_hex(a) -> Optional[str]:
    a = str(a)
    try:
        if a.startswith("T") and len(a) == 34:
            return b58_to_hex20(a)
        a = a.lower().removeprefix("0x")
        return a[-40:] if len(a) >= 40 else None
    except Exception:
        return None


async def verify_usdt_tx(txid: str, from_address: str) -> Optional[Decimal]:
    """USDT confirmed on TRON from `from_address` to our receiving address in this transaction, or None."""
    if not onchain_enabled() or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        return None
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            r = await hc.get(f"https://api.trongrid.io/v1/transactions/{txid}/events",
                             params={"only_confirmed": "true"}, headers=headers)
        if r.status_code != 200:
            return None
        events = r.json().get("data", [])
    except (httpx.HTTPError, ValueError):
        return None
    want_to, want_from = _addr_hex(USDT_ADDRESS), _addr_hex(from_address)
    total = D(0)
    for ev in events:
        res = ev.get("result", {})
        if (ev.get("event_name") == "Transfer" and ev.get("contract_address") == USDT_CONTRACT
                and _addr_hex(res.get("to", "")) == want_to and _addr_hex(res.get("from", "")) == want_from):
            total += D(str(res.get("value", "0"))) / D(10 ** 6)
    return total if total > 0 else None


async def payment_watcher():
    """Re-checks PENDING payments every 2 minutes until the transfer confirms on-chain (48h window)."""
    while True:
        try:
            await asyncio.sleep(120)
            async with SessionLocal() as db:
                pend = (await db.scalars(select(Payment).where(
                    Payment.status == "PENDING", Payment.from_address.is_not(None),
                    Payment.created_at > utcnow() - timedelta(hours=48)))).all()
                for pay in pend:
                    amt = await verify_usdt_tx(pay.txid, pay.from_address)
                    if amt and q(amt) <= AUTO_CONFIRM_MAX:
                        pay.amount = q(amt)
                        await apply_payment(db, await db.get(User, pay.user_id), pay)
                        log.info("Payment %s confirmed on-chain: %s USDT", pay.id, pay.amount)
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("payment watcher failed")


async def apply_payment(db: AsyncSession, user: User, pay: Payment) -> Decimal:
    applied = min(pay.amount, user.unpaid_fees)
    user.unpaid_fees = q(user.unpaid_fees - applied)
    pay.status = "CONFIRMED"
    evaluate_status(user)  # lifts suspension once debt < $50
    return applied


# ───────────────────────── Public / user endpoints ─────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "time": iso(utcnow())}


@app.get("/api/config/public")
async def public_config():
    return {"google_client_id": GOOGLE_CLIENT_ID, "whatsapp": WHATSAPP_URL or None, "telegram": TELEGRAM_URL or None}


@app.post("/api/auth/register")
async def register(body: RegisterIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if not (body.email or body.phone):
        raise HTTPException(422, "Provide an email or a phone number")
    if not body.accept_terms:
        raise HTTPException(422, "Please accept the Terms, Privacy Policy and Risk Disclosure")
    rate_limit(f"reg:{client_ip(request)}", limit=20, window=3600)
    email = body.email.strip().lower() if body.email else None
    pw_hash = await asyncio.to_thread(hash_pw, body.password)
    async with tx(db):
        conds = [c for c in (User.email == email if email else None, User.phone == body.phone if body.phone else None) if c is not None]
        if await db.scalar(select(User.id).where(or_(*conds))):
            raise HTTPException(409, "An account with this email or phone already exists")
        user = User(name=body.name.strip(), email=email, phone=body.phone, password_hash=pw_hash, terms_at=utcnow())
        db.add(user)
        await db.flush()
        start_session(db, user, request, response)
    return {"user_id": user.id}


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    ident = body.identifier.strip()
    rate_limit(f"login:{client_ip(request)}:{ident.lower()}", limit=8, window=300)
    user = await db.scalar(select(User).where(User.email == ident.lower() if "@" in ident else User.phone == ident))
    ok = bool(user and user.password_hash and await asyncio.to_thread(check_pw, body.password, user.password_hash))
    if not ok:
        raise HTTPException(401, "Invalid email/phone or password")
    async with tx(db):
        start_session(db, user, request, response)
    return {"user_id": user.id}


@app.post("/api/auth/google")
async def google_login(body: GoogleIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured")
    rate_limit(f"google:{client_ip(request)}", limit=30, window=300)
    try:
        async with httpx.AsyncClient(timeout=8) as hc:
            r = await hc.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": body.credential})
        info = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not reach Google. Try again.")
    if not info or info.get("aud") != GOOGLE_CLIENT_ID or str(info.get("email_verified")).lower() != "true":
        raise HTTPException(401, "Google sign-in failed")
    sub, email = info["sub"], (info.get("email") or "").lower()
    async with tx(db):
        user = await db.scalar(select(User).where(User.google_sub == sub))
        if not user and email:
            user = await db.scalar(select(User).where(User.email == email))
            if user:
                user.google_sub = sub  # link Google to the existing email account
        if not user:
            user = User(name=(info.get("name") or email.split("@")[0])[:80], email=email or None, google_sub=sub, terms_at=utcnow())
            db.add(user)
            await db.flush()
        start_session(db, user, request, response)
    return {"user_id": user.id}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    async with tx(db):
        if token:
            sess = await db.scalar(select(AuthSession).where(AuthSession.token_hash == _th(token)))
            if sess:
                await db.delete(sess)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/user/status")
async def user_status(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    try:
        return await full_status(db, user)
    except Exception:
        log.exception("status failed")
        raise HTTPException(500, "Internal error")


@app.post("/api/user/plan")
async def set_plan(body: PlanIn, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    async with tx(db):
        if body.plan == "POWER_POOL" and user.share_pct <= 0:
            raise HTTPException(400, "No Power Share allocation on your account yet. Contact support.")
        open_trades = await db.scalar(select(func.count(Trade.id)).where(
            Trade.user_id == user.id, Trade.status == "OPEN"))
        if open_trades:
            raise HTTPException(409, "Close open trades before switching plans")
        user.plan, user.win_streak = body.plan, 0
        if body.stake_pct is not None:
            user.stake_pct = q(body.stake_pct)
        pool = await get_pool(db)
    return await full_status(db, user)


@app.post("/api/user/settle-fees")
async def settle_fees(body: SettleIn, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    """Records a USDT (TRC-20) payment. With a sender wallet, the transfer is verified on the TRON network and
    credited automatically (up to AUTO_CONFIRM_MAX); otherwise it waits for admin review. A background job
    re-checks pending transfers until they confirm."""
    txid = body.txid.strip().lower()
    if await db.scalar(select(Payment.id).where(Payment.txid == txid)):
        raise HTTPException(409, "This transaction ID was already submitted")
    amount, confirmed = q(body.amount), AUTO_CONFIRM
    if not confirmed and body.from_address:
        onchain = await verify_usdt_tx(txid, body.from_address)
        if onchain and q(onchain) <= AUTO_CONFIRM_MAX:
            amount, confirmed = q(onchain), True  # credit what actually arrived, not what was typed
    async with tx(db):
        pay = Payment(user_id=user.id, txid=txid, amount=amount, network=body.network, from_address=body.from_address)
        db.add(pay)
        applied = await apply_payment(db, user, pay) if confirmed else D("0.00")
    return {"payment_status": pay.status, "applied": float(applied), "status": await full_status(db, user)}


@app.get("/api/signals")
async def list_signals(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    try:
        pool = await get_pool(db)
        if user.account_status != "ACTIVE":  # suspended accounts receive nothing
            return {"blocked": True, "reason": "SUSPENDED", "signals": []}
        if not await broker_verified(db, user):
            return {"blocked": True, "reason": "BROKER_NOT_VERIFIED", "signals": []}
        since = utcnow() - timedelta(hours=6)
        sigs = (await db.scalars(select(Signal).where(Signal.entry_time >= since)
                                 .order_by(Signal.entry_time.desc()).limit(30))).all()
        done = set((await db.scalars(select(Trade.signal_id).where(Trade.user_id == user.id))).all())
        stake, _ = compute_stake(user, pool)
        return {"blocked": False, "signals": [signal_view(s, stake, s.id in done) for s in sigs]}
    except Exception:
        log.exception("list signals failed")
        raise HTTPException(500, "Internal error")


@app.post("/api/signals/{signal_id}/execute")
async def execute_signal(signal_id: int, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    async with tx(db):
        sig = await db.get(Signal, signal_id)
        if not sig:
            raise HTTPException(404, "Signal not found")
        evaluate_status(user)
        if user.account_status != "ACTIVE":
            await db.commit()  # persist the suspension before refusing
            raise HTTPException(403, "Account suspended: settle unpaid fees to resume trading")
        if not await broker_verified(db, user):
            raise HTTPException(403, "Broker account not verified yet")
        if utcnow() > sig.entry_time + timedelta(seconds=EXEC_WINDOW):
            raise HTTPException(410, "Signal expired")
        if await db.scalar(select(Trade.id).where(Trade.user_id == user.id, Trade.signal_id == sig.id)):
            raise HTTPException(409, "Already executed")
        pool = await get_pool(db)
        stake, _ = compute_stake(user, pool)
        if stake <= 0:
            raise HTTPException(400, "Insufficient balance")
        db.add(Trade(user_id=user.id, signal_id=sig.id, stake=stake))
    return broker_payload(user, sig, stake)


# ───────────────────────── Listener / admin endpoints ─────────────────────────
@app.post("/api/signals/parse", dependencies=[Depends(require_admin)])
async def parse_endpoint(body: ParseIn, db: AsyncSession = Depends(get_db)):
    """Called by telegram_listener.py. Parses, stores, and fans out to eligible users."""
    res = parse_result(body.text)  # channel result message ("EUR/USD ✅ WIN") settles the matching open signal
    if res:
        pair, outcome = res
        async with tx(db):
            since = utcnow() - timedelta(hours=3)
            for sig in (await db.scalars(select(Signal).where(
                    Signal.pair == pair, Signal.created_at > since).order_by(Signal.id.asc()))).all():
                if sig.entry_time + timedelta(minutes=sig.expiry_min) > utcnow() + timedelta(seconds=60):
                    continue  # that trade has not expired yet
                settled, suspended = await settle_trades(db, sig.id, outcome, DEFAULT_PAYOUT_PCT)
                if settled:
                    return {"status": "RESULT", "signal_id": sig.id, "outcome": outcome,
                            "settled": settled, "suspended_users": suspended}
        return {"status": "IGNORED", "reason": "result_without_open_trades"}
    parsed, reason = parse_signal(body.text)
    if not parsed:
        return {"status": "IGNORED", "reason": reason}
    async with tx(db):
        if body.channel_id and body.message_id is not None:
            dup = await db.scalar(select(Signal.id).where(
                Signal.channel_id == body.channel_id, Signal.message_id == body.message_id))
            if dup:
                return {"status": "DUPLICATE", "signal_id": dup}
        sig = Signal(channel_id=body.channel_id, message_id=body.message_id, raw_text=body.text,
                     pair=parsed.pair, direction=parsed.direction, expiry_min=parsed.expiry_min,
                     instant=parsed.instant, entry_time=parsed.entry_time)
        db.add(sig)
        await db.flush()
        pool = await get_pool(db)
        executions = []
        verified = {(a.user_id, a.broker) for a in (await db.scalars(
            select(BrokerAccount).where(BrokerAccount.status == "VERIFIED"))).all()}
        for u in (await db.scalars(select(User))).all():
            evaluate_status(u)
            if u.account_status != "ACTIVE":
                executions.append({"user_id": u.id, "status": "BLOCKED", "reason": "DEBT_LIMIT_REACHED"})
                continue
            if REQUIRE_VERIFIED_BROKER and (u.id, u.broker) not in verified:
                executions.append({"user_id": u.id, "status": "BLOCKED", "reason": "BROKER_NOT_VERIFIED"})
                continue
            stake, _ = compute_stake(u, pool)
            if stake <= 0:
                executions.append({"user_id": u.id, "status": "SKIPPED", "reason": "INSUFFICIENT_BALANCE"})
                continue
            executions.append({"user_id": u.id, "status": "READY", "plan": u.plan,
                               "stake": float(stake), "payload": broker_payload(u, sig, stake)})
    return {"status": "OK", "signal": {"id": sig.id, "pair": sig.pair, "direction": sig.direction,
            "expiry_min": sig.expiry_min, "instant": sig.instant, "entry_time": iso(sig.entry_time)},
            "ready": sum(e["status"] == "READY" for e in executions),
            "blocked": sum(e["status"] == "BLOCKED" for e in executions), "executions": executions}


async def settle_trades(db: AsyncSession, signal_id: int, outcome: str, payout_pct: Decimal) -> tuple[int, int]:
    """Settles every open trade of a signal: balance, fee accrual, loss credit, suspension. Returns (settled, suspended)."""
    pool = await get_pool(db)
    trades = (await db.scalars(select(Trade).where(Trade.signal_id == signal_id, Trade.status == "OPEN"))).all()
    suspended = 0
    for t in trades:
        u = await db.get(User, t.user_id)
        t.pnl = q(t.stake * payout_pct / 100) if outcome == "WIN" else -t.stake
        t.status, t.settled_at = outcome, utcnow()
        acct = await db.scalar(select(BrokerAccount).where(
            BrokerAccount.user_id == u.id, BrokerAccount.broker == u.broker))
        t.fee_charged = apply_settlement(u, t.pnl, pool, demo=bool(acct and acct.account_type == "DEMO"))
        suspended += u.account_status == "SUSPENDED"
    return len(trades), suspended


@app.post("/api/admin/signals/{signal_id}/result", dependencies=[Depends(require_admin)])
async def settle_signal(signal_id: int, body: ResultIn, db: AsyncSession = Depends(get_db)):
    """Manual override: verified result for a signal."""
    async with tx(db):
        settled, suspended = await settle_trades(db, signal_id, body.outcome, body.payout_pct)
    return {"settled": settled, "suspended_users": suspended}


@app.post("/api/admin/payments/{payment_id}/confirm", dependencies=[Depends(require_admin)])
async def confirm_payment(payment_id: int, db: AsyncSession = Depends(get_db)):
    async with tx(db):
        pay = await db.get(Payment, payment_id)
        if not pay or pay.status != "PENDING":
            raise HTTPException(404, "Pending payment not found")
        applied = await apply_payment(db, await db.get(User, pay.user_id), pay)
    return {"confirmed": pay.id, "applied": float(applied)}


@app.post("/api/admin/users/{user_id}/adjust", dependencies=[Depends(require_admin)])
async def adjust_user(user_id: int, body: AdjustIn, db: AsyncSession = Depends(get_db)):
    async with tx(db):
        u = await db.get(User, user_id)
        if not u:
            raise HTTPException(404, "User not found")
        if body.balance is not None:
            u.balance = q(body.balance)
        if body.share_pct is not None:
            u.share_pct = q(body.share_pct)
        if body.broker:
            u.broker = body.broker
        pool = await get_pool(db)
    return status_view(u, pool)


@app.post("/api/admin/pool", dependencies=[Depends(require_admin)])
async def set_pool(body: PoolIn, db: AsyncSession = Depends(get_db)):
    async with tx(db):
        pool = await get_pool(db)
        pool.master_balance = q(body.master_balance)
    return {"master_balance": float(pool.master_balance)}


# ───────────────────────── Broker registration & commission tracking ─────────────────────────
class BrokerAccountIn(BaseModel):
    broker: Broker
    broker_uid: str = Field(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")
    account_type: Literal["REAL", "DEMO"]
    plan: Literal["PASSIVE", "COMPOUNDING", "POWER_POOL"]
    via_link: bool = False


class VerifyIn(BaseModel):
    status: Literal["VERIFIED", "REJECTED"]


def account_view(a: BrokerAccount) -> dict:
    return {"id": a.id, "user_id": a.user_id, "broker": a.broker, "broker_uid": a.broker_uid,
            "account_type": a.account_type, "plan": a.plan, "via_link": a.via_link,
            "status": a.status, "created_at": iso(a.created_at)}


@app.get("/api/config/brokers")
async def broker_links():
    return BROKER_LINKS


@app.get("/api/user/broker-account")
async def my_broker_accounts(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    try:
        rows = (await db.scalars(select(BrokerAccount).where(BrokerAccount.user_id == user.id)
                                 .order_by(BrokerAccount.id.desc()))).all()
        return {"accounts": [account_view(a) for a in rows]}
    except Exception:
        log.exception("broker accounts failed")
        raise HTTPException(500, "Internal error")


@app.post("/api/user/broker-account")
async def register_broker_account(body: BrokerAccountIn, user: User = Depends(get_user),
                                  db: AsyncSession = Depends(get_db)):
    uid = body.broker_uid.strip()
    async with tx(db):
        taken = await db.scalar(select(BrokerAccount.id).where(
            BrokerAccount.broker == body.broker, BrokerAccount.broker_uid == uid, BrokerAccount.user_id != user.id))
        if taken:
            raise HTTPException(409, "This broker account is already linked to another client")
        acct = await db.scalar(select(BrokerAccount).where(
            BrokerAccount.user_id == user.id, BrokerAccount.broker == body.broker))
        if acct:
            if acct.broker_uid != uid or acct.account_type != body.account_type:
                acct.status, acct.verified_at = "PENDING", None  # changed details need re-verification
            acct.broker_uid, acct.account_type, acct.plan = uid, body.account_type, body.plan
            acct.via_link = acct.via_link or body.via_link
        else:
            acct = BrokerAccount(user_id=user.id, broker=body.broker, broker_uid=uid,
                                 account_type=body.account_type, plan=body.plan, via_link=body.via_link)
            db.add(acct)
        user.broker = body.broker
        await db.flush()
    return account_view(acct)


@app.post("/api/admin/broker-accounts/{account_id}/verify", dependencies=[Depends(require_admin)])
async def verify_broker_account(account_id: int, body: VerifyIn, db: AsyncSession = Depends(get_db)):
    async with tx(db):
        acct = await db.get(BrokerAccount, account_id)
        if not acct:
            raise HTTPException(404, "Broker account not found")
        acct.status = body.status
        acct.verified_at = utcnow() if body.status == "VERIFIED" else None
    return account_view(acct)


@app.get("/api/admin/clients", dependencies=[Depends(require_admin)])
async def admin_clients(db: AsyncSession = Depends(get_db)):
    """Every client with broker account, account type and commission: accrued, collected, credited, outstanding."""
    try:
        dec = lambda x: q(D(str(x or 0)))
        pool = await get_pool(db)
        users = (await db.scalars(select(User).order_by(User.id.desc()))).all()
        accrued = {r[0]: dec(r[1]) for r in (await db.execute(
            select(Trade.user_id, func.sum(Trade.fee_charged)).group_by(Trade.user_id))).all()}
        paid = {r[0]: dec(r[1]) for r in (await db.execute(
            select(Payment.user_id, func.sum(Payment.amount)).where(Payment.status == "CONFIRMED")
            .group_by(Payment.user_id))).all()}
        accts: dict[int, list] = {}
        for a in (await db.scalars(select(BrokerAccount).order_by(BrokerAccount.id.desc()))).all():
            accts.setdefault(a.user_id, []).append(account_view(a))
        rows, totals = [], {"accrued": D(0), "collected": D(0), "outstanding": D(0), "credited": D(0)}
        by_plan: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for u in users:
            acc, pd_ = accrued.get(u.id, D("0.00")), paid.get(u.id, D("0.00"))
            credited = max(acc - pd_ - u.unpaid_fees, D("0.00"))
            mine = accts.get(u.id, [])
            by_plan[u.plan] = by_plan.get(u.plan, 0) + 1
            if mine:
                by_type[mine[0]["account_type"]] = by_type.get(mine[0]["account_type"], 0) + 1
            totals["accrued"] += acc; totals["collected"] += pd_
            totals["outstanding"] += u.unpaid_fees; totals["credited"] += credited
            rows.append({"user_id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "plan": u.plan, "fee_rate": float(PLANS[u.plan]["fee"]),
                         "balance": float(user_value(u, pool)), "account_status": u.account_status,
                         "joined": iso(u.created_at), "broker_accounts": mine,
                         "commission": {"accrued": float(acc), "collected": float(pd_),
                                        "credited": float(credited), "outstanding": float(u.unpaid_fees)}})
        return {"pool_balance": float(pool.master_balance), "totals": {k: float(v) for k, v in totals.items()},
                "clients": len(users), "by_plan": by_plan, "by_account_type": by_type, "rows": rows}
    except Exception:
        log.exception("admin clients failed")
        raise HTTPException(500, "Internal error")


# ───────────────────────── History & admin lists ─────────────────────────
@app.get("/api/user/trades")
async def my_trades(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    try:
        rows = (await db.execute(select(Trade, Signal).join(Signal, Signal.id == Trade.signal_id)
                                 .where(Trade.user_id == user.id).order_by(Trade.id.desc()).limit(50))).all()
        return {"trades": [{"id": t.id, "pair": s.pair, "direction": s.direction, "expiry_min": s.expiry_min,
                            "stake": float(t.stake), "status": t.status, "pnl": float(t.pnl),
                            "fee": float(t.fee_charged), "time": iso(t.created_at)} for t, s in rows]}
    except Exception:
        log.exception("trades failed")
        raise HTTPException(500, "Internal error")


@app.get("/api/user/payments")
async def my_payments(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    try:
        rows = (await db.scalars(select(Payment).where(Payment.user_id == user.id)
                                 .order_by(Payment.id.desc()).limit(50))).all()
        return {"payments": [{"id": p.id, "amount": float(p.amount), "txid": p.txid, "status": p.status,
                              "time": iso(p.created_at)} for p in rows]}
    except Exception:
        log.exception("payments failed")
        raise HTTPException(500, "Internal error")


@app.get("/api/admin/signals", dependencies=[Depends(require_admin)])
async def admin_signals(db: AsyncSession = Depends(get_db)):
    try:
        sigs = (await db.scalars(select(Signal).order_by(Signal.id.desc()).limit(40))).all()
        tot = {r[0]: r[1] for r in (await db.execute(
            select(Trade.signal_id, func.count(Trade.id)).group_by(Trade.signal_id))).all()}
        opn = {r[0]: r[1] for r in (await db.execute(
            select(Trade.signal_id, func.count(Trade.id)).where(Trade.status == "OPEN")
            .group_by(Trade.signal_id))).all()}
        return {"signals": [{"id": s.id, "pair": s.pair, "direction": s.direction, "expiry_min": s.expiry_min,
                             "instant": s.instant, "entry_time": iso(s.entry_time), "raw": s.raw_text[:140],
                             "trades": tot.get(s.id, 0), "open": opn.get(s.id, 0)} for s in sigs]}
    except Exception:
        log.exception("admin signals failed")
        raise HTTPException(500, "Internal error")


@app.get("/api/admin/payments", dependencies=[Depends(require_admin)])
async def admin_payments(db: AsyncSession = Depends(get_db)):
    try:
        rows = (await db.execute(select(Payment, User.name).join(User, User.id == Payment.user_id)
                                 .order_by(Payment.id.desc()).limit(60))).all()
        return {"payments": [{"id": p.id, "user_id": p.user_id, "user": n, "amount": float(p.amount),
                              "txid": p.txid, "network": p.network, "status": p.status,
                              "time": iso(p.created_at)} for p, n in rows]}
    except Exception:
        log.exception("admin payments failed")
        raise HTTPException(500, "Internal error")


class BalanceIn(BaseModel):
    balance: Decimal = Field(ge=0, le=10_000_000)


@app.get("/api/config/trade-links")
async def trade_links():
    return BROKER_TRADE_LINKS


@app.post("/api/user/balance")
async def set_balance(body: BalanceIn, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    """Client syncs their real broker balance so percentage-based entries (esp. Compounding) stay accurate."""
    async with tx(db):
        if user.plan == "POWER_POOL":
            raise HTTPException(409, "Power Share value follows the master pool balance")
        if await db.scalar(select(func.count(Trade.id)).where(Trade.user_id == user.id, Trade.status == "OPEN")):
            raise HTTPException(409, "Update your balance after open trades are settled")
        user.balance = q(body.balance)
        user.high_water_mark = max(user.high_water_mark, user.balance)
    return await full_status(db, user)


# ───────────────────────── Admin site settings (links, wallet, master ID) ─────────────────────────
SETTING_KEYS = ("link_pocket_option", "link_quotex", "link_iq_option", "trade_pocket_option", "trade_quotex",
                "trade_iq_option", "whatsapp_url", "telegram_url", "pocket_master_id", "usdt_address")
_CODES = {"pocket_option": "POCKET_OPTION", "quotex": "QUOTEX", "iq_option": "IQ_OPTION"}
_PLACEHOLDER_MASTER, _PLACEHOLDER_USDT = "SET_MASTER_COPY_TRADE_ID", "SET_YOUR_TRC20_ADDRESS"


def apply_settings(v: dict) -> None:
    global POCKET_MASTER_ID, USDT_ADDRESS, WHATSAPP_URL, TELEGRAM_URL
    for k, val in v.items():
        if not val:
            continue
        if k.startswith("link_"):
            BROKER_LINKS[_CODES[k[5:]]] = val
        elif k.startswith("trade_"):
            BROKER_TRADE_LINKS[_CODES[k[6:]]] = val
    POCKET_MASTER_ID = v.get("pocket_master_id") or POCKET_MASTER_ID
    USDT_ADDRESS = v.get("usdt_address") or USDT_ADDRESS
    WHATSAPP_URL = v.get("whatsapp_url") or WHATSAPP_URL
    TELEGRAM_URL = v.get("telegram_url") or TELEGRAM_URL


async def load_settings() -> None:
    async with SessionLocal() as db:
        apply_settings({r.key: r.value for r in (await db.scalars(select(Setting))).all()})


def _valid_url(v: str) -> bool:
    return v.startswith("https://") and " " not in v and len(v) <= 300


def _valid_tron(a: str) -> bool:
    try:
        return len(a) == 34 and a.startswith("T") and bool(b58_to_hex20(a))
    except Exception:
        return False


async def settings_payload(db: AsyncSession) -> dict:
    values = {**{f"link_{k}": BROKER_LINKS[c] for k, c in _CODES.items()},
              **{f"trade_{k}": BROKER_TRADE_LINKS[c] for k, c in _CODES.items()},
              "whatsapp_url": WHATSAPP_URL, "telegram_url": TELEGRAM_URL,
              "pocket_master_id": "" if POCKET_MASTER_ID == _PLACEHOLDER_MASTER else POCKET_MASTER_ID,
              "usdt_address": USDT_ADDRESS if _valid_tron(USDT_ADDRESS) else ""}
    last = await db.scalar(select(func.max(Signal.created_at)))
    checks = [
        ("Broker registration links set", all(BROKER_LINKS[c] != DEFAULT_LINKS[c] for c in _CODES.values())),
        ("Broker trading-page links set", all(BROKER_TRADE_LINKS[c] != DEFAULT_TRADE[c] for c in _CODES.values())),
        ("WhatsApp and Telegram links set", bool(WHATSAPP_URL and TELEGRAM_URL)),
        ("Pocket Option master copy-trade ID set", bool(values["pocket_master_id"])),
        ("USDT TRC-20 address set (on-chain payment checks active)", bool(values["usdt_address"]) and onchain_enabled()),
        ("Telegram feed received a signal in the last 7 days", bool(last and last > utcnow() - timedelta(days=7))),
        ("Broker verification required", REQUIRE_VERIFIED_BROKER),
        ("Auto-confirm of unverified payments is off", not AUTO_CONFIRM),
    ]
    return {"values": values, "checks": [{"label": l, "ok": ok} for l, ok in checks],
            "last_signal_at": last.isoformat() if last else None,
            "pending_accounts": await db.scalar(select(func.count(BrokerAccount.id)).where(BrokerAccount.status == "PENDING")),
            "pending_payments": await db.scalar(select(func.count(Payment.id)).where(Payment.status == "PENDING"))}


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
async def get_settings(db: AsyncSession = Depends(get_db)):
    try:
        return await settings_payload(db)
    except Exception:
        log.exception("settings failed")
        raise HTTPException(500, "Internal error")


@app.post("/api/admin/settings", dependencies=[Depends(require_admin)])
async def save_settings(body: dict[str, str], db: AsyncSession = Depends(get_db)):
    clean: dict[str, str] = {}
    for k, v in body.items():
        if k not in SETTING_KEYS:
            raise HTTPException(422, f"Unknown setting: {k}")
        v = v.strip()
        if not v:
            continue
        if (k.startswith(("link_", "trade_")) or k.endswith("_url")) and not _valid_url(v):
            raise HTTPException(422, f"{k} must be a full https:// link")
        if k == "usdt_address" and not _valid_tron(v):
            raise HTTPException(422, "That is not a valid TRON (TRC-20) address")
        if k == "pocket_master_id" and len(v) > 64:
            raise HTTPException(422, "Master ID is too long")
        clean[k] = v
    async with tx(db):
        for k, v in clean.items():
            row = await db.get(Setting, k)
            if row:
                row.value = v
            else:
                db.add(Setting(key=k, value=v))
    apply_settings(clean)
    return await settings_payload(db)


# ───────────────────────── PWA shell ─────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = ('{"name":"Share Option","short_name":"ShareOption","start_url":"/","display":"standalone",'
            '"background_color":"#0c1119","theme_color":"#0c1119","icons":[{"src":"data:image/svg+xml,'
            '%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 64 64%27%3E%3Crect width=%2764%27 '
            'height=%2764%27 rx=%2714%27 fill=%27%23f2b84b%27/%3E%3C/svg%3E","sizes":"any","type":"image/svg+xml"}]}')
SW = """self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(clients.claim()));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET'||e.request.url.includes('/api/'))return;
e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open('so1').then(k=>k.put(e.request,c));return r})
.catch(()=>caches.match(e.request)))});"""


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(os.path.join(HERE, "share-option-landing.html"))


@app.get("/app", include_in_schema=False)
async def client_app():
    return FileResponse(os.path.join(HERE, "platform.html"))


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return RedirectResponse("/#/admin")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return Response(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    return Response(SW, media_type="application/javascript")


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static"), check_dir=False), name="static")  # broker logos
