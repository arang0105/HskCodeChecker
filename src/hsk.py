"""HS 부호 사전 — 6자리 아래 10자리 목록.

[4] 세번 확정 단계가 하는 일은 '확정된 6자리 밑에서 10자리 하나를 고르는' 것이다.
그 선택지 목록을 만드는 파일이다. API를 부르지 않으므로 비용이 0원이다.

출처 : 공공데이터포털 · 관세청 'HS부호 단위별 품목명' (기준일자 2026-01-01)
왜 코퍼스 역산이 아닌가 : 결정례 5,921건에서 뽑으면 6자리당 10자리가 평균
1.32종뿐이라, 모델이 '고를 게 없어서' 맞히는 상황이 된다. [4]의 성능이 부풀려진다.
"""

import pandas as pd

from src import config, evaluate

XLSX = config.DATA_DIR / "관세청_HS부호 단위별 품목명_20260101.xlsx"
HSK_CSV = config.DATA_DIR / "hsk_codes.csv"

# (시트 이름, 그 시트의 코드 열 이름).
# 'HS8단위(7, 9단위포함)' 은 쉼표 뒤 공백까지 원본과 정확히 같아야 한다.
# 한 글자만 달라도 pandas 가 시트를 못 찾고 죽는다.
SHEETS = [
    ("HS2단위", "HS2단위"),
    ("HS4단위", "HS4단위"),
    ("HS6단위(5단위포함)", "HS6단위"),
    ("HS8단위(7, 9단위포함)", "HS8단위"),
    ("HS10단위", "HS10단위"),
]

# 상위설명에 붙일 자릿수.
# 2단위(류)는 뺀다 — 6자리가 이미 확정된 마당에 '제39류 플라스틱' 은 새 정보가 아니다.
# 5·7·9 가 섞여 있는 이유는 HS 체계가 홀수 단위 중간 마디를 쓰기 때문이다.
PARENT_LENS = (4, 5, 6, 7, 8, 9)


def build_hsk_codes():
    """XLSX 5개 시트를 읽어 data/hsk_codes.csv 를 만든다. 한 번만 돌리면 된다."""
    names = {}   # 코드(문자열) → 한글품목명. 모든 단위를 한 표에 모은다
    ten = None

    for sheet, code_col in SHEETS:
        # dtype=str 필수. 안 주면 pandas 가 '0101211000' 을 숫자로 읽어 앞자리 0 이 날아간다.
        # fillna("") 는 빈 칸을 NaN 이 아니라 빈 문자열로 만든다 (NaN 은 문자열 연산에서 터진다).
        df = pd.read_excel(XLSX, sheet_name=sheet, dtype=str).fillna("")

        # 채점과 같은 규칙으로 정규화한다. 다른 규칙을 쓰면 '목록에 있는데 없다'는
        # 사고가 조용히 난다. .map(함수) 는 열의 모든 값에 함수를 한 번씩 먹인다.
        codes = df[code_col].map(evaluate.normalize_code)

        for code, kor in zip(codes, df["한글품목명"]):
            names[code] = kor.strip()

        if sheet == "HS10단위":
            df["코드"] = codes
            ten = df

        print(f"  {sheet:<18} {len(df):>6}행")

    rows = []
    for code, kor, eng in zip(ten["코드"], ten["한글품목명"], ten["영문품목명"]):
        # 상위 마디의 이름을 위에서부터 이어붙인다.
        # 없는 단위는 건너뛴다 — 국내 세분이 없어 중간 마디가 아예 안 생긴 6자리가 있다.
        # 아래는 list comprehension. Java 로 치면 for + if + add 세 줄을 한 줄로 쓴 것이다.
        parents = [names[code[:n]] for n in PARENT_LENS if code[:n] in names]

        rows.append({
            "hs10": code,
            "hs6": code[:6],
            "품목명": kor.strip(),
            "상위설명": " > ".join(parents),
            "영문품목명": eng.strip(),
        })

    out = pd.DataFrame(rows)
    # utf-8-sig : 엑셀로 열었을 때 한글이 깨지지 않게 하는 BOM 을 붙인다
    out.to_csv(HSK_CSV, index=False, encoding="utf-8-sig")
    print(f"\n저장 완료: {len(out)}행 → {HSK_CSV.name}")
    return out


def load_hsk():
    """만들어 둔 사전을 읽는다."""
    if not HSK_CSV.exists():
        raise FileNotFoundError("먼저 build_hsk_codes() 를 실행하세요.")
    return pd.read_csv(HSK_CSV, dtype=str).fillna("")


def codes_under(hs6, table=None):
    """6자리를 주면 그 아래 10자리 선택지를 dict 리스트로 돌려준다.

    [4] 프롬프트는 이 함수 하나만 쓰면 된다.

    table : load_hsk() 결과를 미리 읽어 넘기면 파일을 반복해 읽지 않는다.
            29건 배치에서 CSV 를 29번 읽을 이유가 없다.
    """
    df = table if table is not None else load_hsk()
    key = evaluate.normalize_code(hs6)[:6]

    # df["hs6"] == key 는 True/False 배열을 만들고, df[...] 는 True 인 행만 고른다.
    # to_dict("records") 는 그 결과를 행 하나당 dict 하나인 리스트로 바꾼다.
    return df[df["hs6"] == key].to_dict("records")


if __name__ == "__main__":
    table = build_hsk_codes() if not HSK_CSV.exists() else load_hsk()

    print("\n=== 자체 검증 ===")
    print(f"10자리 총 건수 : {len(table)}  (기대 11,327)")

    # 6자리당 몇 개인가. value_counts() 는 값별 개수를 세어 준다.
    per6 = table["hs6"].value_counts()
    print(f"6자리 종류     : {len(per6)}")
    print(f"6자리당 평균   : {len(table) / len(per6):.2f}종  (코퍼스 역산은 1.32종이었다)")

    cases = evaluate.load_cases("A")
    있음 = set(table["hs10"])
    이름 = dict(zip(table["hs10"], table["품목명"]))
    상위 = dict(zip(table["hs10"], table["상위설명"]))

    존재 = [c for c in cases if evaluate.normalize_code(c["정답"]) in 있음]
    print(f"\n평가셋 정답 10자리 존재 : {len(존재)}/{len(cases)}")
    for c in cases:
        ans = evaluate.normalize_code(c["정답"])
        if ans not in 있음:
            print(f"  없음 → {c['no']}번 {ans}")

    # [4]가 실제로 판단해야 하는 건 선택지가 2개 이상인 건뿐이다.
    선택지 = [len(codes_under(evaluate.normalize_code(c["정답"])[:6], table)) for c in cases]
    단독 = sum(1 for n in 선택지 if n == 1)
    print(f"\n정답 6자리의 선택지 : 평균 {sum(선택지) / len(선택지):.2f}개 · "
          f"최대 {max(선택지)}개 · 1개뿐 {단독}건")
    print(f"  → 6자리만 맞히면 자동 확정되는 건 {단독}건, "
          f"[4]가 실제로 판단하는 건 {len(cases) - 단독}건")

    # 정답의 품목명이 '기타' 처럼 짧으면 이름만으로는 아무 판단도 못 한다.
    짧음 = [c for c in cases
            if len(이름.get(evaluate.normalize_code(c["정답"]), "")) <= 3]
    print(f"\n정답 품목명이 3자 이하 : {len(짧음)}/{len(cases)} "
          f"({len(짧음) / len(cases) * 100:.1f}%)  → 상위설명이 반드시 필요하다")
    빈상위 = [c for c in 짧음 if not 상위.get(evaluate.normalize_code(c["정답"]), "")]
    if 빈상위:
        for c in 빈상위:
            print(f"  상위설명 비어 있음 → {c['no']}번 {evaluate.normalize_code(c['정답'])}")
    else:
        print("  상위설명이 비어 있는 건 없음")

    ex = evaluate.normalize_code(cases[0]["정답"])
    print(f"\n=== 예시: {ex[:6]} 아래 선택지 ===")
    for r in codes_under(ex[:6], table):
        mark = "★" if r["hs10"] == ex else " "
        print(f"{mark} {r['hs10']}  {r['품목명']}")
        print(f"      {r['상위설명']}")
