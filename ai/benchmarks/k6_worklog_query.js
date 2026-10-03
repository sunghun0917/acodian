import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

// 커스텀 메트릭 정의
const ttftTrend = new Trend('ttft_first_token');
const successRate = new Rate('query_success_rate');

export const options = {
  scenarios: {
    rag_benchmark: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '5s', target: 2 },
        { duration: '20s', target: 3 },
        { duration: '5s', target: 1 },
      ],
      gracefulRampDown: '5s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.5'], // 초기 성공률 임계치
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://host.docker.internal:8000/ai/light/worklogs-v3/query';

export default function () {
  const payload = JSON.stringify({
    query: '릴리즈 노트 누락 승인 전 검토 기록에서 선행업무를 두 차례 따라가면 어디에 도착하나요?',
    enableRerank: false,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/event-stream',
    },
    timeout: '60s',
  };

  const res = http.post(BASE_URL, payload, params);

  // http_req_waiting은 서버가 첫 응답 바이트/헤더를 반환하기까지 걸린 시간 (TTFB/TTFT)
  ttftTrend.add(res.timings.waiting);

  const isSuccess = check(res, {
    'status is 200': (r) => r.status === 200,
  });

  successRate.add(isSuccess);

  // 과도한 Rate Limit 유발 방지를 위한 짧은 휴식 (0.5~1초)
  sleep(0.5);
}
