"""내부 AI 작업 접수에 공통 서비스 토큰을 요구한다."""

from hmac import compare_digest

from fastapi import HTTPException, Request

from app.config.settings import settings


def require_internal_token(request: Request) -> None:
    """미설정 토큰은 장애로 처리하고 인증 실패 시 작업을 접수하지 않는다."""
    expected = settings.ai_internal_token
    if not expected or not expected.strip():
        raise HTTPException(status_code=503, detail="AI_INTERNAL_TOKEN_NOT_CONFIGURED")
    supplied = request.headers.get("x-ai-internal-token", "")
    if not compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="AI_INTERNAL_ACCESS_DENIED")
