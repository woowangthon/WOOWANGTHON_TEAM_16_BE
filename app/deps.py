from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Building, Node, User
from .security import read_session_token, token_digest


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다.")
    username = read_session_token(authorization[7:].strip(), settings.secret_key)
    if not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 만료됐습니다. 다시 로그인하세요.")
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다.")
    return user


def node_from_token(
    x_node_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Node:
    if not x_node_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Node-Token 헤더가 없습니다.")
    node = db.scalar(select(Node).where(Node.token_hash == token_digest(x_node_token)))
    if not node or node.role != "rx":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "등록되지 않은 노드 토큰입니다.")
    return node


def building_from_alarm_token(
    x_alarm_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Building:
    if not x_alarm_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Alarm-Token 헤더가 없습니다.")
    building = db.scalar(select(Building).where(Building.alarm_token_hash == token_digest(x_alarm_token)))
    if not building:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "등록되지 않은 경보 토큰입니다.")
    return building


def get_building_or_404(db: Session, building_id: int) -> Building:
    building = db.get(Building, building_id)
    if not building:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "건물을 찾을 수 없습니다.")
    return building
