from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class BuildingIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: str = Field("", max_length=255)
    floor_label: str = Field("", max_length=40)


class BuildingPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    address: str | None = Field(None, max_length=255)
    floor_label: str | None = Field(None, max_length=40)


def _check_polygon(points: list[list[float]]) -> list[list[float]]:
    if not 3 <= len(points) <= 64:
        raise ValueError("구역은 꼭짓점이 3~64개여야 합니다.")
    out = []
    for p in points:
        if len(p) != 2:
            raise ValueError("좌표는 [x, y] 형식이어야 합니다.")
        x, y = float(p[0]), float(p[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("좌표는 평면도 기준 0~1 사이여야 합니다.")
        out.append([round(x, 5), round(y, 5)])
    return out


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    polygon: list[list[float]]

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v):
        return _check_polygon(v)


class ZonePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    polygon: list[list[float]] | None = None

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v):
        return None if v is None else _check_polygon(v)


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: Literal["tx", "rx"]
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    zone_id: int | None = None


class NodePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    x: float | None = Field(None, ge=0, le=1)
    y: float | None = Field(None, ge=0, le=1)
    zone_id: int | None = None
    clear_zone: bool = False


class CalibrationStartIn(BaseModel):
    duration_seconds: int = Field(120, ge=20, le=600)


class FrameIn(BaseModel):
    t: float
    rssi: float | None = None
    data: list[int] = Field(min_length=2, max_length=1024)


class IngestIn(BaseModel):
    frames: list[FrameIn] = Field(min_length=1, max_length=3000)
    source: Literal["live", "replay"] = "live"


class AlarmIn(BaseModel):
    event: Literal["fire", "test"] = "fire"
    detail: str = Field("", max_length=200)


class IncidentIn(BaseModel):
    note: str = Field("", max_length=300)


class DecisionIn(BaseModel):
    zone_id: int
    action: Literal["searching", "rescued", "cleared", "blocked", "note"]
    people_count: int | None = Field(None, ge=0, le=500)
    note: str = Field("", max_length=500)


class ReplayStartIn(BaseModel):
    speed: float = Field(1.0, ge=0.5, le=4.0)
