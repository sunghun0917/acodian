"""LightRAG native extraction cache 결과를 검증하는 instance-local proxy."""

from typing import Any


class ValidatedWorklogExtractionCache:
    """잘못된 native extraction 결과를 제거하고 나머지 cache 동작은 위임한다."""

    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_storage"), name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "ValidatedWorklogExtractionCache":
        """native asdict가 live storage 연결·lock을 복제하지 않도록 identity를 보존한다."""
        memo[id(self)] = self
        return self

    async def get_by_id(self, key: str) -> Any:
        value = await self._storage.get_by_id(key)
        if value is not None and not _valid_entry(key, value):
            await self._invalidate([key])
            return None
        return value

    async def get_by_ids(self, keys: list[str]) -> list[Any]:
        values = await self._storage.get_by_ids(keys)
        invalid = [key for key, value in zip(keys, values) if value is not None and not _valid_entry(key, value)]
        if invalid:
            await self._invalidate(invalid)
            invalid_set = set(invalid)
            values = [None if key in invalid_set else value for key, value in zip(keys, values)]
        return values

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if any(not _valid_entry(key, value) for key, value in data.items()):
            raise ValueError("Invalid LightRAG extraction cache entry")
        await self._storage.upsert(data)

    async def delete(self, keys: list[str]) -> None:
        await self._storage.delete(keys)

    async def _invalidate(self, keys: list[str]) -> None:
        await self.delete(keys)
        await self._storage.index_done_callback()


def _valid_entry(key: str, entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("cache_type") != "extract":
        return True
    if _is_confirmed_custom_kg(key, entry):
        return True
    return is_valid_native_extraction(entry.get("return"))


def _is_confirmed_custom_kg(key: str, entry: dict[str, Any]) -> bool:
    return (key.startswith("confirmed:extract:")
            and entry.get("original_prompt") == "DB-confirmed Custom KG"
            and entry.get("cache_type") == "extract")


def is_valid_native_extraction(value: Any) -> bool:
    """모든 레코드의 필드 수·필수 값·완료 마커를 검사한다. 의미 품질은 별도다."""
    if not isinstance(value, str):
        return False
    from lightrag.prompt import PROMPTS
    from lightrag.utils import sanitize_and_normalize_extracted_text

    delimiter = PROMPTS["DEFAULT_TUPLE_DELIMITER"]
    completion = PROMPTS["DEFAULT_COMPLETION_DELIMITER"]
    if completion not in value:
        return False
    records_text, terminal, remainder = value.rpartition(completion)
    if not terminal or remainder.strip():
        return False
    if completion in records_text:
        return False
    if not records_text.strip():
        return value.strip() == completion
    records = [line for line in records_text.splitlines() if line.strip()]
    for line in records:
        fields = [field.strip() for field in line.split(delimiter)]
        expected = 4 if fields[0] == "entity" else 5 if fields[0] == "relation" else 0
        if (len(fields) != expected or any(not field for field in fields)
                or any("<|" in field or "|>" in field for field in fields)):
            return False
        name_positions = {1} if expected == 4 else {1, 2}
        for index, field in enumerate(fields[1:], start=1):
            normalized = sanitize_and_normalize_extracted_text(
                field, remove_inner_quotes=index in name_positions or (expected == 4 and index == 2),
            )
            if not normalized.strip():
                # Native의 짧은 숫자 이름 제외 정책은 형식 오류와 구분한다 (예: 업무일지 21).
                numeric_name = field.strip('"')
                if index in name_positions and numeric_name and len(numeric_name) < 3 and all(
                    char.isdigit() or char == "." for char in numeric_name
                ):
                    continue
                return False
            if expected == 4 and index == 2:
                if (any(char in normalized for char in "'()<>|/\\")
                        or not any(token.strip() for token in normalized.split(","))):
                    return False
    return True
