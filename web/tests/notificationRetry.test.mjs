import assert from "node:assert/strict";
import test from "node:test";

import {
  getNotificationRetryTarget,
  resolveNotificationDeepLink,
} from "../src/app/(protected)/notification/_utils/resolveNotificationDeepLink.ts";

test("재시도 가능한 실패 알림만 작업 식별자를 반환한다", () => {
  const item = { referenceType: "WORKLOG", referenceId: 12, retryable: true, retryRequestId: "request-1" };
  assert.deepEqual(getNotificationRetryTarget(item), { worklogId: 12, requestId: "request-1" });
  assert.equal(getNotificationRetryTarget({ ...item, retryable: false }), null);
  assert.equal(getNotificationRetryTarget({ ...item, retryRequestId: null }), null);
});

test("삭제된 업무의 알림은 없는 업무 상세로 연결하지 않는다", () => {
  assert.equal(resolveNotificationDeepLink({ referenceType: "WORKLOG", referenceId: 12, targetDeleted: true }), null);
  assert.equal(resolveNotificationDeepLink({ referenceType: "WORKLOG", referenceId: 12 }), "/worklog/detail/12");
});
