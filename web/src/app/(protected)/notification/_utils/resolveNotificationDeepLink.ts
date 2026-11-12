import type { NotificationItem } from "../_types/notification.types"

export function resolveNotificationDeepLink(notification: NotificationItem) {
  if (notification.targetDeleted) return null
  if (notification.referenceType === "WORKLOG" && notification.referenceId) {
    return `/worklog/detail/${notification.referenceId}`
  }

  return "/notification"
}

export function getNotificationRetryTarget(notification: NotificationItem) {
  if (!notification.retryable || notification.referenceType !== "WORKLOG" ||
      !notification.referenceId || !notification.retryRequestId) return null
  return { worklogId: notification.referenceId, requestId: notification.retryRequestId }
}
