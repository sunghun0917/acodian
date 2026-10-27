-- 최신 요청·실행의 결과 권위만 저장하며 Celery 재시도 예약은 관리하지 않는다.
ALTER TABLE tb_worklog
    ADD COLUMN light_index_request_id UUID,
    ADD COLUMN light_index_execution_id UUID,
    ADD COLUMN light_index_status VARCHAR(10) CHECK (light_index_status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
    ADD COLUMN light_index_error VARCHAR(1000),
    ADD COLUMN light_index_completed_at TIMESTAMP;
ALTER TABLE tb_worklog_light_outbox ADD COLUMN request_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE tb_worklog_ai_post_process_outbox
    ADD COLUMN light_request_id UUID,
    ADD COLUMN retry_light_request_id UUID,
    ADD COLUMN light_accepted BOOLEAN NOT NULL DEFAULT false;
-- 완료 checkpoint는 그대로 두고 미완료 생성 작업만 안정적인 요청에 연결한다.
UPDATE tb_worklog_ai_post_process_outbox SET light_request_id = gen_random_uuid()
WHERE NOT ('LIGHT_INDEX' = ANY(completed_stages));
UPDATE tb_worklog SET light_index_request_id = p.light_request_id, light_index_status = 'PENDING'
FROM tb_worklog_ai_post_process_outbox p
WHERE tb_worklog.worklog_id = p.worklog_id AND p.light_request_id IS NOT NULL;
-- 수정·삭제 의도가 있으면 해당 요청이 생성 작업보다 최신이다.
UPDATE tb_worklog SET light_index_request_id = o.request_id, light_index_status = 'PENDING'
FROM tb_worklog_light_outbox o WHERE tb_worklog.worklog_id = o.worklog_id;
UPDATE tb_worklog_ai_post_process_outbox p SET light_request_id = o.request_id,
    light_accepted = true, completed_stages = array_remove(p.completed_stages, 'LIGHT_INDEX')
FROM tb_worklog_light_outbox o WHERE p.worklog_id = o.worklog_id;
UPDATE tb_worklog_ai_post_process_outbox SET retry_light_request_id = gen_random_uuid()
WHERE retry_payload IS NOT NULL;
