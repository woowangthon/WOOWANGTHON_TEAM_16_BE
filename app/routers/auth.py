from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..models import User
from ..schemas import LoginIn
from ..security import make_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "아이디 또는 비밀번호가 맞지 않습니다.")
    token, exp = make_session_token(user.username, settings.secret_key, settings.token_ttl_hours)
    return {
        "token": token,
        "username": user.username,
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
    }


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username}
