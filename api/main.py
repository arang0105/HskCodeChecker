"""본 API. `src/` 를 그대로 부르는 얇은 껍데기다.

    uvicorn api.main:app --reload            (로컬)
    uvicorn api.main:app --host 0.0.0.0 --port $PORT   (Render)

**비즈니스 로직을 여기 새로 쓰지 않는다.** 분류의 순서와 판단은 전부
`src/pipeline.py` 에 있고, 이 파일이 하는 일은 HTTP 요청을 그 함수들에
넘기고 결과를 JSON 으로 돌려주는 것뿐이다. Streamlit 앱(app.py)과
**같은 함수를 같은 순서로** 부른다 — 그래야 두 앱의 답이 갈리지 않는다.
"""

import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src import config, hsk, pipeline, search, storage

# 상한 — app.py 와 같은 값이다. 공개 URL + 인증 없음 + 내 API 키이므로
# 이게 유일한 방어선이다(CLAUDE.md 보안).
세션_분류_상한 = 5
일일_분류_상한 = 50
세션_게이트_상한 = 15      # 되묻기만 반복하는 것도 막는다


# ------------------------------------------------------------------ 기동
# 무거운 데이터를 담아 둘 곳. 모듈 전역에 dict 하나로 둔다.
#
# **왜 dict 인가** — Streamlit 은 @st.cache_resource 가 이 일을 해줬다.
# FastAPI 에는 그런 장치가 없으니 직접 들고 있어야 한다. 클래스를 만들
# 이유는 없다(CLAUDE.md "추상화를 미리 도입하지 말 것").
자원 = {}


@asynccontextmanager
async def lifespan(app):
    """앱이 뜰 때 한 번, 내려갈 때 한 번 도는 자리.

    **여기서 데이터를 미리 올리는 이유** — 0단계 실측에서 임베딩+메타 적재가
    Render 에서 11.4초 걸렸다(로컬 1.7초). 그리고 무료 티어는 15분 무활동이면
    프로세스를 죽인다. 깨어난 뒤 첫 요청 안에서 적재하면 그 11.4초를
    **첫 사용자가 뒤집어쓴다.** 여기로 옮기면 Render 가 기동을 기다리는
    구간에 흡수된다.

    yield 앞이 시작, 뒤가 종료다. @asynccontextmanager 는 "yield 하나로
    앞뒤를 나눠 쓰는 함수"를 만들어 준다 — 자바의 try/finally 자리와 같다.
    """
    자원["인덱스"] = search.load_index()
    자원["세번표"] = hsk.load_hsk()
    # 표가 없으면 만든다. app.py 의 DB_준비() 와 같은 일인데, 여기서는
    # 프로세스당 한 번만 돌면 되므로 조건 없이 부른다.
    try:
        storage.init_db()
    except Exception as e:
        # **DB 가 없어도 앱은 뜬다.** 분류는 DB 없이도 되고, 기록은 부가
        # 기능이다. 여기서 죽이면 Supabase 가 잠든 동안 URL 자체가 죽는다.
        print(f"[기동] DB 준비 실패 — {type(e).__name__}", flush=True)
    yield
    자원.clear()


app = FastAPI(title="HS코드 분류 검증 보조", lifespan=lifespan)

# **CORS** — 화면(React)과 API 가 다른 도메인에 있으면 브라우저가 기본으로
# 요청을 막는다. 허용할 주소를 명시해야 한다.
#
# `*` 로 열지 않는다. 환경변수로 받아서 3단계 배포 때 실제 주소만 넣는다.
# 쉼표로 여러 개 줄 수 있다.
허용_출처 = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=허용_출처,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ------------------------------------------------------------ 상한 헬퍼
# app.py 의 같은 이름 함수들을 그대로 옮겼다. 이유도 그대로다.
def _오늘():
    """일일 카운터의 날짜 키. **한국 시각 기준의 오늘**이다.

    datetime.now() 는 코드가 도는 기계의 날짜라, UTC 컨테이너에서는 상한이
    한국 시각 오전 9시에 리셋된다. 실제로 그 일이 있었다.
    """
    return datetime.now(config.KST).date().isoformat()


def 일일_사용량():
    """오늘 몇 건 분류했는지. **DB 가 안 잡히면 0 (fail-open).**

    세어야 막는 건데 못 세니 막지 못하는 셈이라 마음에 걸리는 선택이다.
    그래도 이쪽인 이유 — Supabase 무료는 7일 무접속이면 일시정지되는데,
    그때 앱이 "오늘 상한을 다 썼습니다"로 굳으면 시연에서 URL 이 죽는다.
    """
    try:
        return storage.daily_count(_오늘())
    except Exception as e:
        print(f"[일일 카운터] 읽기 실패 — {type(e).__name__}", flush=True)
        return 0


def 일일_더하기(delta):
    """오늘 사용량을 delta 만큼 바꾼다. 실패 시 -1 로 되돌리는 데도 쓴다."""
    try:
        return storage.daily_add(_오늘(), delta)
    except Exception as e:
        print(f"[일일 카운터] 쓰기 실패 — {type(e).__name__}", flush=True)
        return 0


def 세션_분류횟수(세션id):
    """그 브라우저가 분류한 건수. **DB 가 안 잡히면 0 (fail-open).**"""
    try:
        return storage.session_count(세션id)
    except Exception as e:
        print(f"[세션 카운터] 읽기 실패 — {type(e).__name__}", flush=True)
        return 0


# 게이트 호출 횟수는 DB 에 안 남으므로 메모리에 센다.
#
# **콜드스타트마다 리셋된다는 것을 알고 쓴다.** Streamlit 의 st.session_state
# 도 서버 메모리라 재시작하면 같이 날아갔다 — 방어 수준은 동등하다.
# 게이트는 flash 1콜이라 싸고, 비싼 쪽(분류)은 DB 로 센다.
게이트횟수 = {}


def 상한_검사(세션id):
    """분류·게이트 공통. 막아야 하면 사유 문자열, 통과면 None.

    **화면에서만 잠그지 않고 처리 시점에 다시 검사한다.** app.py 에서
    버튼의 disabled 는 '그려질 때'의 상태라 한 박자 늦었고, 실제로 6번째가
    통과한 적이 있다(app.py:604). 클라이언트 검증만 하고 서버 검증을
    빠뜨린 것과 같은 실수다.
    """
    if 세션_분류횟수(세션id) >= 세션_분류_상한:
        return "이 브라우저에서 쓸 수 있는 횟수를 다 썼습니다."
    if 일일_사용량() >= 일일_분류_상한:
        return "오늘 사용량을 모두 썼습니다. 내일 다시 시도해 주세요."
    if 게이트횟수.get(세션id, 0) >= 세션_게이트_상한:
        return "이 브라우저에서 요청이 너무 많았습니다. 잠시 후 새로고침해 주세요."
    return None


# ------------------------------------------------------------ 요청·응답 형식
# pydantic 모델이다. 들어온 JSON 을 이 모양으로 검사해서 안 맞으면
# FastAPI 가 422 를 대신 돌려준다 — 자바의 DTO + Bean Validation 자리다.
#
# 같은 모양을 web/src/types.ts 에 손으로 한 벌 더 적는다. 자동 생성하지 않는다.
class GateIn(BaseModel):
    desc: str = Field(max_length=5000)
    # 브라우저가 만든 익명 uuid4. localStorage 에 두고 매 요청에 실어 보낸다.
    # 사람을 식별하지 않는다 — 세션 상한을 세는 데만 쓴다.
    세션id: str = Field(max_length=64)


class GateOut(BaseModel):
    충분: bool
    부족항목: list[str]
    질문: list[str]


class ClassifyIn(BaseModel):
    desc: str = Field(max_length=5000)
    세션id: str = Field(max_length=64)
    # 주소 뒤의 ?u=... 를 프론트가 붙여 보낸다. 링크마다 다른 값을 주면
    # 누가 어느 경로로 들어왔는지 갈린다. 길이를 자르는 이유 — 주소는
    # 아무나 아무 값이나 넣을 수 있다.
    유입: str | None = Field(default=None, max_length=20)
    # 게이트가 "부족"이라 했는데도 사용자가 그냥 진행하겠다고 누른 경우.
    강행: bool = False
    # **기록용으로만 받는다.** 방금 /api/gate 가 돌려준 값을 프론트가 그대로
    # 되돌려 보낸다. 여기서 게이트를 다시 부르지 않는 이유는 app.py 와 같다 —
    # 방금 판정한 것을 또 물으면 답이 같고 돈만 한 번 더 든다.
    #
    # 클라이언트가 보낸 값을 믿는 셈이지만, 이건 **집계용 기록이지 방어선이
    # 아니다.** 실제로 막는 것은 위의 세 상한이고 그건 전부 서버가 센다.
    게이트_충분: bool = True
    게이트_부족항목: list[str] = []
    게이트_질문: list[str] = []


class FeedbackIn(BaseModel):
    run_id: int
    세션id: str = Field(max_length=64)
    # 'up' / 'down' / None. None 은 "평가를 지운다"가 아니라 "메모만 보낸다"이다 —
    # 아래 주석 참조.
    평가: str | None = None
    메모: str | None = Field(default=None, max_length=500)


class 결정례Out(BaseModel):
    참조번호: str
    score: float
    품명: str
    결정세번: str
    물품설명: str


class RankedOut(BaseModel):
    code: str = ""
    reason: str | None = None
    근거결정례: str | None = None


class ClassifyOut(BaseModel):
    """app.py 의 `결과` dict 와 같은 모양이다. 화면이 쓰는 것만 담았다.

    카탈로그 관련 열(초안·빠진정보·추출토큰)은 뺐다 — 1차 범위가 아니다.
    """
    run_id: int | None = None
    저장실패: str | None = None
    오류: str | None = None

    코드: str = ""
    순위: list[str] = []
    ranked: list[RankedOut] = []

    확신도: str | None = None
    확인포인트: list[str] = []
    확정근거: str | None = None
    확정확신도: str | None = None
    확정확인포인트: list[str] = []
    top근거: str | None = None
    top결정례: str | None = None

    결정례: list[결정례Out] = []
    선택지수: int = 0
    자동확정: bool = False
    elapsed: float = 0.0
    남은횟수: int = 0


# ------------------------------------------------------------------ 엔드포인트
@app.get("/api/health")
def 상태():
    """살아 있는지, 데이터가 올라와 있는지. 주간 점검이 이걸 찌른다."""
    벡터 = 자원.get("인덱스", (None, None))[0]
    return {
        "ok": True,
        "결정례": 0 if 벡터 is None else len(벡터),
        "세번표": len(자원["세번표"]) if "세번표" in 자원 else 0,
    }


@app.post("/api/gate", response_model=GateOut)
def 게이트(req: GateIn):
    """[0] 분류에 착수하기 전에 정보가 충분한지만 판단한다.

    되물어야 하면 여기서 끝난다. **차감하지 않는다** — 되물을수록 손해면
    사용자가 게이트를 우회하게 되고, 그러면 안전장치가 벌칙이 된다.
    """
    desc = req.desc.strip()
    if not desc:
        # 400 = 요청이 잘못됨. 상한(429)과 **코드를 갈라 둔다** —
        # 프론트는 429 를 받으면 입력창을 잠가야 하고 400 은 다시 쓰면 된다.
        raise HTTPException(status_code=400, detail="물품설명을 입력해 주세요.")
    if 사유 := 상한_검사(req.세션id):
        raise HTTPException(status_code=429, detail=사유)

    try:
        g = pipeline.gate(desc, model=config.MODEL_DEV)
    except Exception as e:
        # **원인 문자열을 사용자에게 던지지 않는다.** 종류 이름만 남긴다 —
        # 접속 실패 메시지에는 호스트나 키 조각이 섞여 나올 수 있다.
        print(f"[게이트] 실패 — {type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=502, detail="게이트 판정에 실패했습니다.")

    게이트횟수[req.세션id] = 게이트횟수.get(req.세션id, 0) + 1
    return GateOut(충분=g["충분"], 부족항목=g["부족항목"], 질문=g["질문"])


def _분류_흐름(req):
    """[1]~[4] 를 돌리면서 단계마다 결과를 하나씩 내놓는 **제너레이터**다.

    yield 가 있는 함수는 값을 한 번에 돌려주고 끝나지 않는다. 부를 때마다
    다음 값을 하나씩 내놓고 그 자리에서 멈춰 기다린다. 자바로 치면
    Iterator 를 손으로 구현한 것과 같은데, 문법이 훨씬 짧다.

    **이렇게 나눈 이유** — 평범한 POST 와 SSE 가 **같은 코드**를 써야 한다.
    두 벌로 두면 한쪽만 고쳐서 두 엔드포인트의 답이 갈리고, 그걸 알아챌
    방법이 없다. 아래 두 함수는 이 제너레이터를 소비하는 방식만 다르다.

    내놓는 것은 (종류, 값) 짝이다.
      ("단계", {...})  진행 상황
      ("결과", ClassifyOut)  마지막에 딱 한 번
    """
    desc = req.desc.strip()

    # **차감은 여기서 한 번만.** 게이트에서 되물은 건은 차감하지 않는다.
    일일_더하기(1)

    오류 = None
    first = third = fourth = None
    hits, 순위, ranked = [], [], []

    def 단계(번호, 이름, 상태, 초=None):
        d = {"번호": 번호, "이름": 이름, "상태": 상태}
        if 초 is not None:
            d["초"] = round(초, 1)
        return ("단계", d)

    try:
        yield 단계(1, "6자리 후보 3개를 뽑는 중", "시작")
        # use_cache=False — 앱이 만든 항목이 측정용 후보 캐시에 섞이지 않게 한다.
        first = pipeline.generate_candidates(
            desc, model=config.MODEL_DEV, use_cache=False)
        후보 = first["candidates"]
        if not 후보:
            raise RuntimeError("후보를 만들지 못했습니다")
        yield 단계(1, "6자리 후보 3개를 뽑는 중", "완료", first.get("elapsed"))

        yield 단계(2, "비슷한 과거 결정례를 찾는 중", "시작")
        t = time.time()
        hits = search.search(desc, top_k=5, index=자원["인덱스"])
        yield 단계(2, "비슷한 과거 결정례를 찾는 중", "완료", time.time() - t)

        yield 단계(3, "결정례를 근거로 순위를 다시 판단하는 중", "시작")
        third = pipeline.rerank(desc, 후보, hits, model=config.MODEL_MAIN)
        # 재정렬이 깨졌으면 1차 순서를 그대로 쓴다. 한 단계가 실패해도 답은 낸다.
        순위 = third["codes"] or [c["code"] for c in 후보]
        ranked = (third.get("data") or {}).get("ranked", [])
        yield 단계(3, "결정례를 근거로 순위를 다시 판단하는 중", "완료",
                  third.get("elapsed"))

        yield 단계(4, "10자리 세번을 고르는 중", "시작")
        fourth = pipeline.finalize(desc, 순위[0], table=자원["세번표"],
                                   model=config.MODEL_DEV)
        yield 단계(4, "10자리 세번을 고르는 중", "완료", fourth.get("elapsed"))
    except Exception as e:
        # 원인 문자열은 DB 와 로그에만. 화면에는 안내만 간다.
        오류 = f"{type(e).__name__}: {e}"
        print(f"[분류] 실패 — {오류}", flush=True)
        일일_더하기(-1)          # 실패했으면 차감을 되돌린다

    def 합(키):
        """세 단계의 토큰·시간을 더한다. 없는 단계는 0 으로 친다."""
        return sum((x or {}).get(키, 0) for x in (first, third, fourth))

    d3 = (third.get("data") or {}) if third else {}
    d4 = (fourth.get("data") or {}) if fourth else {}

    기록 = {
        "세션id": req.세션id,
        "유입": req.유입,
        "물품설명": desc,
        "입력출처": "텍스트",      # 카탈로그 경로는 1차 범위가 아니다
        "게이트_충분": req.게이트_충분,
        "게이트_부족항목": req.게이트_부족항목,
        "게이트_질문": req.게이트_질문,
        "강행": req.강행,
        "후보1차": [c["code"] for c in (first or {}).get("candidates", [])],
        "검색결과": [{"참조번호": h["참조번호"], "score": round(h["score"], 4)}
                   for h in hits],
        "재정렬": 순위,
        "확신도": d3.get("confidence"),
        "확인포인트": d3.get("check_points", []),
        "최종10자리": (fourth or {}).get("code", ""),
        "확정근거": d4.get("reason"),
        "확정확신도": d4.get("confidence"),
        "선택지수": (fourth or {}).get("선택지수", 0),
        "자동확정": (fourth or {}).get("auto", False),
        "모델_재정렬": config.MODEL_MAIN,
        "elapsed": round(합("elapsed"), 1),
        "in_tokens": 합("in_tokens"),
        "billed_out": 합("billed_out"),
        "오류": 오류,
    }

    # **저장 실패가 분류 결과를 지우면 안 된다.** 여기까지 오는 데 pro 1콜을
    # 썼다. 기록은 부가 기능, 답이 본체다.
    run_id = 저장실패 = None
    try:
        run_id = storage.save_run(기록)
    except Exception as e:
        저장실패 = type(e).__name__
        print(f"[저장] 실패 — {저장실패}", flush=True)

    yield ("결과", ClassifyOut(
        run_id=run_id,
        저장실패=저장실패,
        오류="분류에 실패했습니다. 잠시 후 다시 시도해 주세요." if 오류 else None,
        코드=기록["최종10자리"],
        순위=순위,
        ranked=[RankedOut(**r) for r in ranked if isinstance(r, dict)],
        확신도=기록["확신도"],
        확인포인트=기록["확인포인트"],
        확정근거=기록["확정근거"],
        확정확신도=기록["확정확신도"],
        확정확인포인트=d4.get("check_points", []),
        top근거=ranked[0].get("reason") if ranked else None,
        top결정례=ranked[0].get("근거결정례") if ranked else None,
        결정례=[
            결정례Out(참조번호=h["참조번호"], score=h["score"], 품명=h["품명"],
                    결정세번=h["결정세번"], 물품설명=h["물품설명"][:400])
            for h in hits
        ],
        선택지수=기록["선택지수"],
        자동확정=기록["자동확정"],
        elapsed=기록["elapsed"],
        남은횟수=max(0, 세션_분류_상한 - 세션_분류횟수(req.세션id)),
    ))


def _착수_검사(req):
    """분류를 시작해도 되는지. 두 엔드포인트가 같은 검사를 쓴다."""
    if not req.desc.strip():
        raise HTTPException(status_code=400, detail="물품설명을 입력해 주세요.")
    if 사유 := 상한_검사(req.세션id):
        raise HTTPException(status_code=429, detail=사유)


@app.post("/api/classify", response_model=ClassifyOut)
def 분류(req: ClassifyIn):
    """진행 표시 없이 결과만. **SSE 가 프록시에 막힐 때 돌아갈 자리다.**

    curl 로 시험하기도 이쪽이 쉬워서 남겨 둔다.
    """
    _착수_검사(req)
    결과 = None
    for 종류, 값 in _분류_흐름(req):
        if 종류 == "결과":
            결과 = 값
    return 결과


@app.post("/api/classify/stream")
def 분류_스트림(req: ClassifyIn):
    """같은 일을 하면서 ①②③④ 진행을 흘려보낸다. **화면은 이쪽을 쓴다.**

    분류가 26~164초 걸리는데 화면이 그 편차를 구분해 주지 못하면 사용자가
    멈춘 줄 알고 새로고침한다. 그러면 250 req/day 를 두 번 문다.

    **한계를 알고 쓴다** — 한 단계가 도는 동안에는 아무것도 안 나간다.
    LLM 호출이 파이썬을 붙들고 있어서 그 사이에 yield 를 끼울 수 없다.
    0단계에서 180초 침묵이 안 끊기는 것을 확인했으므로 한 단계가 그보다
    짧으면 문제없고, 넘으면 그때 별도 스레드 + 하트비트로 바꾼다.
    단계별 소요가 이제 이벤트에 찍히니 넘는지 아닌지 바로 보인다.
    """
    _착수_검사(req)

    def 흐름():
        try:
            for 종류, 값 in _분류_흐름(req):
                # SSE 규격 — 'event:' 로 종류, 'data:' 로 내용, 빈 줄이 끝 표시다.
                # ensure_ascii=False 가 없으면 한글이 \uXXXX 로 나간다.
                본문 = 값 if isinstance(값, dict) else 값.model_dump()
                yield (f"event: {종류}\n"
                       f"data: {json.dumps(본문, ensure_ascii=False)}\n\n")
        except Exception as e:
            # 스트림이 시작된 뒤에는 HTTP 상태코드를 바꿀 수 없다. 이미 200 이
            # 나갔기 때문이다. 그래서 오류도 이벤트로 흘려보낸다.
            print(f"[스트림] 실패 — {type(e).__name__}: {e}", flush=True)
            yield ('event: 오류\ndata: '
                   '{"detail": "분류에 실패했습니다."}\n\n')

    return StreamingResponse(
        흐름(),
        media_type="text/event-stream",
        # 중간 프록시가 응답을 모아 뒀다 한꺼번에 보내면 스트리밍이 아니게 된다.
        # 0단계 프로브에서 이 헤더로 버퍼링 없음을 확인했다.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.post("/api/feedback")
def 피드백(req: FeedbackIn):
    """👍/👎 와 한 줄 의견을 그 행에 채운다.

    **평가와 메모를 한꺼번에 덮어쓴다.** storage.save_feedback 이 두 열을
    같이 UPDATE 하기 때문이다. 그래서 프론트는 **둘 다 매번 보내야 한다** —
    👍 만 다시 누를 때도 이미 쓴 메모를 함께 실어 보내지 않으면 메모가 지워진다.
    Streamlit 은 session_state 에 기억해 두는 방식으로 같은 일을 하고 있다.

    **세션id 를 함께 넘겨 그 브라우저가 만든 행만 고치게 한다.** run_id 가
    연번이라 남의 번호를 찍어 넣을 수 있는데, 공개 URL 에 인증이 없다.
    """
    if req.평가 not in (None, "up", "down"):
        raise HTTPException(status_code=400, detail="평가 값이 올바르지 않습니다.")

    메모 = (req.메모 or "").strip() or None

    try:
        고친행 = storage.save_feedback(
            req.run_id, req.평가, 메모, 세션id=req.세션id)
    except Exception as e:
        print(f"[피드백] 저장 실패 — {type(e).__name__}", flush=True)
        raise HTTPException(status_code=502, detail="저장하지 못했습니다. 잠시 뒤 다시 눌러 주세요.")

    if not 고친행:
        # 없는 번호이거나 남의 것이다. 둘을 **구분해서 알려주지 않는다** —
        # 구분해 주면 어느 번호가 존재하는지 훑어볼 수 있다.
        raise HTTPException(status_code=404, detail="해당 결과를 찾을 수 없습니다.")

    return {"ok": True, "평가": req.평가, "메모": 메모}
