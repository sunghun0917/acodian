"""일반 테스트가 개발자의 실제 Langfuse 프로젝트에 데이터를 보내지 않게 한다."""

import pytest

from app.config.settings import settings


@pytest.fixture(autouse=True)
def disable_cloud_tracing(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_tracing_enabled", False)
