import os
import sys
import tempfile
from pathlib import Path

# 테스트는 임시 SQLite와 빈 모델 경로로 돈다. app을 import하기 전에 설정해야 한다.
_TMP = Path(tempfile.mkdtemp(prefix="watchmate-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["DEMO_USERNAME"] = "tester"
os.environ["DEMO_PASSWORD"] = "pw-for-tests"
os.environ["MODEL_PATH"] = str(_TMP / "models" / "presence_model.joblib")
os.environ["SAMPLE_DATA_DIR"] = str(_TMP / "sample_data")
os.environ["ALLOW_SYNTHETIC_DEMO"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP_DIR = _TMP
