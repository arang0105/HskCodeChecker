"""배포 전에 도는 검사 몇 개. **LLM 을 한 번도 부르지 않는다.**

    python -m src.checks

**pytest 를 안 쓴다.** 검사가 셋인데 틀을 들여올 이유가 없고, CLAUDE.md 가
「테스트 커버리지 확대」를 스코프 밖으로 두고 있다. 여기 모은 것은 커버리지가
아니라 **조용히 틀릴 수 있는 자리**만 골라 막은 것이다. 시끄럽게 죽는 코드는
그냥 두고, 틀린 채로 그럴듯한 답을 내는 코드만 본다.

GEMINI_API_KEY 가 있어야 import 가 되지만(src/config.py) **값은 안 쓴다.**
CI 는 가짜 문자열을 넣어 준다.
"""

import sys

from src import config

# 두 requirements 에 같이 적힌 패키지는 버전이 같아야 한다.
_REQ = [config.ROOT / "requirements.txt", config.ROOT / "api" / "requirements.txt"]


def _버전들(path):
    """requirements 파일에서 {패키지: 버전} 을 뽑는다. 주석·빈 줄은 건너뛴다."""
    표 = {}
    for 줄 in path.read_text(encoding="utf-8").splitlines():
        줄 = 줄.strip()
        if not 줄 or 줄.startswith("#") or "==" not in 줄:
            continue
        이름, _, 버전 = 줄.partition("==")
        표[이름.strip()] = 버전.strip()
    return 표


def 요구사항_버전():
    """루트와 api 의 requirements 가 공통 패키지에서 같은 버전을 쓰는가.

    **api/requirements.txt 주석이 이걸 요구하는데 지키는 코드가 없었다.**
    두 앱(Streamlit·FastAPI)이 같은 Supabase 표에 기록하므로, 여기서 버전이
    갈리면 같은 입력에 다른 결과가 나올 수 있다. 사람이 한쪽만 올리기
    딱 좋은 자리다.
    """
    루트, api = _버전들(_REQ[0]), _버전들(_REQ[1])
    어긋남 = [(이름, 루트[이름], api[이름])
            for 이름 in sorted(루트.keys() & api.keys())      # & 는 dict 키의 교집합
            if 루트[이름] != api[이름]]
    if 어긋남:
        raise RuntimeError(
            "두 requirements 의 버전이 갈렸습니다: "
            + ", ".join(f"{n} (루트 {a} / api {b})" for n, a, b in 어긋남))
    return f"공통 패키지 {len(루트.keys() & api.keys())}개 버전 일치"


def 채점_정규화():
    """코드 비교 전 정규화가 CLAUDE.md 채점 규칙대로 도는가.

    `.`·`-`·공백·non-breaking space(\xa0)·탭을 지운다. 이게 틀어지면
    맞힌 답이 오답으로 집계되는데, 비율만 보면 알아챌 수 없다.
    """
    from src import evaluate

    표본 = [
        ("3207.20-9000", "3207209000"),      # 점과 하이픈
        ("3820\xa0002000", "3820002000"),    # 엑셀에서 딸려오는 non-breaking space
        ("\t3926 90 9000 ", "3926909000"),   # 탭과 공백
        (None, ""),                          # 없는 값은 빈 문자열
        (3926909000, "3926909000"),          # 숫자로 들어와도 문자열로
    ]
    for 넣은값, 기대 in 표본:
        실제 = evaluate.normalize_code(넣은값)
        if 실제 != 기대:
            raise RuntimeError(f"normalize_code({넣은값!r}) → {실제!r}, 기대 {기대!r}")
    return f"표본 {len(표본)}개 통과"


def 코퍼스_표시():
    """검색에서 빼야 할 건들이 코퍼스에 표시돼 있는가.

    실제 검사는 search.load_index() 안에 있다. 여기서는 그걸 부르기만 한다 —
    앱이 기동할 때 도는 것과 **같은 코드**여야 하기 때문이다. 검사를 두 벌로
    두면 CI 만 통과하고 배포가 죽는다.
    """
    from src import search

    _, meta = search.load_index()
    return (f"{len(meta)}건 · 평가셋 {search.평가셋_건수} · 회귀셋 {search.회귀셋_건수}")


def main():
    실패 = 0
    for 검사 in (요구사항_버전, 채점_정규화, 코퍼스_표시):
        try:
            print(f"  OK  {검사.__name__} — {검사()}")
        except Exception as e:
            # 첫 실패에서 멈추지 않는다. 한 번 돌려서 다 보는 게 낫다.
            실패 += 1
            print(f"  NG  {검사.__name__} — {type(e).__name__}: {e}")
    print(f"\n{'실패 ' + str(실패) + '건' if 실패 else '전부 통과'}")
    return 1 if 실패 else 0


if __name__ == "__main__":
    sys.exit(main())
