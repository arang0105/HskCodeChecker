"""HS코드 분류 검증 보조 시스템 — Streamlit UI.

**Streamlit 을 처음 볼 때 알아야 할 것 하나**
이 파일은 사용자가 버튼을 누를 때마다 **위에서 아래로 통째로 다시 실행된다.**
JSP 처럼 요청 하나에 응답 하나가 아니라, 매번 main() 이 처음부터 다시 도는 것에
가깝다. 그래서 일반 변수는 다음 실행에서 사라지고, 유지할 상태는 전부
st.session_state (dict 처럼 쓰는 저장소) 에 넣는다.
"""

import os
import uuid
from datetime import date

import streamlit as st

# --- src 를 import 하기 **전에** API 키를 환경변수에 올린다 ---
# 배포(Streamlit Cloud)에는 .env 가 없고 st.secrets 를 쓴다. 그런데 src/config.py 는
# 키가 없으면 import 시점에 RuntimeError 를 던진다.
# config.py 에 streamlit 을 넣으면 CLI 스크립트까지 전부 streamlit 을 읽게 되므로,
# 여기서 환경변수를 채워 두고 config 는 그대로 둔다.
try:
    if "GEMINI_API_KEY" in st.secrets:
        os.environ.setdefault("GEMINI_API_KEY", st.secrets["GEMINI_API_KEY"])
except Exception:
    pass  # 로컬에는 secrets.toml 이 없다. 그때는 .env 를 쓴다

from src import config, hsk, pipeline, search, storage  # noqa: E402  (위 설명 참조)

# --- 호출 상한 ---
# 공개 URL + 인증 없음 + 내 API 키다. 상한이 유일한 방어선이다.
세션_분류_상한 = 5
일일_분류_상한 = 50
세션_게이트_상한 = 15  # 되묻기만 반복하는 것도 막는다

# [0-a] 카탈로그 추출은 분류 상한과 **별개로** 센다.
# 추출은 flash 1콜이라 싸고, 초안을 다시 뽑는 게 벌칙이 되면 사용자가
# 마음에 안 드는 초안을 그냥 쓰게 된다. 게이트를 따로 세는 것과 같은 이유다.
세션_추출_상한 = 10
카탈로그_최대_파일수 = 3

예시_충분 = (
    "폴리프로필렌(PP) 재질의 일회용 도시락 용기. 뚜껑 일체형, 용량 700ml, "
    "전자레인지 사용 가능. 표면 인쇄 없음. 사출 성형품."
)
예시_부족 = "P/N 4471-BK / 1EA / MADE IN VIETNAM"


# ---------------------------------------------------------------- 무거운 로딩
# @st.cache_resource 는 함수 위에 붙이는 표시(decorator)다. Java 애노테이션과
# 위치·역할이 비슷하다. 한 번 계산한 결과를 들고 있다가 다음 실행에서 그대로 준다.
#
# cache_data 가 아니라 cache_resource 인 이유 — 전자는 값을 복사해서 돌려주고
# 후자는 **같은 객체를 그대로** 돌려준다. 5,921×768 numpy 배열을 매번 복사하면
# 안 하느니만 못하다.
@st.cache_resource(show_spinner="결정례 5,872건을 불러오는 중...")
def 인덱스_로드():
    return search.load_index()


@st.cache_resource(show_spinner="HS부호 사전을 불러오는 중...")
def 세번표_로드():
    return hsk.load_hsk()


def DB_준비():
    """테이블이 없으면 만든다. **세션당 한 번만 돈다.**

    처음엔 @st.cache_resource 를 붙였다가 깨졌다. 캐시는 "이 함수를 이미
    돌렸다"를 프로세스가 사는 내내 기억하는데, 이 함수는 값을 계산하는 게 아니라
    바깥 상태(DB)를 바꾼다. DB 파일을 지우자 앱은 "이미 만들었다"를 들고 있어서
    다시 만들지 않았고, 저장할 때 no such table: runs 로 죽었다.

    그래서 매번 부르게 두었는데, 저장소가 Supabase 로 가면서 그건 비싸졌다.
    화면은 위젯을 건드릴 때마다 처음부터 다시 그려진다. 로컬 파일이면 공짜지만
    원격 DB 로는 **버튼 한 번에 TCP+TLS 접속 + information_schema 조회**가 붙는다.

    절충이 st.session_state 플래그다. 캐시와 달리 **새로고침하면 풀리므로**,
    DB 가 앱 모르게 사라졌을 때의 복구 경로(F5)가 살아 있다.
    """
    if st.session_state.get("db준비"):
        return
    try:
        위치 = storage.init_db()
    except Exception as e:
        # **여기서 죽으면 화면이 통째로 스택트레이스가 된다.** 기록은 부가
        # 기능이고 분류가 본체다. 플래그를 안 세우니 다음 실행에서 다시 시도한다.
        #
        # 다만 **조용히 넘어가면 안 된다.** 한 번 그렇게 뒀다가, DATABASE_URL 이
        # 안 잡혀 SQLite 로 떨어졌는데도 화면이 멀쩡해 보여서 못 알아챘다.
        # print 는 Streamlit Cloud 의 "Manage app → 로그"에 찍힌다.
        # 접속 실패 메시지에 host·user 는 나오지만 **비밀번호는 안 나온다.**
        # flush=True 가 필요하다. 파이썬은 출력이 터미널이 아니면 버퍼에 모아
        # 두는데, 배포 로그로는 그 버퍼가 언제 비워질지 알 수 없다.
        # 실제로 이 줄이 없어서 로그에 아무것도 안 찍혔다.
        print(f"[기록 저장소] 연결 실패 — {type(e).__name__}: {e}", flush=True)
        return
    # 어느 쪽에 붙었는지 로그에 한 줄 남긴다. 이게 없으면 SQLite 로 떨어진 걸
    # 화면만 봐서는 구분할 수 없다. init_db 는 접속 문자열을 돌려주지 않는다.
    print(f"[기록 저장소] {위치}", flush=True)
    st.session_state.db준비 = True


# ---------------------------------------------------------------- 일일 카운터
def 일일_사용량():
    """오늘 몇 건 분류했는지 읽는다. 날짜가 바뀌면 0부터 다시 센다.

    **원래는 .usage_daily.json 파일이었다.** 배포 파일시스템이 휘발성이라
    재배포할 때마다 상한이 0 으로 리셋됐다. CLAUDE.md 가 "상한이 유일한
    방어선"이라고 못 박은 항목이라 기록과 같은 DB 로 옮겼다.

    **DB 가 안 잡히면 0 을 돌려준다(fail-open).** 세어야 막는 건데 못 세니
    막지 못하는 셈이라 마음에 걸리는 선택이다. 그래도 이쪽을 고른 이유 —
    Supabase 무료 프로젝트는 7일 무접속이면 일시정지되는데, 그때 앱이
    "오늘 상한을 다 썼습니다"로 굳어 버리면 시연에서 URL 이 죽는다.
    막히더라도 **세션 5회 상한은 st.session_state 라서 그대로 산다.**
    """
    try:
        return storage.daily_count(date.today().isoformat())
    except Exception as e:
        print(f"[일일 카운터] 읽기 실패 — {type(e).__name__}: {e}", flush=True)
        return 0


def 일일_더하기(delta):
    """오늘 사용량을 delta 만큼 바꾼다. 실패했을 때 -1 로 되돌리는 데도 쓴다."""
    try:
        return storage.daily_add(date.today().isoformat(), delta)
    except Exception as e:
        print(f"[일일 카운터] 쓰기 실패 — {type(e).__name__}: {e}", flush=True)
        return 0


# ---------------------------------------------------------------- 세션 상태
# setdefault 는 "키가 없을 때만 넣는다". 매 실행마다 이 줄을 지나가지만
# 두 번째부터는 아무 일도 안 한다.
st.session_state.setdefault("세션id", str(uuid.uuid4()))
st.session_state.setdefault("입력", "")
st.session_state.setdefault("분류횟수", 0)
st.session_state.setdefault("게이트횟수", 0)
st.session_state.setdefault("게이트", None)   # 직전 [0] 판정 결과
st.session_state.setdefault("결과", None)     # 직전 분류 결과
st.session_state.setdefault("이력", [])
st.session_state.setdefault("카탈로그", None)  # 직전 [0-a] 추출 결과
st.session_state.setdefault("초안", None)      # 입력창에 넣은 초안 {"텍스트","빠진정보"}
st.session_state.setdefault("추출횟수", 0)


def 세번_표기(코드):
    """3924100000 → 3924.10-0000. 신고서에서 쓰는 모양으로 보여준다."""
    숫자 = "".join(ch for ch in str(코드) if ch.isdigit())
    if len(숫자) != 10:
        return 코드 or "—"
    return f"{숫자[:4]}.{숫자[4:6]}-{숫자[6:]}"


def 예시_채우기(텍스트):
    """버튼 콜백. **재실행이 시작되기 전에** 불리므로 입력창이 새 값을 집는다.

    버튼 안에서 st.session_state.입력 을 그냥 바꾸면 이미 그려진 입력창에는
    반영되지 않는다. on_click 콜백이 그 타이밍 문제를 없애 준다.
    """
    st.session_state.입력 = 텍스트


def 초안_채우기(텍스트, 빠진정보):
    """카탈로그에서 뽑은 초안을 입력창에 넣는다.

    예시_채우기 와 같은 이유로 on_click 콜백이다 — 버튼 안에서 그냥 대입하면
    이미 그려진 입력창에 반영되지 않는다.

    빠진정보를 함께 기억해 두는 것만 다르다. 두 군데에 쓴다.
      1) 입력창 아래에 "이 항목은 직접 채우세요" 를 계속 띄우는 데
      2) 분류할 때 입력출처가 '카탈로그' 인지 '카탈로그(수정)' 인지 판정하는 데
    """
    st.session_state.입력 = 텍스트
    st.session_state.초안 = {"텍스트": 텍스트, "빠진정보": 빠진정보}


def 파일_형식(f):
    """업로드 파일의 MIME 형식을 정한다. 모르면 None.

    브라우저가 알려주는 f.type 을 먼저 믿고, 비었거나 우리가 안 받는
    형식이면 파일 이름의 확장자로 판정한다. 브라우저·OS 에 따라 f.type 이
    빈 문자열로 오는 경우가 있어서다.
    """
    if f.type in pipeline.CATALOG_MIME:
        return f.type
    확장자 = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    # dict 를 뒤집어 확장자 → mime 으로 찾는다. jpg 는 jpeg 와 같은 형식이다.
    for mime, 별칭 in pipeline.CATALOG_MIME.items():
        if 확장자 == 별칭 or (확장자 == "jpeg" and 별칭 == "jpg"):
            return mime
    return None


# ---------------------------------------------------------------- 화면
st.set_page_config(page_title="HS코드 분류 검증 보조", page_icon="📦")

DB_준비()

st.title("📦 HS코드 분류 검증 보조")
st.markdown(
    "물품설명을 넣으면 **HS 10자리 후보와 그 근거가 된 과거 결정례**를 함께 돌려줍니다."
)
# 마크다운에서 줄을 바꾸려면 줄 끝에 **공백 두 칸**을 두고 개행한다.
# 그냥 \n 만 넣으면 한 문단으로 이어 붙는다.
st.warning(
    "**참고용 보조 도구입니다.** 품목분류는 신고 결과에 가산세·과태료가 따르는 영역입니다.  \n"
    "신고 전 반드시 관세사 확인 또는 관세청 품목분류 사전심사를 받으세요."
)

남은_세션 = 세션_분류_상한 - st.session_state.분류횟수
# **이 실행에서 딱 한 번만 읽는다.** 카운터가 DB 로 가면서 읽기 한 번이
# 곧 왕복 한 번이 됐다. 아래 사이드바도 이 변수를 쓰고, 분류가 돌면
# 일일_더하기 가 돌려준 새 값으로 갱신한다.
오늘_사용량 = 일일_사용량()
남은_일일 = 일일_분류_상한 - 오늘_사용량

게이트 = st.session_state.게이트
되묻는중 = bool(게이트) and not 게이트["충분"]

# --- [0-a] 카탈로그에서 초안 뽑기 ---
# **기본은 접힘.** 첫 화면을 깔끔하게 두려는 것이고, 측정된 경로는 여전히
# 글로 넣는 쪽이다. 카탈로그가 있는 사람만 펼쳐서 쓴다.
남은_추출 = 세션_추출_상한 - st.session_state.추출횟수
뽑기 = False
올린파일 = []

with st.expander("📄 카탈로그(PDF·사진)에서 물품설명 뽑기"):
    st.caption(
        "제품 안내서·사양표·제품 사진을 올리면 물품설명 초안을 만들어 드립니다.  \n"
        "**카탈로그에 없는 정보는 채우지 않습니다.** 비어 있는 항목은 따로 알려드립니다."
    )
    올린파일 = st.file_uploader(
        "카탈로그 파일",
        type=["pdf", "png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,   # 한 제품이 앞뒤 두 장에 걸칠 수 있다
        label_visibility="collapsed",
    ) or []

    c_안내, c_뽑기 = st.columns([2, 1], vertical_alignment="center")
    c_안내.caption(f"파일 {카탈로그_최대_파일수}개 · 합계 "
                  f"{pipeline.CATALOG_MAX_MB}MB 까지 · 남은 횟수 "
                  f"{남은_추출}/{세션_추출_상한}")
    뽑기 = c_뽑기.button("카탈로그 읽기", disabled=not 올린파일 or 남은_추출 <= 0,
                       use_container_width=True)

    카탈로그 = st.session_state.카탈로그
    if 카탈로그 and 카탈로그.get("오류"):
        st.error(카탈로그["오류"])
    elif 카탈로그 and 카탈로그["제품들"]:
        제품들 = 카탈로그["제품들"]
        if len(제품들) > 1:
            이름 = st.selectbox(
                "분류할 제품을 고르세요",
                range(len(제품들)),
                format_func=lambda i: 제품들[i]["이름"],
            )
            고른것 = 제품들[이름]
        else:
            고른것 = 제품들[0]
            st.markdown(f"**{고른것['이름']}**")

        with st.container(border=True):
            st.markdown(고른것["물품설명"])
        if 고른것["빠진정보"]:
            st.markdown(":orange[카탈로그에 없어서 비워 둔 항목: "
                        f"**{', '.join(고른것['빠진정보'])}**]")

        _, c_넣기 = st.columns([2, 1])
        c_넣기.button("이 초안을 물품설명에 넣기", type="primary",
                     use_container_width=True,
                     on_click=초안_채우기,
                     args=(고른것["물품설명"], 고른것["빠진정보"]))

# --- 입력 ---
st.text_area(
    # 라벨에는 **굵게** 같은 인라인 문법만 먹는다. #### 같은 제목 문법은
    # 그대로 글자로 출력된다 — 실제로 그렇게 나오는 것을 확인했다.
    "**물품설명**",
    key="입력",
    height=140,
    placeholder="재질 · 용도 · 형태 · 구성을 적을수록 정확해집니다.\n"
                "예) 스테인리스 진공 이중구조 보온병, 용량 500ml, 뚜껑 폴리프로필렌",
)
# 카탈로그 초안을 넣었으면 못 채운 항목을 **접히지 않는 자리에** 띄운다.
# 위 상자 안에도 같은 내용이 있지만 그건 접으면 사라진다. "재질은 내가
# 채워야 한다"는 사실이 안 보이면, 비어 있는 채로 분류가 돌아간다.
if st.session_state.초안 and st.session_state.초안["빠진정보"]:
    st.caption(
        ":orange[카탈로그에 없어서 비워 둔 항목: "
        f"**{', '.join(st.session_state.초안['빠진정보'])}** — 아시면 위에 직접 채워 주세요]"
    )
# 버튼 3개를 한 줄에 오른쪽 정렬로 둔다.
# 첫 칸은 비워 두는 여백이다 — 넓게 잡을수록 버튼이 오른쪽으로 밀려
# 입력창 오른쪽 모서리에 맞는다. _ 는 "안 쓰는 값"이라는 관례적 이름이다.
_, c_예1, c_예2, c_실행 = st.columns([2.2, 1, 1, 1], vertical_alignment="center")
c_예1.button("예시: 상세 설명", on_click=예시_채우기, args=(예시_충분,),
             use_container_width=True)
c_예2.button("예시: 품번만", on_click=예시_채우기, args=(예시_부족,),
             use_container_width=True)

눌림 = c_실행.button("분류하기", type="primary", disabled=남은_세션 <= 0,
                   use_container_width=True)
if 남은_세션 <= 0:
    st.error("이 브라우저에서 쓸 수 있는 횟수를 다 썼습니다.")

# --- [0] 게이트가 되물은 내용 ---
# 되묻는 내용과 빠져나갈 버튼을 테두리 하나로 묶는다. 버튼이 박스 밖에 있으면
# 무엇에 대한 '그래도'인지가 안 붙는다.
#
# st.error() 안에는 위젯을 못 넣는다. 그래서 st.container(border=True) 로
# 박스를 만들고 글자만 :red[...] 로 칠했다 — 마크다운에 색을 넣는 문법이고
# HTML 없이 된다.
강행 = False
if 되묻는중:
    with st.container(border=True):
        항목 = ", ".join(게이트["부족항목"]) or "구체적인 물품 정보"
        st.markdown(f":red[**분류하기에는 정보가 부족합니다.**]  \n"
                    f":red[비어 있는 항목: {항목}]")
        if 게이트["질문"]:
            st.markdown("아래를 알려주시면 정확해집니다.")
            st.markdown("\n".join(f"- {q}" for q in 게이트["질문"]))

        # 오른쪽 아래로 민다. 왼쪽 칸은 여백일 뿐이고, 넓게 잡을수록
        # 버튼이 좁아지면서 오른쪽 끝에 붙는다.
        # 비율을 더 좁히면 '(정확도 ↓)' 가 두 줄로 접힌다.
        _, c_강행 = st.columns([2, 1])
        강행 = c_강행.button("그래도 분류해줘 (정확도 ↓)", type="primary",
                           use_container_width=True)

# --- 직전 분류 결과 ---
# 실행 블록에서 바로 그리지 않고 상태에 담아 여기서 그린다.
# 실행 블록은 화면 맨 아래를 지나온 뒤라, 거기서 그리면 위쪽(되묻기 경고,
# 남은 횟수)이 낡은 상태로 남는다.
결과 = st.session_state.결과
if 결과 and 결과["오류"]:
    st.error("분류 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요. "
             "(사용 횟수는 차감되지 않았습니다)")

elif 결과:
    if 결과["강행"]:
        st.warning("정보가 부족한 상태로 분류했습니다. **결과를 그대로 믿지 마세요.**")

    st.markdown(f"## `{세번_표기(결과['코드'])}`")

    # 확신도. **한쪽 방향으로만 믿을 수 있다.**
    #
    # 시험 30건을 두 번 돌렸는데 결과가 비대칭이었다.
    #   high 가 아닌 답  : 10건 전부 오답 (1회차 6 + 2회차 4)
    #   high 인 답       : 65.4% 만 정답 — 3건 중 1건은 틀린다
    #
    # 그래서 high 에도 문구를 하나 붙인다. 2회차에서 **최종 코드는 30건 전건
    # 그대로였는데 오답 3건의 라벨만 medium → high 로 올라갔다.** 라벨 자체가
    # 흔들리므로 "high = 안전"으로 읽히게 두면 안 된다.
    확신도 = 결과["확신도"]
    if 확신도 == "high":
        st.success(
            "모델이 **확신도 high** 로 답했습니다.  \n"
            "다만 시험 30건에서 high 인 답도 **3건 중 1건은 오답**이었습니다. "
            "아래 확인 사항과 근거 결정례를 보세요."
        )
    else:
        st.error(
            f"모델이 **확신도 {확신도 or '미상'}** 로 답했습니다. "
            "**이 답은 틀렸을 가능성이 높습니다.**  \n"
            "시험 30건을 두 번 돌렸을 때 high 가 아닌 답 10건은 **전부 오답**이었습니다."
        )

    # --- 왜 이 코드인가 ---
    # 코드를 6자리와 뒤 4자리로 나눠 보여준다. 서로 다른 단계가 정한 것이고,
    # 실무에서 틀리는 자리도 다르다 — baseline 에서 호 경합(6자리) 41.7%,
    # 한국 고유 세번(10자리) 25.0% 로 원인이 갈렸다.
    앞6 = (결과["순위"][0] if 결과["순위"] else "")
    st.markdown("**왜 이 코드인가**")
    if 결과["top근거"]:
        줄 = f"- **앞 6자리 `{앞6}`** — {결과['top근거']}"
        if 결과["top결정례"] and 결과["top결정례"] != "없음":
            줄 += f"  \n  근거로 삼은 결정례: {결과['top결정례']}"
        st.markdown(줄)
    if 결과["확정근거"]:
        st.markdown(f"- **뒤 4자리** — {결과['확정근거']}")
    elif 결과["자동확정"]:
        st.markdown("- **뒤 4자리** — 이 6자리 아래 신고 가능한 10자리가 "
                    "하나뿐이라 자동으로 정해졌습니다.")

    # 두 단계의 확인 포인트를 합친다. dict.fromkeys 는 순서를 지키면서
    # 중복만 없앤다 — set 을 쓰면 순서가 뒤섞인다.
    포인트 = list(dict.fromkeys(결과["확인포인트"] + 결과["확정확인포인트"]))
    if 포인트:
        st.markdown("**사람이 확인해야 할 점**")
        st.markdown("\n".join(f"- {p}" for p in 포인트))

    if not 결과["자동확정"]:
        꼬리 = (f" ([4]단계 확신도 {결과['확정확신도']})"
               if 결과["확정확신도"] else "")
        st.caption(f"10자리 선택지 {결과['선택지수']}개 중에서 골랐습니다.{꼬리}")

    # --- 6자리 후보 순위 ---
    with st.expander("6자리 후보 3개와 판단 근거", expanded=False):
        if 결과["ranked"]:
            for i, r in enumerate(결과["ranked"], start=1):
                st.markdown(
                    f"**{i}. {r.get('code', '')}** — {r.get('reason', '')}  \n"
                    f"근거 결정례: {r.get('근거결정례', '없음')}"
                )
        else:
            st.markdown(" · ".join(결과["순위"]))

    # --- 근거 결정례 ---
    with st.expander(f"근거로 본 과거 결정례 {len(결과['결정례'])}건", expanded=False):
        for h in 결과["결정례"]:
            st.markdown(
                f"**{h['참조번호']}** · 유사도 {h['score']:.3f} · "
                f"결정세번 `{세번_표기(h['결정세번'])}`  \n"
                f"{h['품명']}"
            )
            st.caption(h["물품설명"])
            st.divider()

    # --- 피드백 → DB 의 '평가' 열을 UPDATE 한다 ---
    # 이 두 열이 DB 를 둔 이유다. 저장 시점에는 비어 있고 나중에 채워지므로
    # 파일에 한 줄 덧붙이는 방식으로는 안 된다.
    run_id = 결과.get("run_id")
    if run_id:
        # **언제 저장되는지가 화면에서 분명해야 한다.**
        # 전에는 👍 는 누르는 즉시, 메모는 Enter·포커스 이동 때 저장됐는데
        # 어디에도 그렇게 안 적혀 있었다. 게다가 메모가 비어 있지 않으면
        # 화면이 다시 그려질 때마다 같은 내용을 계속 다시 썼다.
        #
        # 이제 규칙은 둘뿐이다.
        #   👍/👎     → 누르는 즉시 저장. 클릭 한 번이 곧 제출이라 헷갈릴 게 없고,
        #               버튼을 한 번 더 누르게 하면 가장 값싼 신호를 놓친다
        #   글 의견   → [의견 보내기] 를 눌렀을 때만 저장
        #
        # 무엇을 이미 보냈는지 기억해 둔다. save_feedback 은 평가와 메모 두 열을
        # **한꺼번에** UPDATE 하므로, 하나만 넘기면 다른 하나가 지워진다.
        보낸 = st.session_state.setdefault(f"의견_{run_id}",
                                         {"평가": None, "메모": None})

        # 묻는 말과 👍/👎 를 **같은 줄에** 둔다. 세로로 쌓으면 둘이 한 묶음으로
        # 안 보이고, 그만큼 아래 입력칸까지 밀려 내려간다.
        # vertical_alignment 는 열 높이가 서로 다를 때 세로 가운데로 맞춘다 —
        # 이게 없으면 글씨와 아이콘이 윗변 기준으로 붙어 어긋나 보인다.
        c_묻기, c_따봉 = st.columns([3, 1], vertical_alignment="center")
        with c_묻기:
            st.markdown("**이 결과가 맞았나요?**")
        with c_따봉:
            평가 = st.feedback("thumbs", key=f"fb_{run_id}")

        메모 = st.text_input("한 줄 의견 (선택)", key=f"memo_{run_id}",
                           label_visibility="collapsed",
                           placeholder="틀렸다면 정답이나 이유를 적어주세요 (선택)")

        # 보내기 버튼은 입력칸 **오른쪽 끝**에 붙인다. 글을 다 쓰고 시선이
        # 멈추는 자리이고, 위쪽 [분류하기] 와 같은 위치라 규칙이 하나가 된다.
        # 빈 열을 왼쪽에 두는 것이 Streamlit 에서 오른쪽 정렬하는 방법이다.
        _, c_보내기 = st.columns([3, 1])
        with c_보내기:
            # type="primary" 가 테마 색을 입힌다. 기본값은 테두리만 있는 흰
            # 버튼이라 눌러야 저장된다는 게 안 보였다.
            보내기 = st.button("의견 보내기", key=f"send_{run_id}",
                            type="primary", use_container_width=True)

        새평가 = {1: "up", 0: "down"}.get(평가)
        새메모 = 메모.strip() or None

        보낼것 = None
        if 새평가 is not None and 새평가 != 보낸["평가"]:
            보낼것 = {"평가": 새평가, "메모": 보낸["메모"]}   # 메모는 건드리지 않는다
        if 보내기:
            if 새메모 is None:
                st.caption(":orange[적을 내용이 없습니다. 👍/👎 만 눌러 주셔도 됩니다.]")
            else:
                보낼것 = {"평가": 새평가, "메모": 새메모}

        if 보낼것:
            try:
                storage.save_feedback(run_id, 보낼것["평가"], 보낼것["메모"])
                st.session_state[f"의견_{run_id}"] = 보낼것
                보낸 = 보낼것
            except Exception:
                st.caption(":orange[저장하지 못했습니다. 잠시 뒤 다시 눌러 주세요.]")

        # 무엇이 저장돼 있는지 **항상** 보여준다. 화면이 다시 그려져도 남는다.
        if 보낸["평가"] or 보낸["메모"]:
            남은것 = []
            if 보낸["평가"]:
                남은것.append("👍" if 보낸["평가"] == "up" else "👎")
            if 보낸["메모"]:
                남은것.append("의견")
            st.caption(f"저장됨: {' · '.join(남은것)} — 고맙습니다.")
    elif 결과.get("저장실패"):
        # run_id 가 없으면 나중에 채울 행이 없으니 👍/👎 를 띄우지 않는다.
        # 대신 왜 안 뜨는지는 말해 준다 — 결과 자체는 정상이다.
        st.caption(f":orange[기록 저장에 실패해 의견 남기기를 띄우지 못했습니다 "
                   f"({결과['저장실패']}). 위 결과는 정상입니다.]")

    st.warning(
        "**참고용입니다.** 신고 전 관세사 확인 또는 관세청 품목분류 사전심사를 받으세요."
    )

# --- 이 브라우저에서 앞서 물어본 것들 ---
if len(st.session_state.이력) > 1:
    with st.expander(f"이전 결과 {len(st.session_state.이력) - 1}건"):
        for h in st.session_state.이력[1:]:
            st.markdown(f"`{세번_표기(h['코드'])}` ({h['확신도']}) — {h['desc'][:60]}")


# --- [0-a] 실행 ---
# 분류 실행과 마찬가지로 **맨 아래**에서 처리한다. 위쪽 상자 안에서 바로 돌리면
# 방금 만든 결과를 그릴 코드가 이미 지나간 뒤라 화면에 안 나온다.
if 뽑기:
    # **상한을 여기서 다시 검사한다.** 위의 disabled= 는 버튼이 그려질 때의
    # 상태라 한 박자 늦다. 분류 버튼에서 6번째가 통과했던 것과 같은 자리다.
    파일들 = []
    문제 = None
    합계바이트 = sum(len(f.getvalue()) for f in 올린파일)

    if st.session_state.추출횟수 >= 세션_추출_상한:
        문제 = "이 브라우저에서 카탈로그를 읽을 수 있는 횟수를 다 썼습니다."
    elif len(올린파일) > 카탈로그_최대_파일수:
        문제 = f"파일은 {카탈로그_최대_파일수}개까지 올릴 수 있습니다."
    elif 합계바이트 > pipeline.CATALOG_MAX_MB * 1024 * 1024:
        문제 = (f"파일이 너무 큽니다. 합계 {pipeline.CATALOG_MAX_MB}MB 까지 "
              f"올릴 수 있습니다 (지금 {합계바이트 / 1048576:.1f}MB).")
    else:
        for f in 올린파일:
            mime = 파일_형식(f)
            if mime is None:
                문제 = f"'{f.name}' 은 읽을 수 없는 형식입니다. PDF·PNG·JPG 만 됩니다."
                break
            파일들.append((f.getvalue(), mime))

    if 문제:
        st.session_state.카탈로그 = {"제품들": [], "오류": 문제}
    else:
        # 차감을 먼저 한다. 실패하면 아래에서 되돌린다.
        st.session_state.추출횟수 += 1
        try:
            with st.status("카탈로그를 읽는 중...", expanded=True) as 진행:
                st.write("① 파일에서 제품과 사양을 찾는 중")
                r = pipeline.catalog_extract(파일들, model=config.MODEL_DEV)
                진행.update(label="카탈로그 읽기 완료", state="complete",
                           expanded=False)
            if r["읽기실패"]:
                st.session_state.추출횟수 -= 1
                st.session_state.카탈로그 = {
                    "제품들": [],
                    "오류": "카탈로그에서 제품을 찾지 못했습니다. "
                          "제품 사양이 보이는 페이지로 다시 올리거나, "
                          "아래에 직접 입력해 주세요.",
                }
            else:
                st.session_state.카탈로그 = {
                    "제품들": r["제품들"],
                    "오류": None,
                    "in_tokens": r["in_tokens"],
                    "billed_out": r["billed_out"],
                }
        except Exception:
            # 원인 문자열을 화면에 그대로 던지지 않는다. 분류 전이라
            # DB 행도 없으므로 여기서는 남길 곳이 없다.
            st.session_state.추출횟수 -= 1
            st.session_state.카탈로그 = {
                "제품들": [],
                "오류": "카탈로그를 읽는 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요.",
            }
    st.rerun()


# --- 실행 ---
if 눌림 or 강행:
    desc = st.session_state.입력.strip()

    # **상한을 여기서 다시 검사한다.** 위의 disabled= 는 버튼이 '그려질 때'의
    # 상태라 한 박자 늦다. 마지막 1회를 쓴 직후 화면에는 아직 활성인 버튼이
    # 남아 있고, 그걸 누르면 상한을 넘어 실행된다 — 실제로 6번째가 통과했다.
    # 화면에서만 잠그고 처리에서 안 막는 것은 클라이언트 검증만 하고 서버
    # 검증을 빠뜨린 것과 같다. 여기 값은 위 변수 대신 지금 다시 읽는다.
    if not desc:
        st.error("물품설명을 입력해 주세요.")
    elif st.session_state.분류횟수 >= 세션_분류_상한:
        st.error("이 브라우저에서 쓸 수 있는 횟수를 다 썼습니다.")
    elif 일일_사용량() >= 일일_분류_상한:
        st.error("오늘 사용량을 모두 썼습니다. 내일 다시 시도해 주세요.")
    elif st.session_state.게이트횟수 >= 세션_게이트_상한:
        st.error("이 브라우저에서 요청이 너무 많았습니다. 잠시 후 새로고침해 주세요.")
    else:
        # 강행이면 게이트를 다시 부르지 않는다. 방금 부족하다고 판정한 것을
        # 또 물어봐야 답이 같고, 돈만 한 번 더 든다.
        if not 강행:
            with st.spinner("정보가 충분한지 확인하는 중..."):
                g = pipeline.gate(desc, model=config.MODEL_DEV)
            st.session_state.게이트횟수 += 1
            st.session_state.게이트 = {
                "충분": g["충분"], "부족항목": g["부족항목"], "질문": g["질문"],
            }
            # 되물어야 하면 여기서 끝낸다. **차감하지 않는다** —
            # 되물을수록 손해면 사용자가 게이트를 우회하게 되고,
            # 그러면 안전장치가 벌칙이 된다.
            if not g["충분"]:
                st.session_state.결과 = None   # 이전 분류 결과는 치운다
                st.rerun()

        # 여기부터가 실제 분류다. 차감은 이 지점에서 한 번만 한다.
        st.session_state.분류횟수 += 1
        오늘_사용량 = 일일_더하기(1)

        오류 = None
        first = third = fourth = None
        hits, 순위, ranked = [], [], []
        try:
            # st.status 는 진행 상황을 접었다 폈다 하는 상자다.
            # [3] 재정렬(pro)이 30~60초 걸리는데, 아무 표시가 없으면
            # 사용자가 멈춘 줄 알고 새로고침한다 — 그러면 호출만 두 배로 나간다.
            with st.status("분류하는 중...", expanded=True) as 진행:
                st.write("① 6자리 후보 3개를 뽑는 중")
                # use_cache=False — 앱이 만든 항목이 측정용 후보 캐시에
                # 섞이지 않게 한다. [1]은 flash 1콜이라 싸다.
                first = pipeline.generate_candidates(
                    desc, model=config.MODEL_DEV, use_cache=False)
                후보 = first["candidates"]
                if not 후보:
                    raise RuntimeError("후보를 만들지 못했습니다")

                st.write("② 비슷한 과거 결정례를 찾는 중")
                hits = search.search(desc, top_k=5, index=인덱스_로드())

                st.write("③ 결정례를 근거로 순위를 다시 판단하는 중")
                third = pipeline.rerank(desc, 후보, hits, model=config.MODEL_MAIN)
                # 재정렬이 깨졌으면 1차 순서를 그대로 쓴다. 측정에서 쓰던
                # '안 B' 와 같은 처리다 — 한 단계가 실패해도 답은 낸다.
                순위 = third["codes"] or [c["code"] for c in 후보]
                ranked = (third.get("data") or {}).get("ranked", [])

                st.write("④ 10자리 세번을 고르는 중")
                fourth = pipeline.finalize(desc, 순위[0], table=세번표_로드(),
                                           model=config.MODEL_DEV)
                진행.update(label="분류 완료", state="complete", expanded=False)
        except Exception as e:
            # 사용자에게 원인 문자열을 그대로 던지지 않는다. 화면에는 안내만,
            # 원인은 DB 에만 남긴다.
            오류 = f"{type(e).__name__}: {e}"
            오늘_사용량 = 일일_더하기(-1)          # 실패했으면 차감을 되돌린다
            st.session_state.분류횟수 -= 1

        def 합(키):
            return sum((x or {}).get(키, 0) for x in (first, third, fourth))

        결과 = {
            "desc": desc,
            "강행": 강행,
            "오류": 오류,
            "코드": (fourth or {}).get("code", ""),
            "확신도": (third.get("data") or {}).get("confidence") if third else None,
            "확인포인트": (third.get("data") or {}).get("check_points", []) if third else [],
            # [4]도 근거·확신도·확인포인트를 이미 돌려준다. 안 쓰면 낸 돈을 버리는 것이다.
            "확정근거": (fourth.get("data") or {}).get("reason") if fourth else None,
            "확정확신도": (fourth.get("data") or {}).get("confidence") if fourth else None,
            "확정확인포인트": (fourth.get("data") or {}).get("check_points", []) if fourth else [],
            "top근거": ranked[0].get("reason") if ranked else None,
            "top결정례": ranked[0].get("근거결정례") if ranked else None,
            "순위": 순위,
            "ranked": ranked,
            "결정례": [
                {"참조번호": h["참조번호"], "score": h["score"], "품명": h["품명"],
                 "결정세번": h["결정세번"], "물품설명": h["물품설명"][:400]}
                for h in hits
            ],
            "선택지수": (fourth or {}).get("선택지수", 0),
            "자동확정": (fourth or {}).get("auto", False),
            "평가": None,
        }

        # 이 분류가 카탈로그에서 왔는지, 그리고 초안을 손봤는지 남긴다.
        # 카탈로그 경로는 봉인을 다 써서 정확도를 잴 수 없으므로,
        # "초안을 그대로 썼는가"가 추출 품질을 짐작할 유일한 단서다.
        초안 = st.session_state.초안
        카탈로그 = st.session_state.카탈로그 or {}
        if not 초안:
            출처 = "텍스트"
        elif desc == 초안["텍스트"].strip():
            출처 = "카탈로그"
        else:
            출처 = "카탈로그(수정)"

        기록 = {
            "세션id": st.session_state.세션id,
            "물품설명": desc,
            "입력출처": 출처,
            "카탈로그_빠진정보": (초안 or {}).get("빠진정보", []),
            "추출_in_tokens": 카탈로그.get("in_tokens") if 초안 else None,
            "추출_billed_out": 카탈로그.get("billed_out") if 초안 else None,
            "게이트_충분": (게이트 or {}).get("충분", True),
            "게이트_부족항목": (게이트 or {}).get("부족항목", []),
            "게이트_질문": (게이트 or {}).get("질문", []),
            "강행": 강행,
            "후보1차": [c["code"] for c in (first or {}).get("candidates", [])],
            "검색결과": [{"참조번호": h["참조번호"], "score": round(h["score"], 4)}
                       for h in hits],
            "재정렬": 순위,
            "확신도": 결과["확신도"],
            "확인포인트": 결과["확인포인트"],
            "최종10자리": 결과["코드"],
            "확정근거": 결과["확정근거"],
            "확정확신도": 결과["확정확신도"],
            "선택지수": 결과["선택지수"],
            "자동확정": 결과["자동확정"],
            "모델_재정렬": config.MODEL_MAIN,
            "elapsed": round(합("elapsed"), 1),
            "in_tokens": 합("in_tokens"),
            "billed_out": 합("billed_out"),
            "오류": 오류,
        }

        # **저장 실패가 분류 결과를 지우면 안 된다.**
        # 여기까지 오는 데 pro 1콜을 썼다. 로컬 파일일 땐 실패할 일이 거의
        # 없었지만 원격 DB 는 끊긴다. 기록은 부가 기능, 답이 본체다.
        # 게이트를 fail-open 으로 둔 것과 같은 원칙이다.
        try:
            결과["run_id"] = storage.save_run(기록)
        except Exception as e:
            결과["run_id"] = None
            # 예외 메시지 대신 **종류 이름만** 보여준다. 접속 실패 메시지에는
            # 호스트나 사용자명 같은 접속 정보가 섞여 나온다.
            결과["저장실패"] = type(e).__name__

        st.session_state.결과 = 결과
        if not 오류:
            st.session_state.이력.insert(0, {
                "desc": desc, "코드": 결과["코드"], "확신도": 결과["확신도"],
            })

        # **화면을 처음부터 다시 그린다.**
        # 이 지점은 이미 경고문·입력창·버튼을 다 지나온 뒤다. 방금 바꾼 상태
        # (되묻기 해제, 남은 횟수 차감)가 위쪽 화면에 반영되려면 다시 그려야 한다.
        # 안 그러면 게이트가 '충분'으로 바뀌었는데도 직전 되묻기 경고가
        # 그대로 남는다 — 실제로 그렇게 나오는 것을 확인했다.
        st.rerun()


# ---------------------------------------------------------------- 사이드바
# **파일 맨 아래에 두는 이유** — with st.sidebar 블록은 코드 위치와 상관없이
# 항상 왼쪽에 그려진다. 그런데 위쪽에 두면 위 코드가 횟수를 차감하기 **전에**
# 그려져서, 방금 쓴 1회가 화면에 반영되지 않는다. 스크립트가 위에서 아래로
# 다시 실행된다는 성질이 그대로 드러나는 자리다.
with st.sidebar:
    # **사이드바 전체가 첫 화면에 다 들어와야 한다.** 맨 아래 저장 고지가
    # 스크롤해야 보이면 아무도 안 읽는다. 그래서 st.metric 과 st.header 를
    # 쓰지 않았다 — 숫자를 크게 그리느라 세로로 100px 넘게 먹는다.
    st.markdown("##### 남은 횟수")
    st.markdown(
        f"이 브라우저 **{세션_분류_상한 - st.session_state.분류횟수} / {세션_분류_상한}**"
        f" · 오늘 전체 **{일일_분류_상한 - 오늘_사용량} / {일일_분류_상한}**"
    )

    st.divider()
    st.markdown("##### 주의사항")
    # 사용자는 관세 실무자이지 이 프로젝트의 독자가 아니다.
    # '홀드아웃' '전환율' 같은 내부 용어를 그대로 쓰면 아무 뜻도 전달되지 않는다.
    #
    # 목록 안에서 줄을 바꾸려면 줄 끝에 공백 두 칸을 두고, 다음 줄을 두 칸
    # 들여쓴다. 들여쓰기가 없으면 같은 항목이 아니라 새 문단으로 떨어져 나간다.
    st.markdown(
        # 난이도를 안 밝히면 56.7% 가 실무 평균처럼 읽힌다. 이 30건은 전부
        # 협의회 사례(전문가끼리도 판단이 갈린 건)라 가장 어려운 구간이다.
        # 보통 난이도 20건을 같이 실어야 감이 잡힌다. README 와 같은 단어를 쓴다.
        "- **어려운 문제 30건**(전문가도 판단이 갈린 사례)에서 "
        "6자리 **56.7%** / 10자리 **50.0%**,  \n"
        "  **보통 문제 20건**에서 6자리 **85.0%** / 10자리 **80.0%** 를 맞혔습니다.\n"
        "- 같은 물품을 두 번 물으면 **답이 달라질 수 있습니다.**  \n"
        "  세 번 물어 같은 답이 나온 비율은 80%입니다.\n"
        "- 시험에 쓴 사례가 이미 공개된 결정례라 "
        "**새 물품에서는 이보다 낮을 수 있습니다.**\n"
        "- 참고하는 과거 결정례는 **5,872건**입니다."
    )
    st.divider()
    # 저장 고지는 사이드바 한 곳에만 둔다. 입력창 아래에도 있으면 같은 말이
    # 두 번 보이고, 정작 중요한 문장이 흔한 안내처럼 읽힌다.
    #
    # Streamlit 기본 컴포넌트에는 가운데 정렬 옵션이 없어서 여기만 HTML 을 쓴다.
    # 프로젝트 규칙(기본 컴포넌트만)에서 벗어나는 유일한 지점이고,
    # 사용자가 넣은 값이 아니라 내가 쓴 고정 문자열이라 위험하지 않다.
    st.markdown(
        "<div style='text-align:center; font-size:0.9em;'><b>"
        "입력한 내용과 결과가 저장됩니다.<br>개선 목적으로만 사용합니다."
        "</b></div>",
        unsafe_allow_html=True,
    )
