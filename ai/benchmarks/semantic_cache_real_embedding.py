"""Response-cache benchmark disabled until cached answers support DB validation."""

import asyncio
import statistics
import sys


def summarize(values: list[float]) -> dict[str, float | int]:
    """Keep descriptive statistics available for historical benchmark reports."""
    if not values:
        raise ValueError("empty sample")
    return {"count": len(values), "median_ms": round(statistics.median(values), 3),
            "min_ms": round(min(values), 3), "max_ms": round(max(values), 3)}


def validate_hit(body: dict, marker: str) -> None:
    """Validate historical evidence without running the disabled benchmark."""
    if body.get("answer") != marker or body.get("mode") != "mix" or body.get("internalOnly") is not True:
        raise ValueError("response did not match the seeded cache answer")


def make_server() -> None:
    """Reject the legacy server without contacting Redis or Gemini."""
    raise RuntimeError("response cache is disabled in LightRAG v4")


async def run() -> None:
    """Reject the legacy benchmark while response caching is disabled."""
    raise RuntimeError("response cache is disabled in LightRAG v4")


if __name__ == "__main__":
    if sys.argv[1:] == ["server"]:
        make_server()
    elif sys.argv[1:] == ["run"]:
        asyncio.run(run())
    else:
        raise SystemExit("use 'run' or 'server'")
