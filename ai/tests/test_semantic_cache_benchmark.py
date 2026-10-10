from benchmarks.semantic_cache_real_embedding import summarize, validate_hit


def test_summarize_reports_small_sample_without_p95():
    result = summarize([1.0, 3.0, 2.0])
    assert result == {"count": 3, "median_ms": 2.0, "min_ms": 1.0, "max_ms": 3.0}


def test_validate_hit_requires_marker_and_expected_path():
    body = {"answer": "BENCHMARK_MARKER", "references": [], "mode": "mix", "internalOnly": True}
    validate_hit(body, "BENCHMARK_MARKER")
    try:
        validate_hit({**body, "answer": "wrong"}, "BENCHMARK_MARKER")
    except ValueError:
        pass
    else:
        raise AssertionError("wrong answer must fail")
