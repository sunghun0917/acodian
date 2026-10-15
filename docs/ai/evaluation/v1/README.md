# V1: 기본 평가 자료 및 벤치마크

> **핵심 요약**: V1 데이터셋, 결정적 코퍼스 생성기, 정적 검증기, RAGAS 실행기와 기존 평가 결과를 보관하는 디렉터리입니다. 전체 데이터 계약과 평가 범위는 [상위 평가 안내](../README.md)의 V1 절을 참고하세요.

---

## 📂 주요 파일 구성

- **`evaluation-corpus-v1.sql`**: 합성 업무일지 200건 원문 및 전용 스키마 DDL/DML
- **`corpus-manifest-v1.json`**: 원천 파일 및 산출물의 SHA-256 해시 무결성 기록
- **`benchmark-v1.jsonl`**: 한국어 질문, 기준답변, 본문 및 관계 근거 50문항
- **`generate_corpus.py`**: 표준 라이브러리 기반의 결정적 SQL/manifest 재생성기
- **`validate_benchmark.py`**: 정적 데이터 무결성 검증기
- **`test_validate_benchmark.py`**: 검증기 자체의 결함 감지 회귀 테스트

---

## 💻 실행 및 검증 명령어

저장소 루트에서 Python 3.11+ 환경으로 실행합니다 (외부 DB/API 의존성 없음):

```powershell
# 1. 벤치마크 데이터 정적 무결성 검사
python -B docs/ai/evaluation/v1/validate_benchmark.py

# 2. 검증기 회귀 테스트 실행
python -B -m unittest discover -s docs/ai/evaluation/v1 -p 'test_*.py' -v
```
