"""DB 테이블. 평상시 CSI 원본은 저장하지 않는다는 원칙에 맞춰,
화재 상황(incident) 중의 추론 결과와 사람의 결정만 남긴다."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Building(Base):
    __tablename__ = "buildings"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(255), default="")
    floor_label: Mapped[str] = mapped_column(String(40), default="")
    # 무료 서버는 디스크가 재시작 때 지워지므로 평면도는 DB에 바로 넣는다.
    plan_image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    plan_mime: Mapped[str | None] = mapped_column(String(40), nullable=True)
    plan_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alarm_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    zones: Mapped[list[Zone]] = relationship(
        back_populates="building", cascade="all, delete-orphan", order_by="Zone.id"
    )
    nodes: Mapped[list[Node]] = relationship(
        back_populates="building", cascade="all, delete-orphan", order_by="Node.id"
    )


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    # [[x, y], ...] 평면도 기준 0~1 비율 좌표
    polygon: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    building: Mapped[Building] = relationship(back_populates="zones")


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(8))  # "tx" | "rx"
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rssi: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    building: Mapped[Building] = relationship(back_populates="nodes")


class Baseline(Base):
    """노드 하나의 빈 공간 기준선. 새로 보정하면 이전 것은 active=False가 된다."""

    __tablename__ = "baselines"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    n_windows: Mapped[int] = mapped_column(Integer)
    n_frames: Mapped[int] = mapped_column(Integer)
    n_subcarriers: Mapped[int] = mapped_column(Integer)
    stats: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CalibrationRun(Base):
    __tablename__ = "calibration_runs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # alarm | manual | measurement | replay
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | closed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class Inference(Base):
    """2초 창 하나에 대한 노드 단위 판단 결과."""

    __tablename__ = "inferences"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    zone_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    t: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    change_score: Mapped[float] = mapped_column(Float)
    p_change: Mapped[float] = mapped_column(Float)
    p_person: Mapped[float | None] = mapped_column(Float, nullable=True)
    z: Mapped[dict] = mapped_column(JSON)
    quality: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(10), default="live")  # live | replay


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # searching | rescued | cleared | blocked | note
    people_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    username: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    zone_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
