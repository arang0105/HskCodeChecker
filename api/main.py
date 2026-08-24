"""본 API. `src/` 를 그대로 부르는 얇은 껍데기다.

    uvicorn api.main:app --reload            (로컬)
    uvicorn api.main:app --host 0.0.0.0 --port $PORT   (Render)

**비즈니스 로직을 여기 새로 쓰지 않는다.** 분류의 순서와 판단은 전부
`src/pipeline.py` 에 있고, 이 파일이 하는 일은 HTTP 요청을 그 함수들에
넘기고 결과를 JSON 으로 돌려주는 것뿐이다. Streamlit 앱(app.py)과
**같은 함수를 같은 순서로** 부른다 — 그래야 두 앱의 답이 갈리지 않는다.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
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
