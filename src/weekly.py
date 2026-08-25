"""주 1회 배포 DB 와 API 를 찔러 보고 한 줄 남긴다. **읽기 전용.**

    python -m src.weekly

작업 스케줄러가 매주 월요일 09:00 에 이걸 돌린다(주간점검.bat).

**왜 DB 를 찌르는가** — Supabase 무료 프로젝트는 7일 동안 아무도 접속하지 않으면
일시정지된다. 봉인 열람 2회를 다 써서 남은 측정 채널이 "실무자가 넣은
물품설명 + 👍/👎" 하나뿐인데, 그 표가 잠들면 채널이 끊긴다.

**왜 API 도 찌르는가** — Render 무료는 15분 무활동이면 프로세스를 죽이고,
다시 깨는 데 23초가 걸린다(0단계 실측). 링크를 처음 누른 사람이 그 23초를
뒤집어쓰고, 흰 화면은 고장으로 읽힌다. 미리 깨워 두면 적어도 그 시간대는
바로 뜬다. **둘을 한 스크립트에 둔 이유** — 자동으로 도는 것을 둘로 나누면
하나는 반드시 잊는다.

**왜 화면이 아니라 파일에 남기는가** — 자동으로 도는 것의 콘솔 출력은
아무도 안 본다. 2026-08-21 에 flush 를 안 해서 배포 로그에 아무것도 안 찍힌
적이 있다. 진단 수단이 진단이 안 되면 없는 것과 같다.
"""

import time
from datetime import datetime
from pathlib import Path

import requests

from src import config
# peek 에서 조회 함수와 SQL 을 그대로 가져온다. **같은 SQL 을 두 벌 두지
# 않는다** — 한쪽만 고치면 두 곳의 숫자가 달라지고, 그걸 알아챌 방법이 없다.
from src.peek import READ_URL, 조회, 질의

로그파일 = config.ROOT / "results" / "주간점검.log"


def 직전기록():
    """로그 마지막의 정상 기록에서 up/down 개수를 꺼낸다. 없으면 None.

    지난번보다 의견이 늘었는지 보려는 것뿐이다. 파일이 없거나 형식이
    안 맞으면 **비교를 포기하고 None 을 준다** — 비교는 곁다리고,
    DB 를 깨우는 게 본래 목적이다. 곁다리 때문에 본체가 죽으면 안 된다.
    """
    if not 로그파일.exists():
        return None

    # 파일을 통째로 읽어 줄 목록으로 만든다. 로그는 1년에 52줄이라
    # 통째로 읽어도 아무 문제가 없다. 커지면 그때 생각한다.
    줄들 = 로그파일.read_text(encoding="utf-8").splitlines()

    # 뒤에서부터 본다. reversed() 는 목록을 뒤집어 준다(복사하지 않는다).
    for 줄 in reversed(줄들):
        if "up=" not in 줄:
            continue        # 실패 기록 줄이다. 건너뛴다
        값 = {}
        # 줄을 공백으로 쪼개 'up=3' 같은 조각만 골라 dict 에 넣는다.
        for 조각 in 줄.split():
            if "=" in 조각:
                이름, _, 숫자 = 조각.partition("=")
                값[이름] = 숫자
        return 값
    return None


def 한줄쓰기(줄):
    """로그 끝에 한 줄 덧붙인다. 화면에도 같이 찍는다."""
    로그파일.parent.mkdir(parents=True, exist_ok=True)
    # "a" 는 append — 기존 내용을 지우지 않고 뒤에 붙인다. "w" 로 열면
    # 매주 이전 기록이 통째로 날아간다.
    #
    # encoding="utf-8" 을 반드시 준다. 윈도우 파이썬의 기본값은 cp949 라
    # 이걸 빼면 파일에 한글이 깨져 들어간다.
    with open(로그파일, "a", encoding="utf-8") as f:
        f.write(줄 + "\n")
    print(줄)


def API깨우기():
    """배포된 API 를 한 번 찔러 콜드스타트를 미리 녹인다. 결과를 문자열로 준다.

    `/api/health` 는 LLM 을 안 부르므로 Gemini 일일 한도(250 req/day)를
    쓰지 않는다. 깨우는 것 자체가 목적이라 응답 내용은 보지 않는다.

    **몇 초 걸렸는지를 함께 남긴다.** 그게 이 호출의 유일한 관측값이다 —
    20초대면 자고 있었다는 뜻이고, 1초 미만이면 이미 깨어 있었다는 뜻이다.

    time.monotonic() 은 "시계"가 아니라 "스톱워치"다. datetime.now() 로 재면
    중간에 시스템 시각이 바뀔 때 음수가 나올 수 있다. 경과 시간에는 이쪽을 쓴다.
    """
    시작 = time.monotonic()
    try:
        # timeout 은 넉넉히. 자고 있으면 23초가 정상이다 — 짧게 주면
        # "깨우는 데 성공했는데 우리가 먼저 끊는" 일이 생긴다.
        r = requests.get(f"{config.API_URL}/api/health", timeout=90)
    except Exception as e:
        # **여기서 죽지 않는다.** API 가 안 떠도 DB 점검은 해야 한다.
        # 예외 종류 이름만 적는 것은 이 파일의 다른 곳과 같은 원칙이다.
        return f"API 실패({type(e).__name__})"

    걸린 = time.monotonic() - 시작
    if r.status_code != 200:
        return f"API {r.status_code} {걸린:.1f}s"
    return f"API ok {걸린:.1f}s"


def 점검():
    지금 = datetime.now(config.KST).strftime("%Y-%m-%d %H:%M")

    # **DB 보다 먼저, 그리고 DB 상태와 무관하게 부른다.** 아래 두 갈래는
    # return 으로 빠져나가는데, API 깨우기를 그 뒤에 두면 DB 가 잠든 주에
    # 조용히 건너뛴다. 사용자에게 보이는 건 API 쪽이다.
    api = API깨우기()

    if not READ_URL:
        한줄쓰기(f"{지금} | {api} | 건너뜀 — .env 에 READ_DATABASE_URL 이 없다")
        return

    try:
        df = 조회(질의["요약"])
    except Exception as e:
        # **접속 실패해도 죽지 않는다.** 자동으로 도는 스크립트가 예외로
        # 끝나면 작업 스케줄러 기록에만 남고 이 로그에는 아무것도 안 남는다.
        # 그러면 "안 돌았다"와 "돌았는데 실패했다"를 구분할 수 없다.
        #
        # 예외 메시지 대신 **종류 이름만** 적는다. psycopg2 의 접속 실패
        # 메시지에는 호스트와 사용자명이 섞여 나온다(app.py 와 같은 원칙).
        한줄쓰기(f"{지금} | {api} | DB 실패({type(e).__name__})")
        return

    행 = df.iloc[0]          # 요약 SQL 은 항상 한 행만 돌려준다
    이번 = {
        "전체": int(행["전체"]),
        "분류": int(행["분류완료"]),
        "up": int(행["맞음"]),
        "down": int(행["틀림"]),
    }

    # 지난번보다 의견이 늘었으면 눈에 띄게 표시한다. 이 로그를 여는 이유가
    # 결국 그것이기 때문이다.
    꼬리 = ""
    지난 = 직전기록()
    if 지난:
        늘어난것 = ((이번["up"] + 이번["down"])
                  - (int(지난.get("up", 0)) + int(지난.get("down", 0))))
        if 늘어난것 > 0:
            꼬리 = f"   ** 새 의견 {늘어난것}건 **"

    # f-string 안의 {이번['전체']} 처럼 따옴표가 겹치면 안 되므로 미리 꺼내 둔다.
    숫자 = " ".join(f"{이름}={값}" for 이름, 값 in 이번.items())
    한줄쓰기(f"{지금} | {api} | DB ok | {숫자}{꼬리}")


if __name__ == "__main__":
    점검()
