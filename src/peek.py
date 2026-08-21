"""배포 DB(Supabase) 를 터미널에서 들여다본다. **읽기 전용.**

    python -m src.peek            최근 20건
    python -m src.peek 의견        👍/👎 나 메모가 달린 것만
    python -m src.peek 요약        전체 집계 한 줄
    python -m src.peek 12         id=12 한 건을 전부 펼쳐서
    python -m src.peek sql "select ..."   직접 짠 조회

**왜 DATABASE_URL 이 아니라 READ_DATABASE_URL 인가**

.env 에 DATABASE_URL 을 넣으면 config.py 가 그걸 읽고, 로컬에서 앱을
돌릴 때도 Supabase 에 쓰게 된다. 내 시험 행이 지인들 실제 기록과 같은
표에 섞인다. 이름을 다르게 두면 **이 파일만** 배포 DB 를 보고,
앱은 지금처럼 로컬 SQLite 를 쓴다.

    .env 에 이렇게 한 줄 넣으세요 (.gitignore 에 들어 있습니다)
    READ_DATABASE_URL=postgresql://postgres.xxxx:비밀번호@aws-....:5432/postgres

**쓰기는 하지 않는다.** sql 모드도 select 로 시작하지 않으면 거절한다.
지우거나 고치는 일은 Supabase 대시보드에서 눈으로 보면서 한다.
"""

import os
import sys
import warnings
from contextlib import closing

import pandas as pd
from dotenv import load_dotenv

from src import config

# 한글이 cp949 콘솔에서 깨지는 것을 막는다. 명령 앞에 PYTHONIOENCODING=utf-8
# 를 매번 붙이지 않아도 되게 코드에서 처리한다.
# **stderr 도 함께 해야 한다.** stdout 만 고쳤더니 오류가 났을 때 파이썬이
# 찍는 추적 메시지의 한글 함수 이름이 깨져 나와, 정작 필요할 때 못 읽었다.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(config.ROOT / ".env")
READ_URL = os.getenv("READ_DATABASE_URL")

# 자주 보는 열만. 전부 뽑으면 30열이라 터미널에서 못 읽는다.
요약열 = """
  id,
  to_char(시각::timestamp, 'MM-DD HH24:MI')        as 시각,
  입력출처,
  left(물품설명, 34)                                as 물품설명,
  case when 게이트_충분 = 0 then '되물음' end       as 게이트,
  최종10자리,
  확신도,
  평가,
  left(평가메모, 20)                                as 메모,
  round(elapsed::numeric, 1)                        as 초
"""

질의 = {
    "최근": f"select {요약열} from runs order by id desc limit 20",
    "의견": f"""select {요약열} from runs
               where 평가 is not null or 평가메모 is not null
               order by id desc""",
    "요약": """select
        count(*)                                        as 전체,
        count(*) filter (where 게이트_충분 = 0)          as 되물음,
        count(*) filter (where 최종10자리 is not null)   as 분류완료,
        count(*) filter (where 오류 is not null)         as 오류,
        count(*) filter (where 평가 = 'up')              as 맞음,
        count(*) filter (where 평가 = 'down')            as 틀림,
        count(*) filter (where 입력출처 like '카탈로그%') as 카탈로그,
        round(avg(elapsed)::numeric, 1)                  as 평균초,
        sum(in_tokens)                                   as 입력토큰,
        sum(billed_out)                                  as 출력토큰
        from runs""",
}


def 조회(sql, params=None):
    """SELECT 를 돌려 DataFrame 으로 돌려준다.

    psycopg2 를 여기서 import 하는 이유는 storage.py 와 같다 — 이 파일을
    안 쓰는 사람에게 설치를 강요하지 않는다.
    """
    import psycopg2

    # pandas 는 SQLAlchemy 를 쓰라고 경고하지만, 그것 때문에 의존성을 하나 더
    # 들일 이유가 없다. 조회는 그대로 된다. 경고만 끈다.
    warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

    # storage.py 와 같은 이유로 closing() 을 겹친다 — psycopg2 연결도
    # with 만으로는 트랜잭션만 끝나고 닫히지 않는다.
    with closing(psycopg2.connect(READ_URL, connect_timeout=10)) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def 출력(df):
    if df.empty:
        print("(해당하는 기록이 없습니다)")
        return
    # 기본값이면 긴 물품설명 하나가 표를 통째로 밀어낸다.
    with pd.option_context("display.max_columns", None,
                           "display.width", 200,
                           "display.max_colwidth", 40):
        print(df.to_string(index=False))
    print(f"\n{len(df)}건")


def 상세(run_id):
    """한 건을 세로로 펼친다. JSON 열도 그대로 보여준다."""
    df = 조회("select * from runs where id = %s", (run_id,))
    if df.empty:
        print(f"id={run_id} 인 기록이 없습니다.")
        return
    행 = df.iloc[0]
    for 이름 in df.columns:
        값 = 행[이름]
        if 값 is None or (isinstance(값, float) and pd.isna(값)):
            continue
        print(f"{이름:>18} : {값}")


if __name__ == "__main__":
    if not READ_URL:
        print("READ_DATABASE_URL 이 없습니다.\n"
              ".env 에 아래 한 줄을 넣으세요 (값은 Supabase 접속 문자열).\n"
              "  READ_DATABASE_URL=postgresql://postgres.xxxx:비밀번호@...:5432/postgres")
        sys.exit(1)

    인자 = sys.argv[1] if len(sys.argv) > 1 else "최근"

    if 인자 == "sql":
        직접 = " ".join(sys.argv[2:]).strip()
        # 읽기 전용이라는 약속을 코드로 지킨다. 실수로 delete 를 치는 것을 막는다.
        if not 직접.lower().startswith("select"):
            print("select 로 시작하는 조회만 됩니다. 고치거나 지우는 일은 "
                  "Supabase 대시보드에서 하세요.")
            sys.exit(1)
        출력(조회(직접))
    elif 인자 in 질의:
        출력(조회(질의[인자]))
    elif 인자.isdigit():
        상세(int(인자))
    else:
        print(__doc__)
