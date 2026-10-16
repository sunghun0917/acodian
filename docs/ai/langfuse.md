# LightRAG v4 Langfuse 관측

## 범위

- 기존 Gemini와 `lightrag-hku==1.4.10`을 유지한다.
- `/ai/light/worklogs-v4/index`, `/reindex`, `/query`, `/query/stream`을 요청별 trace로 기록한다.
- Gemini 생성·임베딩 호출을 하위 observation으로 묶는다. 다른 AI 기능은 전송하지 않는다.
- 자동 평가, 평가용 LLM, 모델 비교 실험은 포함하지 않는다.

## 설정과 실행

`ai/requirements.txt`를 설치하고 `ai/.env`에 다음을 설정한 뒤 서버를 재시작한다.
실제 키는 `.env.example`, 소스, 채팅에 복사하지 않는다.

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENABLED=true
```

`LANGFUSE_BASE_URL`은 프로젝트 리전에 맞춘다. EU는 위 주소, US는
`https://us.cloud.langfuse.com`, Japan은 `https://jp.cloud.langfuse.com`이다.
두 키 중 하나가 없거나 `LANGFUSE_TRACING_ENABLED=false`이면 초기화하지 않는다.
키는 Pydantic Settings에서 직접 SDK에 전달하므로 실행 디렉터리의 `.env` 로딩에 의존하지 않는다.

기존 내부 인증 헤더를 사용해 index/query API를 호출하고 Cloud에서 다음 이름을 확인한다.

| 요청 | Trace 이름 |
|---|---|
| index | `index-worklogs` |
| reindex | `reindex-worklogs` |
| query | `query-worklogs` |
| query/stream | `stream-worklogs-query` |

모든 관측에는 `lightrag-v4` 태그가 붙는다. query의 입력은 질문, 출력은 최종 답변이다.
index/reindex의 입력은 업무일지 ID 목록, 출력은 항목별 성공/실패 결과다.
생성 호출에는 모델·프롬프트·응답·시간·제공되는 토큰 사용량이 포함된다.
API 인증 헤더나 Settings 객체는 수집하지 않으며, 임베딩 벡터 값은 가린다.

## 동작과 한계

- `langfuse==5.0.1`과 `openinference-instrumentation-google-genai==0.1.17`을 사용한다.
  instrumentor 1.x는 Google SDK 2.x를 대상으로 하므로 현재 `google-genai==1.73.1`과 함께 업그레이드하지 않는다.
- LightRAG 1.4.10의 영속 LLM/임베딩 worker는 최초 요청 context를 재사용한다.
  어댑터에서 큐 진입 시 context를 전달하고 모델 실행 시 복원한다. 라이브러리 전역 패치는 하지 않는다.
- 캐시 적중·이미 처리된 문서 등 실제 모델을 호출하지 않은 요청에는 생성 observation이 없을 수 있다.
- 토큰은 모델 API가 제공한 사용량만 기록한다. Gemini 임베딩 응답처럼 사용량을 제공하지 않는
  경우 토큰/비용은 비어 있을 수 있으며, 0원으로 해석하면 안 된다.
- 비용은 Langfuse 모델 가격표에 따른 추정값이며 무료 할당량·개별 계약을 반영한 실제 청구액이 아니다.
- 알려진 API 키·비밀번호·토큰이 포함된 observation은 exception event/status까지 검사해 전송하지 않는다.
  이는 일반적인 개인정보 익명화 기능이 아니다. 현재는 더미 데이터 사용을 전제로 한다.
- 수집/전송 오류가 원래 요청의 결과나 예외를 바꾸지 않도록 한다. 원문 업무 예외 대신 예외 종류를 root에 기록한다.
  앱 종료 시 전송을 flush하고, SDK 프로세스 자원은 atexit에서 정리한다.

## 검증

```powershell
cd ai
.\.venv\Scripts\python.exe -m pytest tests -q
```

일반 테스트는 실제 Cloud 전송을 끈다. 관측 테스트는 실제 SDK·LightRAG worker와
메모리 exporter를 사용하고 외부 Gemini HTTP 응답만 대체한다.

참고: [Google Gemini 연동](https://langfuse.com/integrations/model-providers/google-gemini),
[좋은 trace 구성](https://langfuse.com/docs/observability/best-practices),
[SDK 고급 설정](https://langfuse.com/docs/observability/sdk/advanced-features).
