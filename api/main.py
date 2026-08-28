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
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src import config, hsk, pipeline, search, storage

# **로그 출력을 UTF-8 로 고정한다. 이걸 빼면 앱이 죽는다.**
#
# 2026-08-24 에 실제로 겪은 일 — 윈도우에서 파이썬의 기본 출력 인코딩은
# cp949 다. 아래 print 문들의 '—'(em dash) 를 cp949 가 표현하지 못해
# UnicodeEncodeError 가 났는데, 하필 그게 **except 블록 안**이라서
# 오류를 처리하다 오류가 났다. 그러면 save_run 도 못 하고 결과 이벤트도
# 못 내보내서 화면이 영원히 '분류 중'으로 남는다.
#
# 세 곳이 같은 이유로 연달아 터졌다(llm.py 의 재시도 안내, 여기 두 곳).
# sys.stdout 은 프로세스 전체가 공유하는 하나이므로, 진입점인 여기서 한 번
# 바꿔 두면 src/ 쪽 print 도 같이 고쳐진다.
#
# Render(리눅스)는 기본이 UTF-8 이라 배포에서는 안 났을 문제다. 그래도
# 로컬에서 못 돌면 고칠 수가 없다. src/peek.py 가 같은 처리를 하고 있다.
for 스트림 in (sys.stdout, sys.stderr):
    # 파이프로 넘길 때 reconfigure 가 없는 객체일 수 있어 확인하고 부른다.
    if hasattr(스트림, "reconfigure"):
        스트림.reconfigure(encoding="utf-8")

# 상한 — app.py 와 같은 값이다. 공개 URL + 인증 없음 + 내 API 키이므로
# 이게 유일한 방어선이다(CLAUDE.md 보안).
세션_분류_상한 = 5
일일_분류_상한 = 50
세션_게이트_상한 = 15      # 되묻기만 반복하는 것도 막는다

# [0-a] 카탈로그 추출은 분류 상한과 **별개로** 센다.
# 추출은 flash 1콜이라 싸고, 초안을 다시 뽑는 게 벌칙이 되면 사용자가
# 마음에 안 드는 초안을 그냥 쓰게 된다. 게이트를 따로 세는 것과 같은 이유다.
세션_추출_상한 = 10
카탈로그_최대_파일수 = 3

# 끊김 보고의 하루 상한. 보고는 분류 시도에 딸린 것이라 **분류보다 많을 수
# 없다.** 그래서 같은 수로 둔다.
일일_끊김_상한 = 일일_분류_상한

# 게이트의 하루 상한. 한 사람이 게이트 15회에 분류 5회이므로 3:1 이다.
# 일일 분류 50 에 같은 비율을 적용했다.
일일_게이트_상한 = 150

# 카탈로그 추출의 하루 상한. 세션 10회 기준 하루 5명분이다.
# **분류 상한(상한_검사)에 얹지 않고 따로 센다** — 위의 세션_추출_상한 주석과
# 같은 이유로, 초안을 다시 뽑는 것이 분류 횟수를 깎는 벌칙이 되면 안 된다.
# 나누는 것은 그대로 두고, 세션id 위조에 견디는 바닥만 깐다.
일일_추출_상한 = 50


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


def _카운터키(항목=""):
    """counters 표에서 쓸 행 키. 항목을 주면 그 항목만 따로 센다.

    **`항목=""` 는 기본값이 있는 매개변수다.** 안 넘기면 "" 가 들어오므로
    기존 호출부(`일일_사용량()`)는 한 글자도 바뀌지 않는다. 자바에서
    메서드를 오버로딩해 해결하던 자리를 파이썬은 이렇게 대신한다.

    counters 의 날짜 열이 TEXT 라 '끊김:2026-08-27' 같은 문자열을 그대로
    키로 쓸 수 있다 — 카운터 종류를 늘리려고 표를 새로 만들지 않아도 된다.
    """
    return f"{항목}:{_오늘()}" if 항목 else _오늘()


def 일일_사용량(항목=""):
    """오늘 몇 건 썼는지. **DB 가 안 잡히면 0 (fail-open).**

    세어야 막는 건데 못 세니 막지 못하는 셈이라 마음에 걸리는 선택이다.
    그래도 이쪽인 이유 — Supabase 무료는 7일 무접속이면 일시정지되는데,
    그때 앱이 "오늘 상한을 다 썼습니다"로 굳으면 시연에서 URL 이 죽는다.
    """
    try:
        return storage.daily_count(_카운터키(항목))
    except Exception as e:
        print(f"[일일 카운터{항목}] 읽기 실패 — {type(e).__name__}", flush=True)
        return 0


def 일일_더하기(delta, 항목=""):
    """오늘 사용량을 delta 만큼 바꾼다. 실패 시 -1 로 되돌리는 데도 쓴다."""
    try:
        return storage.daily_add(_카운터키(항목), delta)
    except Exception as e:
        print(f"[일일 카운터{항목}] 쓰기 실패 — {type(e).__name__}", flush=True)
        return 0


def 세션_분류횟수(세션id):
    """그 브라우저가 **오늘** 분류한 건수. **DB 가 안 잡히면 0 (fail-open).**

    날짜 키는 일일 카운터와 같은 _오늘() 을 쓴다. 두 상한이 서로 다른 자정에
    풀리면 "남은 횟수" 두 줄이 어긋나 보인다.
    """
    try:
        return storage.session_count(세션id, _오늘())
    except Exception as e:
        print(f"[세션 카운터] 읽기 실패 — {type(e).__name__}", flush=True)
        return 0


# 게이트 호출 횟수는 DB 에 안 남으므로 메모리에 센다.
#
# **콜드스타트마다 리셋된다는 것을 알고 쓴다.** Streamlit 의 st.session_state
# 도 서버 메모리라 재시작하면 같이 날아갔다 — 방어 수준은 동등하다.
# 게이트는 flash 1콜이라 싸고, 비싼 쪽(분류)은 DB 로 센다.
게이트횟수 = {}
추출횟수 = {}
끊김보고 = {}

# 마지막으로 위 셋을 비운 날. **dict 로 두는 이유** — 함수 안에서 모듈
# 전역 변수에 새 값을 넣으려면 global 선언이 필요한데, dict 는 내용만
# 바꾸면 되므로 그게 없어도 된다. 위의 `자원 = {}` 과 같은 이유다.
_비운날 = {"값": None}


def _날짜_바뀌면_비우기():
    """하루가 지났으면 메모리 카운터를 통째로 비운다.

    **두 가지를 같이 고친다.**

    (1) 키가 쌓이기만 했다. 세션id 는 브라우저마다 새로 생기는데 지우는
        코드가 없어서, 프로세스가 사는 동안 계속 늘었다.

    (2) 날짜가 바뀌어도 안 풀렸다. DB 로 세는 상한들은 _오늘() 이 키라서
        자정에 풀리는데(커밋 9374373 "세션 상한을 하루 단위로"), 이 dict
        들은 안 풀린다. **어제 게이트를 15회 쓴 브라우저가 오늘도 막힌다는
        뜻이다.** Render 무료가 15분 무활동이면 프로세스를 죽여서 실질적
        으로는 리셋돼 왔는데, 그건 우연이지 설계가 아니다.

    dict 를 새로 만들지 않고 .clear() 를 쓴다. 다른 함수들이 이 dict 를
    이름으로 붙잡고 있어서, 새 객체로 바꿔 넣으면 그쪽은 옛 것을 계속 본다.
    """
    오늘 = _오늘()
    if _비운날["값"] != 오늘:
        _비운날["값"] = 오늘
        for 카운터 in (게이트횟수, 추출횟수, 끊김보고):
            카운터.clear()


def 추출_남은(세션id):
    """이 브라우저가 카탈로그를 몇 번 더 읽을 수 있는지.

    **두 상한 중 작은 쪽이다.** 세션 몫이 남아도 하루 몫이 떨어지면 못 쓴다.
    화면이 "10회 남음"을 띄운 뒤 눌렀을 때 429 가 나면 그건 고장으로 읽힌다.

    quota 와 catalog 응답이 같은 값을 내도록 계산을 여기 한 곳에 둔다.
    """
    _날짜_바뀌면_비우기()
    return min(
        max(0, 세션_추출_상한 - 추출횟수.get(세션id, 0)),
        max(0, 일일_추출_상한 - 일일_사용량("추출")),
    )


def 파일_형식(f: UploadFile):
    """업로드 파일의 MIME 형식을 정한다. 모르면 None.

    **app.py:205 의 같은 이름 함수를 그대로 옮겼다.** 브라우저가 알려주는
    content_type 을 먼저 믿고, 비었거나 우리가 안 받는 형식이면 파일 이름의
    확장자로 판정한다. 브라우저·OS 에 따라 형식이 빈 문자열로 오기 때문이다.
    """
    if f.content_type in pipeline.CATALOG_MIME:
        return f.content_type
    이름 = f.filename or ""
    확장자 = 이름.rsplit(".", 1)[-1].lower() if "." in 이름 else ""
    for mime, 별칭 in pipeline.CATALOG_MIME.items():
        if 확장자 == 별칭 or (확장자 == "jpeg" and 별칭 == "jpg"):
            return mime
    return None


def 상한_검사(세션id):
    """분류·게이트 공통. 막아야 하면 사유 문자열, 통과면 None.

    **화면에서만 잠그지 않고 처리 시점에 다시 검사한다.** app.py 에서
    버튼의 disabled 는 '그려질 때'의 상태라 한 박자 늦었고, 실제로 6번째가
    통과한 적이 있다(app.py:604). 클라이언트 검증만 하고 서버 검증을
    빠뜨린 것과 같은 실수다.
    """
    _날짜_바뀌면_비우기()
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
    # [0-a] 카탈로그에서 온 입력인가. 게이트 값들과 **같은 성격의 기록이다** —
    # 프론트가 판정해서 보내고 서버는 믿는다. 막는 데 쓰지 않으므로
    # 틀린 값이 와도 집계가 조금 흐려질 뿐이다. 아는 값이 아니면 '텍스트'로 친다.
    입력출처: str = Field(default="텍스트", max_length=20)
    카탈로그_빠진정보: list[str] = []


class DroppedIn(BaseModel):
    """화면이 결과를 못 받고 끝났다고 알려 오는 내용."""
    세션id: str = Field(max_length=64)
    desc: str = Field(default="", max_length=5000)
    # 마지막으로 '완료' 이벤트를 받은 단계 번호. 0 이면 하나도 못 받았다는 뜻이다.
    단계: int = 0
    경과: float = 0.0
    사유: str = Field(default="", max_length=200)


class 제품Out(BaseModel):
    이름: str = ""
    물품설명: str = ""
    빠진정보: list[str] = []


class CatalogOut(BaseModel):
    제품들: list[제품Out] = []
    남은추출: int = 0


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


@app.get("/api/quota")
def 잔여(세션id: str = Query("", max_length=64)):
    """이 브라우저와 오늘 전체가 몇 번 더 쓸 수 있는지.

    **막는 데는 쓰지 않는다.** 막는 것은 처리 시점에 상한_검사() 가 다시
    한다(main.py:159). 여기는 화면에 미리 띄워 주기만 하는 자리다 —
    다섯 번째를 누르고 나서야 다 썼다는 걸 아는 것보다는 낫다.

    세션id 가 비면 세션 몫은 상한 그대로 돌려준다. 화면이 처음 뜰 때
    localStorage 가 아직 비어 있는 경우다.
    """
    세션쓴 = 세션_분류횟수(세션id) if 세션id else 0
    오늘쓴 = 일일_사용량()
    return {
        "세션남은": max(0, 세션_분류_상한 - 세션쓴),
        "세션상한": 세션_분류_상한,
        "일일남은": max(0, 일일_분류_상한 - 오늘쓴),
        "일일상한": 일일_분류_상한,
        # 카탈로그 추출은 분류와 별개로 센다. 이쪽은 서버 메모리라
        # **콜드스타트마다 리셋된다** — 게이트 횟수와 같은 수준의 방어다.
        "추출남은": 추출_남은(세션id),
        "추출상한": 세션_추출_상한,
    }


@app.post("/api/catalog", response_model=CatalogOut)
def 카탈로그(세션id: str = Form("", max_length=64),
           files: list[UploadFile] = File(default=[])):
    """[0-a] 카탈로그(PDF·사진)에서 물품설명 초안을 뽑는다.

    **이 단계는 파이프라인 앞에 붙는다.** 여기서 나온 초안을 사람이 고친
    텍스트가 [0]~[4] 로 간다. 그래야 봉인을 열어 얻은 숫자가 그대로 유효하다
    (pipeline.py:33 의 같은 주석 참조).

    **async def 가 아닌 이유** — catalog_extract 는 LLM 응답을 기다리는
    동안 스레드를 붙잡는다. async 함수 안에서 그러면 이벤트 루프가 통째로
    멈춰 다른 요청까지 선다. sync 로 두면 FastAPI 가 스레드풀에서 돌린다.
    파일도 f.file.read() 로 동기로 읽는다.
    """
    if not files:
        raise HTTPException(status_code=400, detail="파일을 올려 주세요.")
    # **버튼의 disabled 를 믿지 않는다.** 화면이 그려질 때의 상태라 한 박자
    # 늦다. 분류 버튼에서 실제로 6번째가 통과한 적이 있다(app.py:604).
    _날짜_바뀌면_비우기()
    if 추출횟수.get(세션id, 0) >= 세션_추출_상한:
        raise HTTPException(
            status_code=429,
            detail="이 브라우저에서 카탈로그를 읽을 수 있는 횟수를 다 썼습니다.")
    # **위의 카운터는 서버 메모리라 세션id 를 바꾸면 그냥 지나간다.**
    # 게다가 이 함수는 상한_검사() 를 부르지 않아서 일일 상한에도 걸리지
    # 않았다 — 2026-08-27 시험에서 새 세션 500회가 전부 통과했다.
    # 파일이 붙는 만큼 요청당 토큰이 제일 큰 경로인데 바닥이 없었던 셈이다.
    #
    # 그래서 세션과 무관한 DB 카운터를 하나 더 둔다. 파일을 읽기 **전에**
    # 검사한다 — 어차피 거절할 요청이면 40MB 를 받아 놓고 버릴 이유가 없다.
    if 일일_사용량("추출") >= 일일_추출_상한:
        raise HTTPException(
            status_code=429,
            detail="오늘 카탈로그를 읽을 수 있는 횟수를 모두 썼습니다. "
                   "내일 다시 시도하거나, 아래에 직접 입력해 주세요.")
    if len(files) > 카탈로그_최대_파일수:
        raise HTTPException(
            status_code=400,
            detail=f"파일은 {카탈로그_최대_파일수}개까지 올릴 수 있습니다.")

    한도 = pipeline.CATALOG_MAX_MB * 1024 * 1024

    # **읽기 전에 크기부터 본다.**
    # UploadFile.size 는 multipart 를 해석하면서 세어 둔 값이라 파일을
    # 건드리지 않고도 안다(실측: 40MB 파일에서 41943044).
    #
    # 크기를 모르는 파일은 받지 않는다. 모른 채로 read() 하면 얼마가 올라올지
    # 모르는 채로 올리는 것이고, 그게 바로 막으려는 상황이다. 브라우저가
    # 보내는 multipart 에는 항상 들어 있다.
    if any(f.size is None for f in files):
        raise HTTPException(status_code=400,
                            detail="파일 크기를 확인할 수 없습니다. 다시 올려 주세요.")
    미리합계 = sum(f.size for f in files)
    if 미리합계 > 한도:
        raise HTTPException(
            status_code=400,
            detail=f"파일이 너무 큽니다. 합계 {pipeline.CATALOG_MAX_MB}MB 까지 "
                   f"올릴 수 있습니다 (지금 {미리합계 / 1048576:.1f}MB).")

    # 여기까지 왔으면 합계가 한도 이하임이 **보장된다.** 그래서 아래 read()
    # 는 통째로 읽어도 최대 10MB 다.
    #
    # 고치기 전에는 이 검사가 read() **뒤**에 있었다. UploadFile 의 속은
    # SpooledTemporaryFile 이라 1MB 넘는 파일은 디스크에 있는데, 인자 없는
    # read() 가 그걸 통째로 메모리에 올린다. 80MB 를 올려놓고 "10MB 까지"
    # 라며 버리고 있었다(실측: 힙 80.2MB). Render 무료는 512MB 이고 이
    # 경로는 인증 없는 공개 URL 이다.
    #
    # **조각내어 읽는 방법도 해 봤는데 되돌렸다.** 조각을 모았다가
    # b"".join 하면 조각들과 합친 것이 순간 같이 살아 있어 메모리가 2배가
    # 된다 — 9MB 업로드에서 9.1MB 가 18.1MB 로 늘었다. 큰 파일은 위에서
    # 이미 막으니, 통과한 것은 한 번에 읽는 쪽이 싸다.
    파일들 = []
    for f in files:
        mime = 파일_형식(f)
        if mime is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' 은 읽을 수 없는 형식입니다. PDF·PNG·JPG 만 됩니다.")
        파일들.append((f.file.read(), mime))

    # 차감을 먼저 한다. 성공하지 못하면 아래 finally 가 되돌린다.
    추출횟수[세션id] = 추출횟수.get(세션id, 0) + 1
    일일_더하기(1, "추출")

    성공 = False
    try:
        try:
            r = pipeline.catalog_extract(파일들, model=config.MODEL_DEV)
        except Exception as e:
            # **원인 문자열을 사용자에게 던지지 않는다.** 종류 이름만 남긴다 —
            # 접속 실패 메시지에는 호스트나 키 조각이 섞여 나올 수 있다.
            print(f"[카탈로그] 실패 — {type(e).__name__}: {e}", flush=True)
            raise HTTPException(status_code=502,
                                detail="카탈로그를 읽는 중 문제가 생겼습니다. "
                                       "잠시 후 다시 시도해 주세요.")

        if r["읽기실패"]:
            # 읽지 못한 것은 사용자 잘못이 아니다. 되돌려 준다.
            raise HTTPException(status_code=422,
                                detail="카탈로그에서 제품을 찾지 못했습니다. "
                                       "제품 사양이 보이는 페이지로 다시 올리거나, "
                                       "아래에 직접 입력해 주세요.")

        # **응답을 다 만든 뒤에 성공으로 친다.** 위 두 줄을 바꿔 놓으면
        # CatalogOut 조립이 터졌을 때 finally 가 성공으로 보고 안 되돌린다.
        응답 = CatalogOut(
            제품들=[제품Out(**p) for p in r["제품들"]],
            남은추출=추출_남은(세션id),
        )
        성공 = True
        return 응답
    finally:
        # **되돌리는 코드를 여기 한 곳에만 둔다.**
        #
        # 고치기 전에는 같은 두 줄이 except 안과 읽기실패 분기에 **두 벌**로
        # 있었다. 그 방식의 문제는 벌수가 아니라, **거기 안 적힌 실패는 안
        # 되돌려진다**는 것이다 — 위 CatalogOut 조립이나 추출_남은() 이 터지면
        # 두 분기 어디에도 안 걸려서 차감만 남았다. 상한이 실제보다 빨리
        # 차오르고, 사용자는 쓰지도 않은 횟수를 잃는다.
        #
        # finally 는 어떻게 빠져나가든 돈다. except 를 하나 더 만들어도
        # 여기를 고칠 필요가 없다는 게 두 벌과의 진짜 차이다.
        if not 성공:
            추출횟수[세션id] -= 1
            일일_더하기(-1, "추출")


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
    # **상한_검사() 를 통과해도 이 경로는 바닥이 없었다.**
    # 그 안의 세 상한 중 둘(세션 분류·일일 분류)은 분류가 셀 때만 올라가고,
    # 나머지 하나(게이트횟수)는 서버 메모리라 세션id 를 바꾸면 지나간다.
    # 게이트는 통과해도 아무 카운터를 올리지 않았으므로, 새 uuid 를 계속
    # 보내면 flash 를 무제한 부를 수 있었다 — 2026-08-27 시험에서 새 세션
    # 500회가 전부 통과했고 일일 카운터는 0 그대로였다.
    if 일일_사용량("게이트") >= 일일_게이트_상한:
        raise HTTPException(
            status_code=429,
            detail="오늘 요청이 너무 많았습니다. 내일 다시 시도해 주세요.")

    # **세기를 호출 전에 한다.** 성공한 뒤에 세면 실패한 호출은 공짜가 되는데,
    # 실패해도 LLM 은 이미 불렀고 돈도 이미 썼다. 분류가 일일_더하기(1) 를
    # 먼저 하는 것과 같은 이유다(_분류_흐름).
    일일_더하기(1, "게이트")

    try:
        g = pipeline.gate(desc, model=config.MODEL_DEV)
    except Exception as e:
        # **원인 문자열을 사용자에게 던지지 않는다.** 종류 이름만 남긴다 —
        # 접속 실패 메시지에는 호스트나 키 조각이 섞여 나올 수 있다.
        일일_더하기(-1, "게이트")     # 분류와 같다 — 실패는 되돌려 준다
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
    완료 = False          # 결과를 만들어 냈는가. 아래 finally 가 이걸 보고 갈린다
    마지막 = 0            # 끝낸 단계 번호. 중단됐을 때 어디까지 갔는지 남긴다
    first = third = fourth = None
    hits, 순위, ranked = [], [], []

    def 단계(번호, 이름, 상태, 초=None):
        d = {"번호": 번호, "이름": 이름, "상태": 상태}
        if 초 is not None:
            d["초"] = round(초, 1)
        return ("단계", d)

    def 기록만들기(오류):
        """runs 에 넣을 한 행. **정상 종료와 중단이 같은 조립기를 쓴다.**

        바깥 변수(first·third·fourth·hits·순위)를 인자로 받지 않는다. 중첩
        함수는 **부르는 시점**의 바깥 값을 읽기 때문이다 — 위 `단계()` 와 같다.
        자바의 지역 클래스가 사실상 final 인 지역변수만 볼 수 있던 것과 달리,
        파이썬은 읽기만 할 거면 제한이 없다.

        **조립을 두 벌로 두지 않는 이유** — 중단된 건도 어디까지 갔는지(후보1차·
        검색결과·토큰)를 정상 건과 똑같이 알고 있다. 두 벌이면 한쪽만 고쳐서
        두 기록이 갈리고, 그걸 알아챌 방법이 없다.
        """
        def 합(키):
            """세 단계의 토큰·시간을 더한다. 없는 단계는 0 으로 친다."""
            return sum((x or {}).get(키, 0) for x in (first, third, fourth))

        d3 = (third.get("data") or {}) if third else {}
        d4 = (fourth.get("data") or {}) if fourth else {}

        return {
            "세션id": req.세션id,
            "유입": req.유입,
            "물품설명": desc,
            "입력출처": (req.입력출처
                      if req.입력출처 in ("텍스트", "카탈로그", "카탈로그(수정)")
                      else "텍스트"),
            "카탈로그_빠진정보": req.카탈로그_빠진정보[:20],
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

    # **착수를 표에 먼저 남긴다.** 이 함수에서 제일 중요한 자리다.
    #
    # 끝날 때 한 번만 쓰면 **끝까지 못 간 건이 표에 아예 안 남는다.**
    # 사용자가 처리 중에 탭을 닫으면 Starlette 은 응답 제너레이터를 close()
    # 하지 않고 그냥 버린다(starlette 1.3.1 에서 실측). 그러면 GeneratorExit
    # 도 finally 도 오지 않아서 **정리 코드를 걸 자리 자체가 없다.**
    # 시작할 때 남기는 것 말고는 방법이 없다.
    #
    # 남아 있는 `오류='진행중'` 행이 곧 중도 이탈 건수다. 로그를 눈으로 훑던
    # 판정("착수만 있고 완료가 없으면 처리 중에 죽은 것")이 SELECT 한 줄이 된다.
    #
    # /api/dropped 로는 못 메운다. 그건 화면이 살아 있어야 보내는 것이라
    # 탭을 닫으면 보고하는 코드 자체가 안 돈다(web/src/App.tsx 의 catch).
    run_id = 저장실패 = None
    try:
        run_id = storage.save_run(기록만들기("진행중"))
    except Exception as e:
        저장실패 = type(e).__name__
        print(f"[저장] 착수 실패 — {저장실패}", flush=True)

    # **세션id 는 앞 8자만 찍는다.** 로그로 사람을 되짚을 이유가 없고,
    # 같은 브라우저인지 가리는 데는 8자면 충분하다.
    print(f"[분류] 착수 세션={req.세션id[:8]} 글자수={len(desc)} "
          f"(run_id={run_id})", flush=True)

    def 남기기(오류):
        """착수 때 넣어 둔 행을 결과로 채우고 (기록, run_id, 저장실패) 를 준다.

        **저장 실패가 분류 결과를 지우면 안 된다.** 여기까지 오는 데 pro 1콜을
        썼다. 기록은 부가 기능이고 답이 본체다. 그래서 착수 저장이 실패해
        고칠 행이 없으면 여기서 새로 넣어 본다 — 고치기 전과 같은 동작이다.

        `시각` 을 안 넘기므로 update_run 이 그 열을 건드리지 않는다. 표에 남는
        시각은 **착수 시각**이고, 걸린 시간은 elapsed 에 따로 있다.
        """
        기록 = 기록만들기(오류)
        try:
            if run_id is not None:
                storage.update_run(run_id, 기록)
                return 기록, run_id, 저장실패
            return 기록, storage.save_run(기록), None
        except Exception as e:
            print(f"[저장] 실패 — {type(e).__name__}", flush=True)
            return 기록, run_id, type(e).__name__

    # **try 가 두 겹이다.** 안쪽은 [1]~[4] 의 실패를 잡고, 바깥쪽은 이 함수가
    # 어떤 이유로 끝나든 마지막에 한 번 돌 자리를 만든다. 한 겹으로 합칠 수
    # 없는 이유는 finally 가 except 직후에 돌아 버려서, 그 아래 저장 코드가
    # 아직 안 돈 시점에 "중단"으로 판정하기 때문이다.
    try:
        try:
            yield 단계(1, "6자리 후보 3개를 뽑는 중", "시작")
            # use_cache=False — 앱이 만든 항목이 측정용 후보 캐시에 섞이지 않게 한다.
            first = pipeline.generate_candidates(
                desc, model=config.MODEL_DEV, use_cache=False)
            후보 = first["candidates"]
            if not 후보:
                raise RuntimeError("후보를 만들지 못했습니다")
            마지막 = 1
            yield 단계(1, "6자리 후보 3개를 뽑는 중", "완료", first.get("elapsed"))

            yield 단계(2, "비슷한 과거 결정례를 찾는 중", "시작")
            t = time.time()
            hits = search.search(desc, top_k=5, index=자원["인덱스"])
            마지막 = 2
            yield 단계(2, "비슷한 과거 결정례를 찾는 중", "완료", time.time() - t)

            yield 단계(3, "결정례를 근거로 순위를 다시 판단하는 중", "시작")
            third = pipeline.rerank(desc, 후보, hits, model=config.MODEL_MAIN)
            # 재정렬이 깨졌으면 1차 순서를 그대로 쓴다. 한 단계가 실패해도 답은 낸다.
            순위 = third["codes"] or [c["code"] for c in 후보]
            ranked = (third.get("data") or {}).get("ranked", [])
            마지막 = 3
            yield 단계(3, "결정례를 근거로 순위를 다시 판단하는 중", "완료",
                      third.get("elapsed"))

            yield 단계(4, "10자리 세번을 고르는 중", "시작")
            fourth = pipeline.finalize(desc, 순위[0], table=자원["세번표"],
                                       model=config.MODEL_DEV)
            마지막 = 4
            yield 단계(4, "10자리 세번을 고르는 중", "완료", fourth.get("elapsed"))
        except Exception as e:
            # 원인 문자열은 DB 와 로그에만. 화면에는 안내만 간다.
            오류 = f"{type(e).__name__}: {e}"
            print(f"[분류] 실패 — {오류}", flush=True)
            일일_더하기(-1)          # 실패했으면 차감을 되돌린다

        기록, run_id, 저장실패 = 남기기(오류)

        print(f"[분류] 완료 세션={req.세션id[:8]} {기록['elapsed']}초 "
              f"→ {기록['최종10자리'] or '실패'} (run_id={run_id})", flush=True)

        # **저장한 뒤, yield 하기 전에 세운다.** 뒤로 미루면 아래 yield 에서
        # 끊겼을 때 finally 가 같은 건을 한 번 더 저장한다.
        완료 = True

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
            확정확인포인트=((fourth or {}).get("data") or {}).get("check_points", []),
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
    finally:
        # **여기는 보조 장치다. 스트림 끊김은 여기로 안 온다.**
        #
        # 처음에는 이 자리가 중도 이탈을 잡는 곳이라고 봤는데, 재현해 보니
        # 아니었다. 탭을 닫아도 GeneratorExit 이 오지 않는다 — Starlette 은
        # 제너레이터를 close() 하지 않고 그냥 버려서, 60초를 기다려도 이
        # finally 가 안 돌았다(starlette 1.3.1). 그건 위에서 착수 행을 먼저
        # 넣는 것으로 잡는다.
        #
        # 그래도 남겨 두는 이유 — 제너레이터가 **실제로 닫히는** 경우가 있다.
        # 비스트림 /api/classify, 워커 종료, 소비하는 쪽의 예기치 못한 예외다.
        # 그때는 '진행중' 을 단계 번호까지 붙여 고쳐 주니 정보가 더 많다.
        #
        # **차감은 되돌리지 않는다.** 3단계까지 갔으면 pro 호출은 이미 나갔고
        # 250 req/day 는 진짜로 줄었다. 여기서 환불하면 "시작 → 3단계 → 끊기
        # → 환불" 을 반복해 일일 상한을 무한히 우회할 수 있다.
        #
        # **여기서 yield 하면 안 된다.** GeneratorExit 처리 중에 yield 하면
        # RuntimeError: generator ignored GeneratorExit 가 난다.
        if not 완료:
            print(f"[분류] 미완료 세션={req.세션id[:8]} 단계={마지막}", flush=True)
            try:
                남기기(f"중단(단계{마지막})")
            except Exception as e:
                # 중단을 남기다 또 터지면 원래 끊김을 덮는다. 로그만 남기고 삼킨다.
                print(f"[분류] 미완료 기록 실패 — {type(e).__name__}", flush=True)


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

@app.post("/api/dropped")
def 끊김(req: DroppedIn):
    """화면이 결과를 못 받고 스트림이 끝났다. **기록만 한다.**

    **왜 브라우저가 알려 줄 수 있는가** — "연결이 끊겼습니다" 를 띄운 화면은
    살아 있다. 죽은 것은 그 한 요청이지 탭이 아니다. 그러니 그 사실을 다시
    한 번 서버에 부탁할 수 있다.

    **차감하지 않는다.** 분류를 시작할 때 이미 1 을 뺐다. 여기서 또 빼면
    한 번 끊긴 사람이 두 번 손해를 본다.

    이 행이 없으면 "가끔 끊긴다" 가 몇 %인지 영영 알 수 없고, 하트비트를
    넣어도 나아졌는지 증명할 수 없다. 세는 것이 목적이다.
    """
    # 남용 방지 — **두 겹으로 막는다.**
    #
    # (가) 세션 단위(메모리). 한 사람이 같은 보고를 반복하는 것을 막는다.
    _날짜_바뀌면_비우기()
    if 끊김보고.get(req.세션id, 0) >= 세션_분류_상한:
        raise HTTPException(status_code=429, detail="보고가 너무 많습니다.")

    # (나) 하루 단위(DB). **세션id 는 브라우저가 만드는 값이라 믿을 수 없다.**
    # 매 요청 새 uuid 를 보내면 (가)는 그냥 지나간다 — 2026-08-27 시험에서
    # 새 세션 300회가 전부 통과했고, 속도는 74 req/s 였다. 이 엔드포인트는
    # LLM 을 안 부르니 느려질 이유가 없어서 그렇게 빠르다.
    #
    # 그 속도면 Supabase 무료 500MB 가 몇 분 만에 찬다. runs 표가 차면
    # 봉인 2회를 다 쓴 지금 유일하게 남은 측정 채널(👍/👎)이 같이 죽는다.
    # 끊김을 세자고 만든 엔드포인트가 측정을 통째로 날리는 셈이다.
    #
    # 그래서 세션과 무관한 DB 카운터를 최종 방어선으로 둔다. 분류가
    # 일일_분류_상한 으로 막히는 것과 같은 구조다 — 그쪽은 위조에 견딘다.
    if 일일_사용량("끊김") >= 일일_끊김_상한:
        raise HTTPException(status_code=429, detail="보고가 너무 많습니다.")

    # **세기를 먼저 한다.** 저장이 실패해도 요청은 이미 들어온 것이라,
    # 실패를 되돌려 주면 실패시키는 것만으로 상한을 피할 수 있다.
    끊김보고[req.세션id] = 끊김보고.get(req.세션id, 0) + 1
    일일_더하기(1, "끊김")

    기록 = {
        "세션id": req.세션id,
        "물품설명": req.desc,
        "입력출처": "텍스트",
        "elapsed": round(req.경과, 1),
        # 오류 열에 남기므로 peek 의 오류 집계에 그대로 잡힌다.
        "오류": f"연결 끊김 (마지막 완료 단계 {req.단계}) {req.사유}"[:200],
    }
    try:
        run_id = storage.save_run(기록)
    except Exception as e:
        # 끊김을 기록하려다 또 실패했다. 화면에 알릴 것은 없다 —
        # 사용자는 이미 오류 안내를 보고 있다.
        print(f"[끊김] 저장 실패 — {type(e).__name__}", flush=True)
        return {"ok": False}

    print(f"[끊김] 세션={req.세션id[:8]} 단계={req.단계} "
          f"{req.경과:.0f}초 (run_id={run_id})", flush=True)
    return {"ok": True}


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
