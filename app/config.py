"""환경변수에서 설정을 읽는다. 비밀 값은 코드에 두지 않는다."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("watchmate")

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """backend/.env가 있으면 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다(배포 환경 우선)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BACKEND_DIR / ".env")


def _normalize_db_url(url: str) -> str:
    # Neon/Render가 주는 postgres:// 주소를 SQLAlchemy + psycopg3 형식으로 맞춘다.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    demo_username: str
    demo_password: str
    cors_origins: tuple[str, ...]
    model_path: Path
    sample_data_dir: Path
    token_ttl_hours: int
    # 합성 신호 시나리오(개발용)는 기본으로 막는다. 로컬에서 화면을 만들 때만 켠다.
    allow_synthetic_demo: bool
    # 신호처리 파라미터. 학습 CLI도 같은 값을 쓴다.
    window_seconds: float = 2.0
    context_seconds: float = 16.0
    stale_seconds: float = 6.0
    # 현장 실측·녹화 재생에서 모델 없이 쓰는 구역 판정 기준(신호 변화 큼 · 가능). 첫 회차 빈 방 결과를 보고
    # MEASURE_DETECT_P·MEASURE_POSSIBLE_P로 바꾼다. 화재 경보·수동 개시는 0.70·0.35 그대로다.
    measure_detect_p: float = 0.50
    measure_possible_p: float = 0.20


def _measure_thresholds() -> tuple[float, float]:
    try:
        detect = float(os.getenv("MEASURE_DETECT_P", "") or 0.50)
        possible = float(os.getenv("MEASURE_POSSIBLE_P", "") or 0.20)
    except ValueError:
        detect = possible = -1.0
    if not 0 < possible < detect < 1:
        log.warning("MEASURE_DETECT_P·MEASURE_POSSIBLE_P는 0 < 가능 < 큼 < 1이어야 합니다. 기본값 0.50·0.20을 씁니다.")
        return 0.50, 0.20
    return detect, possible


def load_settings() -> Settings:
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        data_dir = BACKEND_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        db_url = f"sqlite:///{data_dir / 'app.db'}"

    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        # 로컬 개발용. 재시작하면 로그인 토큰이 무효가 된다.
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY가 없어 임시 키를 만들었습니다. 배포 환경에서는 반드시 설정하세요.")

    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    model_path = Path(os.getenv("MODEL_PATH", "") or BACKEND_DIR / "models" / "presence_model.joblib")
    if not model_path.is_absolute():
        model_path = BACKEND_DIR / model_path
    sample_dir = Path(os.getenv("SAMPLE_DATA_DIR", "") or BACKEND_DIR / "sample_data")
    if not sample_dir.is_absolute():
        sample_dir = BACKEND_DIR / sample_dir
    measure_detect_p, measure_possible_p = _measure_thresholds()

    return Settings(
        database_url=_normalize_db_url(db_url),
        secret_key=secret,
        demo_username=os.getenv("DEMO_USERNAME", "demo"),
        demo_password=os.getenv("DEMO_PASSWORD", "demo1234"),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        model_path=model_path,
        sample_data_dir=sample_dir,
        token_ttl_hours=int(os.getenv("TOKEN_TTL_HOURS", "12")),
        allow_synthetic_demo=os.getenv("ALLOW_SYNTHETIC_DEMO", "0").strip().lower() in {"1", "true", "yes"},
        measure_detect_p=measure_detect_p,
        measure_possible_p=measure_possible_p,
    )


settings = load_settings()
