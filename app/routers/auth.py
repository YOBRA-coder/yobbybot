import time

from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db
from ..schemas import LoginRequest, SignupRequest
from ..security import create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup")
async def signup(req: SignupRequest):
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone():
        conn.close()
        raise HTTPException(400, "Email already registered")
    uid = f"u_{int(time.time()*1000)}"
    conn.execute(
        "INSERT INTO users (id,name,email,password_hash) VALUES (?,?,?,?)",
        (uid, req.name, req.email, hash_password(req.password)),
    )
    conn.commit()
    conn.close()
    return {"token": create_token(uid), "user": {"id": uid, "name": req.name, "email": req.email}}


@router.post("/login")
async def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
    conn.close()
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    u = dict(user)
    return {"token": create_token(u["id"]), "user": {k: u[k] for k in ("id", "name", "email", "telegram_token", "telegram_chat_id")}}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {k: user[k] for k in ("id", "name", "email", "telegram_token", "telegram_chat_id", "balance", "risk_per_trade")}
