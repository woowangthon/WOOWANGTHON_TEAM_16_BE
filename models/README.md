# models

학습 명령이 만든 사람 판별 모델이 여기에 저장된다.

```
python -m app.ml.train --data ../data/sessions --out models/presence_model.joblib
```

- `presence_model.joblib` — 서버가 시작할 때 읽는 모델(scikit-learn, joblib)
- `presence_model.card.json` — 검증 결과를 담은 모델 카드. AI 검증 화면이 이 내용을 그대로 보여 준다

모델 파일이 없으면 서버는 1단계(기준선 대비 변화)만으로 동작하고 화면 문구도 "신호 변화"로 바뀐다.

joblib 파일은 pickle 기반이라 읽는 순간 코드가 실행될 수 있다. 우리 학습 명령으로 만든 파일만 넣는다.
학습과 서버의 scikit-learn 버전이 같아야 하므로 `requirements.txt`의 버전을 바꾸지 않는다.
