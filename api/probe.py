"""0단계 — Render 무료 티어가 이 앱을 감당하는지 재는 임시 앱.

    uvicorn api.probe:app --reload      (로컬)

**본 API 가 아니다.** 1단계에서 api/main.py 를 만들고 나면 이 파일은 지운다.
여기서 재는 것은 셋이다.

  1. 메모리 — 임베딩 18MB + 메타 13MB 를 올린 뒤의 실사용량
  2. 요청 타임아웃 — 120초를 끄는 요청이 살아남는지 (SSE 도 함께 본다)
  3. 콜드스타트 — 15분 슬립 뒤 첫 응답까지

먼저 재는 이유는 1주차에 Gemini 무료 한도를 먼저 쟀던 것과 같다. 다 만들고
"안 올라간다"를 알면 남은 기간이 통째로 날아간다.
"""

import asyncio
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

# 앱이 뜬 시각. 콜드스타트를 잰 뒤 "정말 새로 뜬 것인지" 확인하는 용도다.
# 슬립에서 깨어난 프로세스는 이 값이 방금 시각으로 바뀌어 있다.
시작시각 = time.time()

# 무거운 데이터를 담아 둘 곳. 처음에는 비어 있다.
# **모듈 맨 위에서 미리 불러오지 않는다** — 적재 전후를 따로 재야 하고,
# 적재가 콜드스타트를 얼마나 늘리는지도 봐야 하기 때문이다.
적재됨 = {}

app = FastAPI(title="HSK 배포 실측 프로브")


def rss_mb():
    """지금 이 프로세스가 실제로 쓰는 물리 메모리(MB). 리눅스 전용.

    /proc/self/status 는 리눅스 커널이 만들어 주는 가짜 파일이다.
    거기 VmRSS 줄에 킬로바이트 단위로 적혀 있다. Render 는 리눅스라 이걸 쓴다.
    윈도우에는 /proc 이 없으므로 로컬에서는 None 이 나온다 — 정상이다.
    숫자는 어차피 Render 에서 잰 것만 의미가 있다.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for 줄 in f:
                if 줄.startswith("VmRSS:"):
                    # 'VmRSS:\t  123456 kB' → split() 로 쪼개면 [이름, 숫자, 단위]
                    return round(int(줄.split()[1]) / 1024, 1)
    except FileNotFoundError:
        return None
    return None


@app.get("/")
def 상태():
    """지금 메모리와 가동 시간. 콜드스타트 잴 때 이걸 부른다."""
    return {
        "rss_mb": rss_mb(),
        "가동초": round(time.time() - 시작시각, 1),
        "적재됨": sorted(적재됨),
    }


@app.get("/load")
def 적재():
    """실제 데이터를 올리고 메모리가 얼마나 늘었는지 잰다.

    한 번 올리면 그대로 둔다. 두 번째 호출은 증가분이 0 으로 나온다 —
    그게 정상이고, 본 API 도 같은 방식으로 한 번만 올려 재사용한다.
    """
    전 = rss_mb()
    t0 = time.time()

    if "index" not in 적재됨:
        # 함수 **안에서** import 한다. 모듈 위에 두면 앱이 뜰 때 같이 올라가
        # 적재 전 메모리를 잴 수 없다. src/storage.py 가 psycopg2 를 함수 안에서
        # import 하는 것과 같은 이유다.
        from src import hsk, search

        적재됨["index"] = search.load_index()
        적재됨["hsk"] = hsk.load_hsk()

    벡터, 메타 = 적재됨["index"]
    후 = rss_mb()
    return {
        "적재전_rss_mb": 전,
        "적재후_rss_mb": 후,
        "증가_mb": None if 전 is None else round(후 - 전, 1),
        "걸린초": round(time.time() - t0, 1),
        "결정례건수": len(메타),
        "벡터모양": list(벡터.shape),
        "hsk행수": len(적재됨["hsk"]),
    }


@app.get("/slow")
async def 느린응답(sec: int = 120):
    """sec 초 동안 아무것도 보내지 않다가 한 번에 응답한다.

    Render 앞단의 프록시가 조용한 연결을 몇 초 만에 끊는지 보는 것이다.
    분류 한 건이 26~110초라 여기서 끊기면 일반 POST 로는 못 만든다.

    async def 로 쓰고 asyncio.sleep 을 쓴다. 그냥 time.sleep 을 쓰면 서버
    전체가 그 시간 동안 멈춰서 다른 요청도 같이 죽는다 — 자바의 동기 블로킹과
    같은 문제이고, 파이썬 비동기에서는 await 붙은 sleep 을 써야 비켜 준다.
    """
    sec = min(sec, 300)          # 실수로 큰 수를 넣어 워커를 오래 잡지 않게
    await asyncio.sleep(sec)
    return {"기다린초": sec, "rss_mb": rss_mb()}


@app.get("/sse")
async def 스트리밍(sec: int = 120):
    """sec 초 동안 5초마다 한 줄씩 흘려보낸다.

    /slow 가 끊기더라도 이쪽이 살아남으면 SSE 로 우회할 수 있다는 뜻이다.
    계획의 /api/classify 가 이 방식이다.

    yield 가 있는 함수는 **제너레이터**다. 값을 한 번에 돌려주고 끝나는 게
    아니라, 부를 때마다 다음 값을 하나씩 내놓고 그 자리에서 멈춰 기다린다.
    그래서 응답을 조금씩 흘려보낼 수 있다.

    보내는 문자열 모양 'data: ...\n\n' 은 SSE 규격이 정한 것이다.
    빈 줄 하나가 "한 덩어리 끝"을 뜻한다.
    """
    sec = min(sec, 300)

    async def 흐름():
        t0 = time.time()
        보낸수 = 0
        while time.time() - t0 < sec:
            await asyncio.sleep(5)
            보낸수 += 1
            yield f"data: tick {보낸수} t={int(time.time()-t0)}s rss={rss_mb()}\n\n"
        yield f"data: done {int(time.time()-t0)}s\n\n"

    return StreamingResponse(
        흐름(),
        media_type="text/event-stream",
        # 중간 프록시가 응답을 모아 뒀다 한꺼번에 보내면 스트리밍이 아니게 된다.
        # 이 헤더가 그걸 하지 말라는 관례적 신호다.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
