import asyncio
import time
import httpx

TARGET_URL = "http://localhost:8000/ai/light/worklogs-v3/query"

# v4 벤치마크 1위 최장 지연 쿼리
BASE_QUERY = "릴리즈 노트 누락 승인 전 검토 기록에서 선행업무를 두 차례 따라가면 어디에 도착하나요?"

async def measure_query(step_name: str, query: str = None):
    # 캐시 바이패스를 위해 동적 타임스탬프를 첨부하여 매번 순수 DB+LLM 파이프라인 수행
    actual_query = f"{BASE_QUERY} [run-{int(time.time())}]" if query is None else query
    payload = {
        "query": actual_query,
        "enableRerank": False
    }
    
    print(f"\n==========================================")
    print(f"[{step_name}] Measurement Start")
    print(f"Query: {actual_query}")
    print(f"==========================================")

    async with httpx.AsyncClient(timeout=60.0) as client:
        start_time = time.perf_counter()
        first_token_time = None
        
        async with client.stream("POST", TARGET_URL, json=payload) as response:
            status_code = response.status_code
            first_chunk = True
            body_chunks = []
            
            async for chunk in response.aiter_bytes():
                if first_chunk:
                    first_token_time = time.perf_counter() - start_time
                    first_chunk = False
                body_chunks.append(chunk)
                
        total_time = time.perf_counter() - start_time
        
    print(f"HTTP Status: {status_code}")
    print(f"[TTFT] First Token/Byte Arrival Time: {first_token_time * 1000:.1f} ms ({first_token_time:.2f} s)")
    print(f"[Total Time] Total Response Duration: {total_time * 1000:.1f} ms ({total_time:.2f} s)")
    print(f"==========================================\n")
    
    return {
        "step": step_name,
        "ttft_ms": first_token_time * 1000,
        "total_ms": total_time * 1000,
        "ttft_s": first_token_time,
        "total_s": total_time
    }

if __name__ == "__main__":
    asyncio.run(measure_query("Step 2: TaskGroup 병렬화 (Non-Streaming)"))
