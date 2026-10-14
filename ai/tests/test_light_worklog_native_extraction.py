import asyncio
import json
from dataclasses import replace

import pytest
from lightrag.prompt import PROMPTS

from tests.test_lightrag_v4_adapter import make_dependencies, make_settings
from app.light.v4.service.lightrag_adapter import _build_gemini_llm_model_func

CONTEXT = dict(entity_types="Data, Concept", language="Korean", examples="",
               tuple_delimiter="<|#|>", completion_delimiter="<|COMPLETE|>")
SYSTEM = PROMPTS["entity_extraction_system_prompt"].format(**CONTEXT)
BODY = "source_type: WORKLOG\nworklog_id: 127\nLiteral <|COMPLETE|> and ``` in source."
USER = PROMPTS["entity_extraction_user_prompt"].format(**CONTEXT, input_text=BODY)
CONTINUE = PROMPTS["entity_continue_extraction_user_prompt"].format(**CONTEXT)
VALID = ("entity<|#|>127<|#|>Data<|#|>Worklog 127\n"
         "entity<|#|>Toast<|#|>Concept<|#|>Toast review\n"
         "relation<|#|>127<|#|>Toast<|#|>review<|#|>127 reviews Toast\n<|COMPLETE|>")


def callback(responses, *, assert_native_request=False):
    deps, _, _, _ = make_dependencies()
    calls = []

    async def complete(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if assert_native_request:
            assert prompt == USER
            assert kwargs["system_prompt"] == SYSTEM
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return _build_gemini_llm_model_func(settings_obj=make_settings(),
        dependencies=replace(deps, gemini_model_complete=complete)), calls


def test_native_prompt_body_and_output_are_preserved_without_json_schema():
    fn, calls = callback([VALID], assert_native_request=True)
    result = asyncio.run(fn(USER, system_prompt=SYSTEM))
    assert result == VALID
    assert "generation_config" not in calls[0]
    from lightrag.operate import _process_extraction_result
    nodes, edges = asyncio.run(_process_extraction_result(result, "chunk-test", 1))
    assert nodes["127"][0]["entity_type"] == "data"
    assert ("127", "Toast") in edges


@pytest.mark.parametrize("bad", [
    "entity<|#|>127<|DataType|>Data<|#|>desc\n<|COMPLETE|>",
    "entity<|#|>Name<|#|>Name<|#|>Concept<|#|>desc\n<|COMPLETE|>",
    "relation<|#|>127<|#|>Toast<|#|>description\n<|COMPLETE|>",
    "relation<|#|>127<|#|>Toast<|#|><|#|>description\n<|COMPLETE|>",
    "entity<|#|>127<|#|>Data<|#|>desc",
    "<|COMPLETE|>\n<|COMPLETE|>",
    json.dumps({"entities": [], "relationships": []}),
    'entity<|#|>""<|#|>Data<|#|>desc\n<|COMPLETE|>',
    'entity<|#|>A<|#|><|DataType|><|#|>desc\nentity<|#|>B<|#|>Data<|#|>desc\n<|COMPLETE|>',
    'entity<|#|>A<|#|>,,,<|#|>desc\nentity<|#|>B<|#|>Data<|#|>desc\n<|COMPLETE|>',
])
def test_invalid_native_output_is_retried_without_caching_partial_records(bad):
    fn, calls = callback([bad, VALID])
    assert asyncio.run(fn(USER, system_prompt=SYSTEM)) == VALID
    assert len(calls) == 2
    assert calls[1]["prompt"].startswith(USER)
    assert calls[1]["system_prompt"] == SYSTEM


def test_persistent_invalid_format_fails_after_three_attempts():
    fn, calls = callback(["bad"])
    with pytest.raises(ValueError, match="extraction"):
        asyncio.run(fn(USER, system_prompt=SYSTEM))
    assert len(calls) == 3


def test_native_gleaning_keeps_original_messages_and_allows_relation_only_output():
    relation = "relation<|#|>127<|#|>Toast<|#|>review<|#|>127 reviews Toast\n<|COMPLETE|>"
    fn, calls = callback([relation])
    history = [{"role": "user", "content": USER}, {"role": "assistant", "content": VALID}]
    original = json.loads(json.dumps(history))
    assert asyncio.run(fn(CONTINUE, system_prompt=SYSTEM, history_messages=history)) == relation
    assert calls[0]["prompt"] == CONTINUE
    assert calls[0]["history_messages"] == history == original
    assert "generation_config" not in calls[0]


def test_empty_native_extraction_is_valid_and_cacheable():
    from lightrag.utils import use_llm_func_with_cache
    from app.light.v4.store.worklog_extraction_cache import ValidatedWorklogExtractionCache
    from tests.test_light_worklog_extraction_cache import MemoryCache
    fn, calls = callback(["\n<|COMPLETE|>\n"])
    native = MemoryCache()
    native.global_config["enable_llm_cache"] = True
    cache = ValidatedWorklogExtractionCache(native)

    async def run():
        first, _ = await use_llm_func_with_cache(USER, fn, cache, system_prompt=SYSTEM)
        second, _ = await use_llm_func_with_cache(USER, fn, cache, system_prompt=SYSTEM)
        assert first == second == "<|COMPLETE|>"

    asyncio.run(run())
    assert len(calls) == 1


def test_keyword_and_answer_paths_do_not_require_native_extraction_format():
    fn, calls = callback(["ordinary answer"])
    assert asyncio.run(fn("query", system_prompt="Answer")) == "ordinary answer"
    assert asyncio.run(fn("keywords", keyword_extraction=True)) == "ordinary answer"
    assert all("generation_config" not in call for call in calls)
