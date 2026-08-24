"""앱 실행 기록 저장 (SQLite 또는 Supabase/Postgres).

app.py 만 이 모듈을 부른다. 파이프라인([1][3][4])은 부르지 않는다 —
측정 코드와 앱 코드를 섞지 않으려는 것이다.

**저장소가 두 개인 이유**

처음에는 SQLite 하나였다. 표준 라이브러리라 의존성이 안 늘고, 접속 정보도 서버도
없고, 파일 하나가 곧 DB다. 지인 3명 규모에 Postgres 를 띄울 이유가 없었다.

깨진 건 배포 쪽이다. Streamlit Community Cloud 의 파일시스템은 휘발성이라
**앱이 재시작되거나 재배포되면 그 파일이 통째로 사라진다.** 실제로 첫 실사용
기록이 그렇게 날아갔고, 배포 환경에는 셸이 없어 사라지기 전에 꺼낼 수도 없었다.

이게 편의 문제가 아닌 이유 — 봉인(홀드아웃) 열람 2회를 다 썼다. 남은 측정 채널은
"실무자가 실제로 넣은 물품설명 + 👍/👎" 하나뿐인데, 그게 재배포 한 번에 지워지면
채널이 아니다.

그래서 config.DATABASE_URL 하나로 갈린다.

    있으면 → Supabase(Postgres). 배포에서 쓴다
    없으면 → data/runs.db (SQLite). 로컬 개발의 기본값이고 지금까지와 같다

**로컬 .env 에는 DATABASE_URL 을 넣지 않는다.** 넣으면 내 시험 실행이 지인들의
실제 기록과 같은 표에 쌓인다. 배포 기록은 Supabase 대시보드에서 본다.

**저장하는 내용에 사용자의 물품설명 원문이 들어간다.**
.gitignore 에 data/*.db 가 들어 있다. 저장소가 공개이므로 한 번 푸시되면
히스토리에서 지우기 어렵다. Postgres 쪽은 제3자(Supabase/AWS)에 영속 저장된다는
점을 사용자에게 알려야 한다.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from src import config

DB_PATH = config.DATA_DIR / "runs.db"

# 저장할 열. 순서가 곧 INSERT 문의 순서다.
# 여기 한 곳만 고치면 CREATE TABLE 과 INSERT 가 같이 따라간다.
COLUMNS = [
    ("시각", "TEXT"),
    ("세션id", "TEXT"),          # 익명 uuid4. 사람을 식별하지 않는다
    # 주소 뒤에 ?u=... 로 붙여 준 값. 링크를 누가 받았는지 갈라 두는 표시다.
    #
    # **왜 필요한가** — 봉인을 다 써서 이 표가 유일한 측정 채널인데,
    # 여기에 내 시험 행과 실무자 판정이 섞이면 인용할 수 없는 숫자가 된다.
    # 세션id 는 브라우저마다 새로 생기는 익명값이라 구분에 못 쓴다.
    # 사람 이름이 아니라 **링크 구분자**다. 누구에게 준 링크인지는 내가 안다.
    ("유입", "TEXT"),
    ("물품설명", "TEXT"),
    # [0-a] 카탈로그 입력.
    # **업로드한 파일 자체는 저장하지 않는다.** 남의 카탈로그를 보관하지 않으려는
    # 것이고, 여기 남는 건 거기서 뽑아낸 물품설명 텍스트뿐이다.
    #
    # 입력출처를 '카탈로그' 와 '카탈로그(수정)' 으로 나눈 이유 —
    # 사용자가 초안을 **그대로 썼는지 손봐야 했는지**가 남는다.
    # 카탈로그 경로는 봉인을 다 써서 정확도를 잴 수 없으므로, 추출 품질을
    # 나중에 짐작할 유일한 단서가 이것이다.
    ("입력출처", "TEXT"),            # 텍스트 / 카탈로그 / 카탈로그(수정)
    ("카탈로그_빠진정보", "TEXT"),    # JSON 배열. 카탈로그에 없어서 못 채운 항목
    ("추출_in_tokens", "INTEGER"),
    ("추출_billed_out", "INTEGER"),
    # [0] 게이트
    ("게이트_충분", "INTEGER"),
    ("게이트_부족항목", "TEXT"),   # JSON 배열
    ("게이트_질문", "TEXT"),       # JSON 배열
    ("강행", "INTEGER"),          # 부족한데도 사용자가 분류를 밀어붙였나
    # [1] ~ [4]
    ("후보1차", "TEXT"),          # JSON 배열
    ("검색결과", "TEXT"),          # JSON 배열 (참조번호 + 유사도)
    ("재정렬", "TEXT"),            # JSON 배열
    ("확신도", "TEXT"),
    ("확인포인트", "TEXT"),        # JSON 배열
    ("최종10자리", "TEXT"),
    ("확정근거", "TEXT"),          # [4] 가 그 10자리를 고른 이유
    ("확정확신도", "TEXT"),
    ("선택지수", "INTEGER"),
    ("자동확정", "INTEGER"),
    # 비용·성능
    ("모델_재정렬", "TEXT"),
    ("elapsed", "REAL"),
    ("in_tokens", "INTEGER"),
    ("billed_out", "INTEGER"),
    ("오류", "TEXT"),
    # 피드백 — 저장 시점에는 비어 있고 나중에 UPDATE 된다.
    # **이 두 열이 이 DB 의 존재 이유다.** 단순 기록이면 파일로 충분하지만,
    # "이 답이 맞았나"는 나중에 채워지므로 UPDATE 가 필요하다.
    ("평가", "TEXT"),             # 'up' / 'down' / NULL
    ("평가메모", "TEXT"),
]

# JSON 문자열로 넣을 열. SQLite 에는 JSON 타입이 없어서 TEXT 로 저장한다.
_JSON_COLS = {"게이트_부족항목", "게이트_질문", "후보1차", "검색결과",
              "재정렬", "확인포인트", "카탈로그_빠진정보"}


def _connect(path=None):
    """DB 연결을 연다. **(연결, 포스트그레스인가) 두 개를 돌려준다.**

    파이썬 함수는 `return a, b` 로 값을 여러 개 돌려줄 수 있다. 자바처럼 담을
    클래스를 만들 필요가 없다. 받는 쪽은 `conn, pg = _connect()` 로 푼다.

    갈림길은 두 가지다.
      path 를 주면      → **무조건 SQLite 파일.** 자체시험·로컬 확인용이다
      path 가 없으면    → DATABASE_URL 이 있으면 Postgres, 없으면 기본 SQLite

    row_factory / cursor_factory 는 같은 일을 한다 — 결과를 row["물품설명"] 처럼
    열 이름으로 꺼내게 해 준다. 기본값은 튜플이라 row[2] 같은 숫자 인덱스만
    되는데, 열이 30개면 그건 못 읽는 코드가 된다.
    """
    if path is None and config.DATABASE_URL:
        # **함수 안에서 import 한다.** 로컬은 SQLite 로 도니까 psycopg2 가 깔려
        # 있지 않아도 앱이 떠야 한다. 파이썬의 import 는 선언이 아니라 실행문이라
        # 이게 된다 — 이 줄에 닿기 전까지는 아무 일도 안 일어난다.
        # (자바의 import 는 컴파일 시점이라 이렇게 못 쓴다)
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            config.DATABASE_URL,
            connect_timeout=10,          # 안 잡히는 DB 를 무한정 기다리지 않는다
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        return conn, True

    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, False


def _ph(pg):
    """SQL 자리표시자. SQLite 는 ?, Postgres 는 %s 를 쓴다.

    방언 차이는 이것 말고 두 개 더 있고, 전부 init_db() 안에 모아 뒀다.
    """
    return "%s" if pg else "?"


# 아래 함수들이 전부 이 한 줄 패턴을 쓴다.
#
#     with closing(_connect(path)[0]) as conn, conn:
#
# **두 개가 하는 일이 다르다.**
#   closing(...)  : 블록을 나갈 때 연결을 닫는다
#   conn          : 블록을 나갈 때 커밋한다(예외면 롤백)
#
# sqlite3 연결은 그 자체를 with 에 넣어도 **닫히지 않는다.** 트랜잭션만 끝난다.
# Java 의 try-with-resources 가 close() 를 불러 주는 것과 다른 지점이고,
# 실제로 이걸 빼먹었더니 Windows 에서 파일이 잠겨 삭제가 안 됐다.
# 콤마로 이어 쓴 것은 with 두 개를 겹친 것과 같다.
# psycopg2 연결도 with 두 개를 똑같이 지원해서 이 패턴이 그대로 통한다.
#
# 다만 **실행은 conn 이 아니라 커서로 한다** — psycopg2 연결에는 .execute() 가
# 아예 없다. sqlite3 연결에는 있지만 .cursor() 도 있으므로, 커서 쪽으로 맞추면
# 두 DB 를 위한 코드가 하나로 합쳐진다.


def init_db(path=None):
    """테이블이 없으면 만든다. 앱 시작 때마다 불러도 안전하다.

    IF NOT EXISTS 덕분에 두 번째 호출부터는 아무 일도 안 한다.
    id 는 안 적어도 SQLite 가 rowid 로 자동 부여한다.
    """
    정의 = ",\n            ".join(f"{이름} {타입}" for 이름, 타입 in COLUMNS)
    conn, pg = _connect(path)

    # **방언 차이 두 개가 여기 다 있다.**
    #   자동 증가 PK : SQLite 는 INTEGER ... AUTOINCREMENT, Postgres 는 SERIAL
    #   기존 열 목록 : SQLite 는 PRAGMA, Postgres 는 information_schema
    # COLUMNS 의 타입(TEXT/INTEGER/REAL)은 양쪽 다 유효해서 손대지 않는다.
    # 한글 열 이름도 Postgres 에서 그대로 쓸 수 있다.
    pk = "id SERIAL PRIMARY KEY" if pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    열목록_sql = (
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_name = 'runs'"
        if pg else "PRAGMA table_info(runs)"
    )

    with closing(conn) as conn, conn:
        cur = conn.cursor()
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS runs (
            {pk},
            {정의}
        )""")

        # **CREATE TABLE IF NOT EXISTS 는 이미 있는 표에 열을 더해 주지 않는다.**
        # 나중에 COLUMNS 에 열을 하나 추가하면, 기존 DB 에서는 INSERT 가
        # "no such column" 으로 죽는다. 없는 열만 골라 덧붙여 그걸 막는다.
        cur.execute(열목록_sql)
        기존 = {r["name"] for r in cur.fetchall()}
        for 이름, 타입 in COLUMNS:
            if 이름 not in 기존:
                cur.execute(f"ALTER TABLE runs ADD COLUMN {이름} {타입}")
                print(f"  열 추가: {이름}")

        # 일일 호출 상한 카운터. 원래 .usage_daily.json 파일이었는데,
        # 배포 파일시스템이 휘발성이라 **재배포할 때마다 상한이 0 으로
        # 리셋됐다.** CLAUDE.md 가 "상한이 유일한 방어선"이라고 못 박은
        # 항목이라 runs 와 같은 DB 로 옮겼다.
        #
        # 날짜별로 한 행이다. 날짜가 바뀌면 그 날짜 행이 없으니 0 부터 센다 —
        # 파일 방식에서 날짜를 비교하던 것과 같은 동작이다.
        # 지난 날짜 행은 그냥 쌓이는데, 하루 한 행이라 신경 쓸 양이 아니다.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            날짜 TEXT PRIMARY KEY,
            횟수 INTEGER NOT NULL DEFAULT 0
        )""")

        # **Postgres(Supabase)에서는 표마다 RLS 를 켠다.**
        #
        # Supabase 는 public 스키마의 표에 anon 역할 권한을 기본으로 준다.
        # RLS 를 안 켜면 anon key 하나로 공개 REST 경로에서 **읽기뿐 아니라
        # 쓰기·삭제까지** 된다. 대시보드가 그런 표를 UNRESTRICTED 로 표시한다.
        #
        #   runs      : 사용자가 넣은 물품설명이 읽히고, 지워질 수 있다.
        #               봉인을 다 쓴 지금 유일하게 남은 측정 채널이다
        #   counters  : UPDATE counters SET 횟수 = 0 한 번이면 일일 상한이
        #               무력화된다. "상한이 유일한 방어선"인데 그게 뚫린다
        #
        # 정책(policy)을 하나도 안 만들면 그 경로가 전부 막힌다.
        # 우리 앱은 표를 만든 주인(postgres)으로 붙으므로 RLS 를 건너뛴다.
        #
        # 손으로 켜면 표를 새로 만들 때마다 잊는다. 여기서 항상 건다 —
        # 이미 켜져 있으면 아무 일도 안 하는 명령이다.
        # 실행은 아래 별도 트랜잭션에서 한다(이유는 그쪽 주석 참조).

    if pg:
        # **표를 만든 트랜잭션과 분리한다.**
        # Postgres 는 트랜잭션 안에서 한 문장이 실패하면 그 트랜잭션 전체를
        # 무효 상태로 만든다. 파이썬에서 예외를 잡아도 이미 늦어서, 같은
        # 블록 안에 두면 RLS 한 줄 때문에 CREATE TABLE 까지 롤백된다.
        # 표가 만들어지는 것이 먼저다.
        for 표 in ("runs", "counters"):
            try:
                conn2, _ = _connect(path)
                with closing(conn2) as conn2, conn2:
                    conn2.cursor().execute(
                        f"ALTER TABLE {표} ENABLE ROW LEVEL SECURITY")
            except Exception as e:
                # 못 걸더라도 앱은 떠야 한다. 다만 조용히 넘기지 않는다 —
                # 안 걸린 것을 모르는 게 제일 나쁘다.
                print(f"  RLS 설정 실패({표}) — {type(e).__name__}: {e}",
                      flush=True)
    # 어디에 만들었는지만 돌려준다. **DATABASE_URL 자체는 절대 돌려주지 않는다** —
    # 접속 문자열에 비밀번호가 들어 있어서, 로그나 화면에 찍히면 그게 유출이다.
    return path or ("Supabase(Postgres)" if pg else DB_PATH)


def _to_db(이름, 값):
    """파이썬 값을 SQLite 가 받는 형태로 바꾼다."""
    if 값 is None:
        return None
    if 이름 in _JSON_COLS:
        # ensure_ascii=False 를 줘야 한글이 \uXXXX 로 안 깨진다.
        return json.dumps(값, ensure_ascii=False)
    if isinstance(값, bool):
        # SQLite 에는 불리언이 없다. 0/1 로 넣는다.
        return int(값)
    return 값


def save_run(row, path=None):
    """실행 1건을 저장하고 id 를 돌려준다.

    row 는 dict 다. 없는 키는 NULL 로 들어간다 — 게이트에서 멈춘 건은
    [1]~[4] 열이 전부 비게 되는데, 그 자체가 "되물어서 분류하지 않았다"는 기록이다.

    인자 30개짜리 함수를 만들지 않은 이유는 부르는 쪽에서 순서를 틀리기 때문이다.

    자리표시자를 쓰고 문자열을 직접 이어 붙이지 않는다. 사용자가 넣은
    물품설명이 그대로 SQL 이 되는 것을 막는다(SQL 인젝션). JDBC 의
    PreparedStatement 와 같은 이유·같은 방식이다.
    """
    이름들 = [이름 for 이름, _ in COLUMNS]
    값들 = [_to_db(이름, row.get(이름)) for 이름 in 이름들]

    # 시각을 안 넘겼으면 지금 시각을 넣는다.
    #
    # **config.KST 를 반드시 넘긴다.** 인자 없는 datetime.now() 는 코드가 도는
    # 기계의 시각이라, 배포 컨테이너(UTC)에서는 9시간 이른 값이 저장된다.
    # 남는 문자열은 '2026-08-21T17:17:00+09:00' 처럼 오프셋이 붙는데, 이걸
    # 잘라내지 않는 이유는 **데이터만 보고 시간대를 판정할 수 있어야** 같은 일이
    # 다시 났을 때 바로 알아채기 때문이다.
    if row.get("시각") is None:
        값들[이름들.index("시각")] = (
            datetime.now(config.KST).isoformat(timespec="seconds")
        )

    conn, pg = _connect(path)
    자리 = ", ".join(_ph(pg) for _ in 이름들)
    sql = f"INSERT INTO runs ({', '.join(이름들)}) VALUES ({자리})"

    with closing(conn) as conn, conn:
        cur = conn.cursor()
        if pg:
            # Postgres 에는 lastrowid 가 없다. 방금 넣은 행의 id 를 달라고
            # INSERT 문에 직접 붙여서 받아 온다. 나중에 👍/👎 를 그 행에
            # 채우려면 id 가 반드시 있어야 한다.
            cur.execute(sql + " RETURNING id", 값들)
            return cur.fetchone()["id"]
        cur.execute(sql, 값들)
        return cur.lastrowid


def save_feedback(run_id, 평가, 메모=None, path=None):
    """나중에 눌린 👍/👎 를 그 행에 채운다.

    이것 때문에 파일 append 가 아니라 DB 여야 한다. 이미 쓴 줄을 고쳐야 하는데,
    CSV 에 한 줄 덧붙이는 방식으로는 못 한다.
    """
    conn, pg = _connect(path)
    q = _ph(pg)
    with closing(conn) as conn, conn:
        conn.cursor().execute(
            f"UPDATE runs SET 평가 = {q}, 평가메모 = {q} WHERE id = {q}",
            (평가, 메모, run_id),
        )


def daily_count(날짜, path=None):
    """그 날짜에 몇 건 썼는지 돌려준다. 행이 없으면 0 이다.

    날짜는 '2026-08-21' 같은 문자열이다. 부르는 쪽에서
    datetime.now(config.KST).date().isoformat() 으로 넘긴다 — **KST 기준**이다.
    서버 시각(배포 컨테이너는 UTC)을 쓰면 상한이 한국 시각 오전 9시에 리셋된다.
    """
    conn, pg = _connect(path)
    with closing(conn) as conn, conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 횟수 FROM counters WHERE 날짜 = {_ph(pg)}", (날짜,))
        행 = cur.fetchone()
    # max(0, ...) 는 방어다. daily_add 가 음수로 되돌릴 때 0 아래로 내려가는 것을
    # 파이썬 쪽에서 막는다. SQL 로 막으려면 SQLite 는 max(), Postgres 는
    # GREATEST() 라 방언이 갈린다 — 굳이 갈릴 이유가 없다.
    return max(0, 행["횟수"] if 행 else 0)


def daily_add(날짜, delta, path=None):
    """그 날짜의 사용량을 delta 만큼 바꾸고 새 값을 돌려준다.

    delta 는 +1 로 미리 깎고, 실패하면 -1 로 되돌리는 데 쓴다.
    호출을 **하기 전에** 깎는 게 중요하다 — 30~60초 걸리는 분류가 도는
    동안 다른 사람이 같은 상한을 다시 쓰는 것을 막는다.

    UPSERT(INSERT ... ON CONFLICT)를 쓰지 않고 읽고-쓴다. 그 문법이
    SQLite 와 Postgres 에서 미묘하게 달라서, 확인할 수 없는 쪽(Postgres)에서
    틀리면 상한이 통째로 안 걸린다. **두 사람이 정확히 같은 순간에 누르면
    한 건이 덜 세어질 수 있지만**, 파일 방식도 같은 성질이었고 이 규모에서
    문제가 되지 않는다. 확실히 도는 쪽을 골랐다.
    """
    conn, pg = _connect(path)
    q = _ph(pg)
    with closing(conn) as conn, conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 횟수 FROM counters WHERE 날짜 = {q}", (날짜,))
        행 = cur.fetchone()
        if 행 is None:
            새값 = max(0, delta)
            cur.execute(
                f"INSERT INTO counters (날짜, 횟수) VALUES ({q}, {q})",
                (날짜, 새값),
            )
        else:
            새값 = max(0, 행["횟수"] + delta)
            cur.execute(
                f"UPDATE counters SET 횟수 = {q} WHERE 날짜 = {q}",
                (새값, 날짜),
            )
    return 새값


def session_count(세션id, path=None):
    """그 브라우저가 지금까지 분류한 건수. 세션 상한(5회)을 세는 데 쓴다.

    **Streamlit 앱은 이 함수가 필요 없다.** 거기서는 st.session_state 에
    분류횟수를 들고 있으면 됐다 — 서버가 세션을 메모리에 붙들고 있기 때문이다.
    FastAPI 에는 그런 게 없다. 브라우저가 만든 익명 uuid4 를 매 요청에 실어
    보내면, 서버는 그 값으로 runs 표를 세는 수밖에 없다.

    **오류로 끝난 건은 빼고 센다.** app.py 가 실패했을 때 분류횟수를 1 되돌리는
    것과 같은 처리다(app.py:673). 안 빼면 서버가 죽어서 못 받은 답 때문에
    사용자가 남은 횟수를 잃는다.

    우회할 수 있는 방어라는 것을 알고 쓴다 — 브라우저 저장소를 지우면 새 uuid
    가 생긴다. 다만 그건 Streamlit 도 마찬가지였고(세션 초기화), 진짜 방어선은
    일일 상한이며 그건 DB 에 있다.
    """
    conn, pg = _connect(path)
    with closing(conn) as conn, conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT count(*) AS n FROM runs WHERE 세션id = {_ph(pg)} "
            f"AND 오류 IS NULL",
            (세션id,),
        )
        행 = cur.fetchone()
    # sqlite3.Row 와 psycopg2 의 RealDictRow 둘 다 이름으로 꺼낼 수 있다.
    return 행["n"] if 행 else 0


def load_runs(limit=100, path=None):
    """최근 기록을 새 것부터 돌려준다. 확인용이다.

    **앱 화면에는 이 함수를 붙이지 않는다.** 공개 URL 에 조회 화면을 만들면
    남이 입력한 물품설명이 그대로 노출된다. 조회는 로컬에서만 한다.
    """
    conn, pg = _connect(path)
    with closing(conn) as conn, conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM runs ORDER BY id DESC LIMIT {_ph(pg)}", (limit,))
        rows = cur.fetchall()
    # sqlite3.Row 를 그대로 두면 밖에서 쓰기 불편하다. dict 로 바꿔 돌려준다.
    # RealDictCursor 가 준 행에도 dict() 가 그대로 통한다.
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # 진짜 DB 를 더럽히지 않으려고 별도 파일에 시험한다.
    # data/*.db 는 .gitignore 에 걸려 있다.
    시험 = config.DATA_DIR / "runs_selftest.db"
    시험.unlink(missing_ok=True)

    init_db(시험)
    print(f"테이블 생성: {시험.name}")

    run_id = save_run({
        "세션id": "test-session",
        "물품설명": "스테인리스 진공 보온병, 용량 500ml, 뚜껑 폴리프로필렌",
        "게이트_충분": True,
        "게이트_부족항목": [],
        "게이트_질문": [],
        "강행": False,
        "후보1차": ["961700", "732393", "392490"],
        "검색결과": [{"참조번호": "가상-001", "score": 0.83}],
        "재정렬": ["961700", "732393", "392490"],
        "확신도": "high",
        "확인포인트": ["진공 구조인지 확인 필요"],
        "최종10자리": "9617001000",
        "선택지수": 2,
        "자동확정": False,
        "모델_재정렬": config.MODEL_MAIN,
        "elapsed": 12.3,
        "in_tokens": 4200,
        "billed_out": 5100,
    }, path=시험)
    print(f"저장 완료: id={run_id}")

    행 = load_runs(path=시험)[0]
    print(f"  물품설명 : {행['물품설명'][:30]}...")
    print(f"  후보1차  : {json.loads(행['후보1차'])}")
    print(f"  평가     : {행['평가']}  (아직 비어 있어야 정상)")

    save_feedback(run_id, "up", "맞았습니다", path=시험)
    행 = load_runs(path=시험)[0]
    print(f"  평가     : {행['평가']} / {행['평가메모']}  (UPDATE 반영 확인)")

    # 게이트에서 멈춘 건도 한 행으로 남는다. [1]~[4] 열은 비어 있다.
    gid = save_run({
        "세션id": "test-session",
        "물품설명": "SKU 88213",
        "게이트_충분": False,
        "게이트_부족항목": ["물품의 종류", "재질", "용도"],
        "게이트_질문": ["어떤 물품인가요?"],
        "강행": False,
    }, path=시험)
    행 = load_runs(path=시험)[0]
    print(f"저장 완료: id={gid} (게이트 중단)")
    print(f"  게이트_충분 : {행['게이트_충분']}  (0 이면 되물었다는 뜻)")
    print(f"  최종10자리  : {행['최종10자리']}  (None 이어야 정상)")
    print(f"  부족항목    : {json.loads(행['게이트_부족항목'])}")

    print(f"\n전체 {len(load_runs(path=시험))}건")

    # 일일 카운터
    오늘 = datetime.now(config.KST).date().isoformat()
    print(f"\n=== 일일 카운터 ({오늘}) ===")
    print(f"  처음      : {daily_count(오늘, path=시험)}  (행이 없으니 0)")
    daily_add(오늘, 1, path=시험)
    daily_add(오늘, 1, path=시험)
    print(f"  +1 두 번  : {daily_count(오늘, path=시험)}")
    daily_add(오늘, -1, path=시험)
    print(f"  -1 되돌림 : {daily_count(오늘, path=시험)}")
    daily_add(오늘, -5, path=시험)
    print(f"  -5 더     : {daily_count(오늘, path=시험)}  (0 아래로 안 내려가야 정상)")
    print(f"  다른 날짜 : {daily_count('1999-01-01', path=시험)}  (0 이어야 정상)")
    시험.unlink()
    print("시험 파일 삭제 완료")
