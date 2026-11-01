from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import __version__
from .config import settings
from .db import Base, SessionLocal, engine
from .models import User
from .routers import auth, buildings, demo, incidents, ingest, model
from .security import hash_password, verify_password
from .services.registry import registry
from .services.replay import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("watchmate")


def _ensure_demo_user() -> None:
    """심사용 테스트 계정. 환경변수의 비밀번호가 바뀌면 시작할 때 맞춰 준다."""
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == settings.demo_username))
        if user is None:
            db.add(User(username=settings.demo_username, password_hash=hash_password(settings.demo_password)))
        elif not verify_password(settings.demo_password, user.password_hash):
            user.password_hash = hash_password(settings.demo_password)
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_demo_user()
    registry.load()
    log.info("모델 상태: %s", "사람 판별 모델 사용" if registry.model else "모델 없음 — 1단계(변화 감지)만 동작")
    yield
    runner.stop()


app = FastAPI(
    title="와치메이트 API",
    version=__version__,
    description="화재 경보 시 Wi-Fi CSI로 구역별 사람 기척을 추정해 현장지휘관에게 보여 주는 서비스의 백엔드",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Node-Token", "X-Alarm-Token"],
)

for r in (auth.router, buildings.router, ingest.router, incidents.router, model.router, demo.router):
    app.include_router(r)


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok", "version": __version__, "mode": registry.mode}
