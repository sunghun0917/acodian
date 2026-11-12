import { expect, test, type Page, type Route } from "@playwright/test";

// 이 테스트는 실제 API 인가를 검증하지 않고 HTTP 응답에 대한 화면 동작만 확인한다.
test.use({
  launchOptions: process.env.E2E_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.E2E_CHROMIUM_EXECUTABLE }
    : {},
});

const requestId = "711de977-6c6a-4501-bf1a-2de0e4c1b18b";

function notification(overrides: Record<string, unknown> = {}) {
  return {
    notificationId: 17,
    notificationType: "WORKLOG_AI_FAILED",
    title: "업무 요약 실패",
    content: "요약을 다시 요청할 수 있습니다.",
    referenceType: "WORKLOG",
    referenceId: 41,
    departmentId: null,
    teamId: 2,
    teamName: "개발팀",
    isRead: false,
    readAt: null,
    createdAt: "2026-10-30T12:00:00Z",
    retryable: true,
    retryRequestId: requestId,
    targetDeleted: false,
    ...overrides,
  };
}

function success(data: unknown) {
  return { success: true, data, timestamp: "2026-10-30T12:00:00Z" };
}

async function mockSessionAndNotifications(
  page: Page,
  items: ReturnType<typeof notification>[],
  retry: (route: Route) => Promise<void>,
) {
  await page.context().addCookies([
    { name: "refreshToken", value: "mock", url: new URL(test.info().project.use.baseURL ?? "http://localhost:3000").origin },
  ]);
  await page.route("**/auth/refresh", async (route) => {
    await route.fulfill({ status: 200, headers: { authorization: "Bearer test-access" }, json: success({}) });
  });
  await page.route("**/users/me", async (route) => {
    await route.fulfill({ json: success({
      userId: 5, userName: "테스트 사용자", email: "test@example.invalid", phone: null,
      departmentId: null, departmentName: "개발부", positionName: "사원", titleName: "사원",
      joinDate: null, employmentStatus: "ACTIVE", profileImageUrl: null, teams: [],
    }) });
  });
  await page.route("**/notifications/stream", async (route) => {
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
  });
  await page.route("**/notifications/me**", async (route) => {
    await route.fulfill({ json: success({
      items, page: 1, pageSize: 6, totalCount: items.length, totalPages: 1,
      isFirst: true, isLast: true, hasNext: false, hasPrevious: false,
    }) });
  });
  await page.route("**/worklogs/*/ai-tasks/*/retry", retry);
  await page.goto("/notification");
  await expect(page.getByRole("heading", { name: "알림 현황" })).toBeVisible();
}

test("실패 작업만 식별해 재시도하며 요청 중 중복 클릭을 막는다", async ({ page }) => {
  let release!: () => void;
  const responseGate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  let requestPath = "";
  let authorization = "";
  const item = notification();
  await mockSessionAndNotifications(page, [item], async (route) => {
    calls += 1;
    requestPath = new URL(route.request().url()).pathname;
    authorization = (await route.request().headerValue("authorization")) ?? "";
    await responseGate;
    item.retryable = false;
    await route.fulfill({ json: success({}) });
  });

  try {
    await page.getByRole("button", { name: "실패한 작업 재시도" }).click();
    await expect(page.getByRole("button", { name: "재시도 요청 중" })).toBeDisabled();
    expect(calls).toBe(1);
    expect(requestPath).toBe(`/api/worklogs/41/ai-tasks/${requestId}/retry`);
    expect(authorization).toBe("Bearer test-access");
  } finally {
    release();
  }
  await expect(page.getByRole("button", { name: "실패한 작업 재시도" })).toHaveCount(0);
});

test("재시도 불가 알림에는 버튼이 없다", async ({ page }) => {
  await mockSessionAndNotifications(page, [notification({ retryable: false })], async () => {
    throw new Error("재시도 불가 작업은 요청되면 안 됩니다");
  });

  await expect(page.getByText("업무 요약 실패")).toBeVisible();
  await expect(page.getByRole("button", { name: "실패한 작업 재시도" })).toHaveCount(0);
});

test("삭제된 업무의 색인 삭제 실패는 재시도할 수 있지만 상세 링크는 없다", async ({ page }) => {
  await mockSessionAndNotifications(page, [notification({ title: "색인 삭제 실패", targetDeleted: true })], async (route) => {
    await route.fulfill({ json: success({}) });
  });

  await expect(page.getByText("색인 삭제 실패")).toBeVisible();
  await expect(page.getByRole("button", { name: "실패한 작업 재시도" })).toBeVisible();
  await expect(page.getByRole("link", { name: "관련 화면 이동" })).toHaveCount(0);
  const retried = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === `/api/worklogs/41/ai-tasks/${requestId}/retry`,
  );
  await page.getByRole("button", { name: "실패한 작업 재시도" }).click();
  expect((await retried).status()).toBe(200);
});

for (const status of [403, 409]) {
  test(`재시도 API가 ${status}를 반환하면 오류를 표시한다`, async ({ page }) => {
    let calls = 0;
    await mockSessionAndNotifications(page, [notification()], async (route) => {
      calls += 1;
      await route.fulfill({ status, json: { success: false, error: { message: "요청할 수 없습니다." } } });
    });

    await page.getByRole("button", { name: "실패한 작업 재시도" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "재시도 요청에 실패했습니다" })).toBeVisible();
    expect(calls).toBe(1);
  });
}
