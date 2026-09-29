# V1 평가 자료

V1 데이터셋, 결정적 코퍼스 생성기, 검증기, RAGAS 실행기와 기존 결과를 보관합니다. 전체 데이터 계약과 평가 범위는 [평가 안내](../README.md)의 V1 절을 참고하세요.

저장소 루트에서 실행:

```powershell
python -B docs/ai/evaluation/v1/validate_benchmark.py
python -B -m unittest discover -s docs/ai/evaluation/v1 -p 'test_*.py' -v
```
