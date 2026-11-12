"""Hooks loaded only by the isolated normal-flow indexing worker."""

import hashlib
import json
import os


async def barrier(redis, phase, worklog_id):
    prefix = f"e2e:{phase}:{worklog_id}"
    if await redis.get(f"{prefix}:armed") != b"1":
        return
    await redis.rpush(f"{prefix}:arrived", "1")
    if await redis.blpop(f"{prefix}:release", timeout=60) is None:
        raise TimeoutError(f"E2E {phase} barrier expired")


async def fake_llm(*args, keyword_extraction=False, answer_generation=False, **kwargs):
    if keyword_extraction:
        return json.dumps({"high_level_keywords": ["E2E"], "low_level_keywords": ["shared"]})
    if answer_generation:
        return "E2E deterministic answer"
    from lightrag.prompt import PROMPTS
    return PROMPTS["DEFAULT_COMPLETION_DELIMITER"]


def install():
    if os.environ.get("WORKLOG_NORMAL_E2E_ENABLED") != "1" or os.environ.get("CELERY_BROKER_URL") != "redis://redis:6379/0":
        raise RuntimeError("E2E worker requires the isolated broker configuration")

    import numpy as np
    from redis.asyncio import from_url
    from app.light.v4.service import lightrag_adapter
    from app.light.v4.service.worklog_index_service import LightWorklogIndexService
    from app.task import light_worklog_tasks

    redis = from_url("redis://redis:6379/0")
    original_prepare = LightWorklogIndexService.prepare_documents
    original_report = light_worklog_tasks._persist_and_report
    original_start = light_worklog_tasks.WorklogIndexClient.report_start
    original_initialize = lightrag_adapter.LightRagWorklogIndexAdapter._get_initialized_rag

    async def initialize(self):
        try:
            return await original_initialize(self)
        except Exception as exc:
            cause = exc
            chain = []
            hints = set()
            while cause is not None and len(chain) < 8:
                chain.append(type(cause).__name__)
                detail = str(cause).lower()
                hints.update(hint for hint in ("neo4j", "qdrant", "postgres", "localhost", "127.0.0.1", "5432", "5434", "6333", "7687") if hint in detail)
                cause = cause.__cause__
            print(f"E2E initialization exception types: {' -> '.join(chain)}; hints: {','.join(sorted(hints))}", flush=True)
            raise

    async def prepare(self, worklog_ids):
        prepared = await original_prepare(self, worklog_ids)
        for worklog_id in worklog_ids:
            await barrier(redis, "prepared", worklog_id)
        return prepared

    async def report(worklog_id, *args, **kwargs):
        await barrier(redis, "callback", worklog_id)
        return await original_report(worklog_id, *args, **kwargs)

    async def start(client, worklog_id, request_id, execution_id):
        await barrier(redis, "prestart", worklog_id)
        await barrier(redis, "start", request_id)
        return await original_start(client, worklog_id, request_id, execution_id)

    def embedding(*, settings_obj, dependencies):
        @dependencies.embedding_wrapper(embedding_dim=settings_obj.embedding_dim,
                                        max_token_size=settings_obj.lightrag_embedding_max_token_size,
                                        model_name="e2e-deterministic")
        async def embed(texts):
            vectors = []
            for text in texts:
                digest = hashlib.sha256(text.encode()).digest()
                vector = np.resize(np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1, settings_obj.embedding_dim)
                vectors.append(vector / np.linalg.norm(vector))
            return np.asarray(vectors, dtype=np.float32)
        return embed

    def llm(*, settings_obj, dependencies):
        return fake_llm

    LightWorklogIndexService.prepare_documents = prepare
    light_worklog_tasks._persist_and_report = report
    light_worklog_tasks.WorklogIndexClient.report_start = start
    lightrag_adapter._build_gemini_embedding_func = embedding
    lightrag_adapter._build_gemini_llm_model_func = llm
    lightrag_adapter.LightRagWorklogIndexAdapter._get_initialized_rag = initialize


if __name__ == "__main__":
    install()
    from app.task.celery_app import celery_app
    celery_app.worker_main(["worker", "--loglevel=WARNING", "--concurrency=1", "--pool=prefork", "--queues=worklog_index"])
