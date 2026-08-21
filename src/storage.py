"""앱 실행 기록 저장 (SQLite).

app.py 만 이 모듈을 부른다. 파이프라인([1][3][4])은 부르지 않는다 —
측정 코드와 앱 코드를 섞지 않으려는 것이다.

**왜 SQLite 인가**
sqlite3 는 파이썬 표준 라이브러리다. requirements.txt 가 안 늘고, 접속 정보도
없고, 서버도 없다. 파일 하나가 곧 DB다. 지인 3명 규모에서 Postgres 를 띄울
이유가 없다.

**알아둘 한계**
Streamlit Community Cloud 의 파일시스템은 휘발성이다. 앱이 재시작되면 이 파일이
사라진다. 로컬 실행에서는 영속하고, 배포 환경에서는 컨테이너가 사는 동안만 남는다.
영속이 필요해지면 _connect() 안쪽만 외부 DB 로 바꾸면 된다.

**저장하는 내용에 사용자의 물품설명 원문이 들어간다.**
.gitignore 에 data/*.db 가 들어 있다. 저장소가 공개이므로 한 번 푸시되면
히스토리에서 지우기 어렵다.
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
    """DB 연결을 연다. 파일이 없으면 그 자리에서 만들어진다.

    row_factory 를 sqlite3.Row 로 두면 결과를 row["물품설명"] 처럼
    열 이름으로 꺼낼 수 있다. 기본값은 튜플이라 row[2] 같은 숫자 인덱스만
    되는데, 열이 22개면 그건 못 읽는 코드가 된다.
    """
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 아래 함수들이 전부 이 한 줄 패턴을 쓴다.
#
#     with closing(_connect(path)) as conn, conn:
#
# **두 개가 하는 일이 다르다.**
#   closing(...)  : 블록을 나갈 때 연결을 닫는다
#   conn          : 블록을 나갈 때 커밋한다(예외면 롤백)
#
# sqlite3 연결은 그 자체를 with 에 넣어도 **닫히지 않는다.** 트랜잭션만 끝난다.
# Java 의 try-with-resources 가 close() 를 불러 주는 것과 다른 지점이고,
# 실제로 이걸 빼먹었더니 Windows 에서 파일이 잠겨 삭제가 안 됐다.
# 콤마로 이어 쓴 것은 with 두 개를 겹친 것과 같다.


def init_db(path=None):
    """테이블이 없으면 만든다. 앱 시작 때마다 불러도 안전하다.

    IF NOT EXISTS 덕분에 두 번째 호출부터는 아무 일도 안 한다.
    id 는 안 적어도 SQLite 가 rowid 로 자동 부여한다.
    """
    정의 = ",\n            ".join(f"{이름} {타입}" for 이름, 타입 in COLUMNS)
    with closing(_connect(path)) as conn, conn:
        conn.execute(f"""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {정의}
        )""")

        # **CREATE TABLE IF NOT EXISTS 는 이미 있는 표에 열을 더해 주지 않는다.**
        # 나중에 COLUMNS 에 열을 하나 추가하면, 기존 DB 파일에서는 INSERT 가
        # "no such column" 으로 죽는다. 없는 열만 골라 덧붙여 그걸 막는다.
        기존 = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        for 이름, 타입 in COLUMNS:
            if 이름 not in 기존:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {이름} {타입}")
                print(f"  열 추가: {이름}")
    return path or DB_PATH


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

    인자 22개짜리 함수를 만들지 않은 이유는 부르는 쪽에서 순서를 틀리기 때문이다.

    ? 자리표시자를 쓰고 문자열을 직접 이어 붙이지 않는다. 사용자가 넣은
    물품설명이 그대로 SQL 이 되는 것을 막는다(SQL 인젝션). JDBC 의
    PreparedStatement 와 같은 이유·같은 방식이다.
    """
    이름들 = [이름 for 이름, _ in COLUMNS]
    값들 = [_to_db(이름, row.get(이름)) for 이름 in 이름들]

    # 시각을 안 넘겼으면 지금 시각을 넣는다.
    if row.get("시각") is None:
        값들[이름들.index("시각")] = datetime.now().isoformat(timespec="seconds")

    자리 = ", ".join("?" for _ in 이름들)
    sql = f"INSERT INTO runs ({', '.join(이름들)}) VALUES ({자리})"

    with closing(_connect(path)) as conn, conn:
        cur = conn.execute(sql, 값들)
        return cur.lastrowid


def save_feedback(run_id, 평가, 메모=None, path=None):
    """나중에 눌린 👍/👎 를 그 행에 채운다.

    이것 때문에 파일 append 가 아니라 DB 여야 한다. 이미 쓴 줄을 고쳐야 하는데,
    CSV 에 한 줄 덧붙이는 방식으로는 못 한다.
    """
    with closing(_connect(path)) as conn, conn:
        conn.execute(
            "UPDATE runs SET 평가 = ?, 평가메모 = ? WHERE id = ?",
            (평가, 메모, run_id),
        )


def load_runs(limit=100, path=None):
    """최근 기록을 새 것부터 돌려준다. 확인용이다.

    **앱 화면에는 이 함수를 붙이지 않는다.** 공개 URL 에 조회 화면을 만들면
    남이 입력한 물품설명이 그대로 노출된다. 조회는 로컬에서만 한다.
    """
    with closing(_connect(path)) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    # sqlite3.Row 를 그대로 두면 밖에서 쓰기 불편하다. dict 로 바꿔 돌려준다.
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
    시험.unlink()
    print("시험 파일 삭제 완료")
