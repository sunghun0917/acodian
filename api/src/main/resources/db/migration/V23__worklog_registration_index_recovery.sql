-- 전달 선점과 결과 대기 기한을 분리하고 최종 실패의 수동 복구 문맥을 보존한다.
ALTER TABLE tb_worklog_ai_post_process_outbox
    ADD COLUMN terminal_failed BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN light_result_deadline_at TIMESTAMP;

-- 배포 전에 접수된 미완료 요청에도 복구 기한을 부여한다.
UPDATE tb_worklog_ai_post_process_outbox
SET light_result_deadline_at = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
WHERE light_accepted AND NOT ('LIGHT_INDEX' = ANY(completed_stages));
