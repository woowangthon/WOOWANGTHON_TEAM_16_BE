FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 무료 서버는 CPU가 0.1개다. 선형대수 라이브러리가 스레드를 여러 개 띄우면 서로 기다리느라
# 기준선 계산이 몇 분씩 걸리므로 1개로 묶는다(측정: 16코어 PC에서도 4.2초 → 2.3초).
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY models ./models
COPY sample_data ./sample_data

# 창 버퍼가 메모리에 있으므로 워커는 하나만 띄운다. Render는 PORT를 넘겨 준다.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
