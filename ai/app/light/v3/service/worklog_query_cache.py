"""권한 범위별 LightRAG 답변의 Exact / Semantic Redis 캐시."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.light.v3.model.worklog_query import WorklogLightQueryResponse


class WorklogQueryCache:
    """Redis 8 Search에 저장하는 업무일지 질의 답변 캐시."""

    _prefix = "worklog:query:v3"
    _index = "idx:worklog:query:v3"
    _ttl = 86400
    _dimensions = 768

    def __init__(self, *, settings_obj, client=None) -> None:
        self._redis = client or Redis.from_url(settings_obj.redis_url)
        self._index_ready = False

    @staticmethod
    def scope_key(allowed_team_ids: list[int] | None) -> str:
        """전체 권한과 정렬된 팀 집합을 서로 다른 해시로 표현한다."""
        teams = None if allowed_team_ids is None else sorted(set(allowed_team_ids))
        raw = json.dumps(teams, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def signature(
        query: str,
        *,
        top_k: int,
        chunk_top_k: int,
        response_type: str,
        enable_rerank: bool,
        rerank_model: str | None,
        llm_model: str,
        embedding_model: str,
    ) -> str:
        """답변에 영향을 주는 질의와 검색·모델 옵션을 해시한다."""
        raw = json.dumps(
            [query, top_k, chunk_top_k, response_type, enable_rerank,
             rerank_model, llm_model, embedding_model],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _scope(scope: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", scope) is None:
            raise ValueError("scope must be a SHA-256 hex digest")
        return scope

    @classmethod
    def _vector(cls, embedding: list[float]) -> bytes:
        if len(embedding) != cls._dimensions or not all(math.isfinite(x) for x in embedding):
            raise ValueError("embedding must contain 768 finite values")
        if not any(embedding):
            raise ValueError("embedding must not be the zero vector")
        try:
            return struct.pack("<768f", *embedding)
        except OverflowError as exc:
            raise ValueError("embedding values must fit FLOAT32") from exc

    async def get_version(self) -> int:
        """TTL 없는 Redis 버전 키의 현재 값을 읽는다. 최초 버전은 0이다."""
        value = await self._redis.get(f"{self._prefix}:version")
        return int(value) if value is not None else 0

    async def bump_version(self) -> int:
        """인덱스 변경 후 캐시 네임스페이스를 원자적으로 전환한다."""
        return await self._redis.incr(f"{self._prefix}:version")

    async def _ensure_index(self) -> None:
        if self._index_ready:
            return
        try:
            await self._redis.execute_command(
                "FT.CREATE", self._index, "ON", "HASH", "PREFIX", "1",
                f"{self._prefix}:semantic:", "SCHEMA", "scope", "TAG",
                "version", "TAG",
                "vector", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32",
                "DIM", self._dimensions, "DISTANCE_METRIC", "COSINE",
            )
        except ResponseError as exc:
            if "Index already exists" not in str(exc):
                raise
        self._index_ready = True

    async def get_exact(self, scope: str, signature: str, version: int) -> WorklogLightQueryResponse | None:
        """정확히 같은 권한·질의·옵션의 답변을 반환한다."""
        scope = self._scope(scope)
        value = await self._redis.get(f"{self._prefix}:exact:{version}:{scope}:{signature}")
        return WorklogLightQueryResponse.model_validate_json(value) if value is not None else None

    async def get_semantic(
        self, scope: str, embedding: list[float], version: int
    ) -> WorklogLightQueryResponse | None:
        """같은 범위에서 cosine 유사도 0.90 이상인 최근접 답변만 반환한다."""
        scope = self._scope(scope)
        vector = self._vector(embedding)
        await self._ensure_index()
        result = await self._redis.execute_command(
            "FT.SEARCH", self._index,
            f"(@scope:{{{scope}}} @version:{{{version}}})=>[KNN 1 @vector $vec AS distance]",
            "PARAMS", "2", "vec", vector,
            "SORTBY", "distance", "RETURN", "2", "response", "distance",
            "DIALECT", "2",
        )
        if result[0] == 0:
            return None
        fields = dict(zip(result[2][::2], result[2][1::2]))
        fields = {key.decode() if isinstance(key, bytes) else key: value for key, value in fields.items()}
        if float(fields["distance"]) > 0.10:
            return None
        return WorklogLightQueryResponse.model_validate_json(fields["response"])

    async def put(
        self, scope: str, signature: str, embedding: list[float], version: int,
        response: WorklogLightQueryResponse,
    ) -> None:
        """Semantic HASH와 Exact 값을 각각 24시간 보관한다."""
        scope = self._scope(scope)
        vector = self._vector(embedding)
        await self._ensure_index()
        payload = response.model_dump_json(by_alias=True)
        key = f"{self._prefix}:semantic:{version}:{scope}:{signature}"
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(key, mapping={"scope": scope, "version": str(version), "vector": vector, "response": payload})
        pipe.expire(key, self._ttl)
        await pipe.execute()
        await self._redis.set(f"{self._prefix}:exact:{version}:{scope}:{signature}", payload, ex=self._ttl)

    async def invalidate_all(self) -> int:
        """Exact·Semantic 캐시 키만 SCAN으로 찾아 배치 DEL하고 삭제 건수를 반환한다."""
        deleted = 0
        for kind in ("exact", "semantic"):
            batch = []
            async for key in self._redis.scan_iter(
                match=f"{self._prefix}:{kind}:*", count=100
            ):
                batch.append(key)
                if len(batch) == 100:
                    deleted += await self._redis.delete(*batch)
                    batch.clear()
            if batch:
                deleted += await self._redis.delete(*batch)
        return deleted

    async def close(self) -> None:
        """Redis 연결을 닫는다."""
        await self._redis.aclose()
