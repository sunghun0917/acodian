import asyncio
from types import SimpleNamespace

from app.config.settings import Settings
from app.light.v3.service.lightrag_adapter import (
    LightRagQueryOptions,
    LightRagWorklogIndexAdapter,
)


def test_scoped_adapter_retrieves_without_generation_and_completes_filtered_prompt() -> None:
    calls = []

    async def aquery_data(query, *, param):
        calls.append(("retrieve", query, param.mode))
        return {"status": "success", "data": {"chunks": []}}

    async def llm_model_func(query, *, system_prompt, stream):
        calls.append(("complete", query, system_prompt, stream))
        return "safe answer"

    async def embedding_func(texts):
        calls.append(("embed", texts))
        return [[0.5] * 768]

    fake_rag = SimpleNamespace(
        aquery_data=aquery_data, llm_model_func=llm_model_func,
        embedding_func=embedding_func,
    )
    adapter = LightRagWorklogIndexAdapter(settings_obj=Settings(_env_file=None))

    async def fake_get_initialized_rag():
        return fake_rag

    adapter._get_initialized_rag = fake_get_initialized_rag
    options = LightRagQueryOptions(
        query="질문", top_k=10, chunk_top_k=5, response_type="Multiple Paragraphs",
    )

    async def run():
        data = await adapter.query_data(options)
        answer = await adapter.complete_scoped("질문", system_prompt="authorized only")
        embedding = await adapter.embed_query("질문")
        return data, answer, embedding

    data, answer, embedding = asyncio.run(run())

    assert data["status"] == "success"
    assert answer == "safe answer"
    assert len(embedding) == 768
    assert calls[0] == ("retrieve", "질문", "mix")
    assert calls[1] == ("complete", "질문", "authorized only", False)
    assert calls[2] == ("embed", ["질문"])
