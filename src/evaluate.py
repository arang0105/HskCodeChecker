"""평가 자동화.

2주차의 핵심 파일. 여기서 하는 일은 두 가지다.
  1) 평가셋(엑셀)을 읽어 파이썬이 다루기 쉬운 형태로 바꾼다
  2) HSK 코드를 비교 가능한 형태로 정규화한다

아직 API는 부르지 않는다. 이 단계는 비용 0원이다.
"""

import json
from datetime import datetime

import pandas as pd

from src import config, llm

# 코드 비교 전에 지울 문자들 (CLAUDE.md 채점 규칙).
# \xa0 은 눈에는 공백으로 보이지만 일반 공백과 다른 문자다.
# 엑셀/웹에서 복사한 값에 자주 섞여 들어와 비교를 조용히 실패시킨다.
_JUNK = [".", "-", " ", "\xa0", "\t"]


def normalize_code(code):
    """'3207.20-9000' → '3207209000' 처럼 비교 가능한 형태로 만든다.

    정답 열은 이미 깨끗하지만, 모델 응답은 점·하이픈을 섞어서 온다.
    그래서 양쪽 모두 이 함수를 통과시킨 뒤에만 비교한다.
    """
    if code is None:
        return ""

    # 파이썬은 타입 선언이 없어서 숫자가 들어올 수도 있다. 문자열로 강제.
    text = str(code)
    for ch in _JUNK:
        text = text.replace(ch, "")
    return text


def load_cases(condition="A"):
    """평가셋을 읽어 dict 리스트로 돌려준다.

    condition="A" → 물품설명 입력, 유효_A 기준 (29건)
    condition="B" → 품명 원문 입력, 유효_B 기준 (28건)

    조건마다 유효 행이 다르기 때문에 인자로 받는다.
    """
    # header=None : 엑셀 첫 줄을 열 이름으로 쓰지 않고 0,1,2... 번호로 다룬다.
    #               이 시트는 위에 안내문이 있어 첫 줄이 열 이름이 아니다.
    # dtype=str   : 필수. 안 주면 pandas가 '0902100000' 을 숫자로 읽어
    #               앞자리 0 이 사라진다.
    df = pd.read_excel(
        config.BASELINE_XLSX,
        sheet_name="케이스목록",
        header=None,
        dtype=str,
    )

    # 엑셀 5행부터 데이터. 파이썬 인덱스는 0부터라 4가 된다.
    rows = df.iloc[4:]

    # 조건에 따라 볼 유효 열이 다르다 (H열=7, I열=8).
    valid_col = 7 if condition == "A" else 8
    # 입력으로 쓸 열도 다르다 (E열=4 물품설명, C열=2 품명 원문).
    input_col = 4 if condition == "A" else 2

    cases = []
    # iterrows() : 데이터프레임을 한 행씩 도는 방법. (인덱스, 행) 쌍이 나온다.
    for _, row in rows.iterrows():
        # 유효 플래그가 'O' 가 아니면 집계에서 제외한다.
        if row[valid_col] != "O":
            continue

        cases.append({
            "no": row[0],
            "결정례번호": row[1],
            "입력": row[input_col],
            "정답": normalize_code(row[3]),
            "품명유형": row[6],
        })

    return cases


def load_testset():
    """홀드아웃 테스트셋 30건을 load_cases() 와 같은 형식으로 읽는다.

    **열람 규칙** (CLAUDE.md) — 5주차 종료 시 1회, 6주차 종료 시 1회, 총 2회뿐.
    이 함수를 부르는 것 자체가 그 횟수를 쓰는 일이다. 부르기 전에 개발셋에서
    할 수 있는 일이 남아 있지 않은지 먼저 확인할 것.

    번호를 'T1' 처럼 붙이는 이유 — baseline 케이스 번호가 1~30이라
    그대로 두면 diff_baseline() 이 **엉뚱한 건끼리 대조한다.** 겹치지 않는
    이름을 주면 baseline 에 없는 번호로 인식돼 대조에서 조용히 빠진다.
    """
    df = pd.read_csv(config.DATA_DIR / "테스트셋.csv", dtype=str).fillna("")

    cases = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        cases.append({
            "no": f"T{i}",
            "결정례번호": row["참조번호"],
            "입력": row["물품설명"],
            "정답": normalize_code(row["결정세번_정규화"]),
            # 테스트셋에는 품명유형 분류가 없다. 되묻기 지표는 평가셋에서만 잰다.
            "품명유형": "",
        })
    return cases


def grade(candidates, answer):
    """후보 목록과 정답을 받아 O/X를 판정한다.

    candidates : 모델이 준 코드 리스트. 1순위가 맨 앞.
    answer     : 정답 코드 (10자리)

    모델은 6자리를 주고 정답은 10자리이므로, 비교 전에 양쪽을
    같은 자릿수로 잘라야 한다. 이 '자르기'가 채점의 전부다.
    """
    ans = normalize_code(answer)
    codes = [normalize_code(c) for c in candidates]

    # [f(x) for x in ...] 는 list comprehension.
    # Java의 stream().map().toList() 를 한 줄로 쓴 것이다.
    # 여기서는 후보 전부를 정규화해 새 리스트로 만들었다.

    top1 = codes[0] if codes else ""

    return {
        "top1_4": top1[:4] == ans[:4],
        "top1_6": top1[:6] == ans[:6],
        # any() 는 하나라도 True 면 True. Java의 anyMatch 와 같다.
        "top3_4": any(c[:4] == ans[:4] for c in codes[:3]),
        "top3_6": any(c[:6] == ans[:6] for c in codes[:3]),
    }


def summarize(results):
    """건별 채점 결과 리스트를 받아 비율로 집계한다.

    results : grade() 가 돌려준 dict 들의 리스트
    """
    total = len(results)
    if total == 0:
        return {}

    summary = {"건수": total}
    for key in ("top1_4", "top1_6", "top3_4", "top3_6"):
        hit = sum(1 for r in results if r[key])
        # 파이썬에서 True 는 1로 취급되지만, 명시적으로 세는 편이 읽기 낫다.
        summary[key] = f"{hit / total * 100:.1f}% ({hit}/{total})"
    return summary


def load_baseline():
    """baseline 측정기록에서 건별 O/X를 읽어 {번호: {...}} 로 돌려준다.

    '측정기록' 시트는 조건 A 결과다. 유효_A 가 O 인 29건만 담는다.
    엑셀에는 1/0 으로 기록돼 있어 True/False 로 바꾼다.
    """
    df = pd.read_excel(
        config.BASELINE_XLSX, sheet_name="측정기록", header=None, dtype=str
    )

    baseline = {}
    for _, row in df.iloc[4:].iterrows():
        if row[4] != "O":  # 유효_A
            continue
        baseline[row[0]] = {
            "정답": normalize_code(row[3]),
            "top1_6": row[14] == "1",
            "top3_6": row[15] == "1",
            "후보": [normalize_code(row[c]) for c in (11, 12, 13)],
            "오답유형": row[16],
        }
    return baseline


def diff_baseline(rows):
    """이번 실행 결과를 baseline과 건별로 대조해 출력한다.

    rows : run_batch() 가 만든 행 리스트 (또는 detail.csv 를 읽은 것)
    """
    base = load_baseline()

    # 4가지로 나눈다. 회수/손실이 핵심이다.
    buckets = {"유지_O": [], "회수": [], "손실": [], "유지_X": []}

    for r in rows:
        b = base.get(str(r["no"]))
        if b is None:
            continue
        now, was = bool(r["top1_6"]), b["top1_6"]
        if was and now:
            key = "유지_O"
        elif not was and now:
            key = "회수"
        elif was and not now:
            key = "손실"
        else:
            key = "유지_X"
        buckets[key].append((r["no"], b["오답유형"]))

    print("\n--- baseline 대조 (Top1 6자리) ---")
    for key in ("유지_O", "회수", "손실", "유지_X"):
        items = buckets[key]
        nums = ", ".join(str(n) for n, _ in items)
        print(f"  {key:6} {len(items):>2}건  {nums}")

    print(f"\n  순증감: {len(buckets['회수']) - len(buckets['손실']):+d}건")
    return buckets


def run_batch(condition="A", model=None, limit=None, note=""):
    """평가셋 전체를 돌려 채점하고 results/ 에 저장한다.

    limit : 앞에서 N건만 돌린다. 파이프라인 점검용 (예: limit=3)
    note  : 이번 실행이 무엇이었는지 메모. 나중에 실험 이력이 된다.
    """
    cases = load_cases(condition)
    if limit:
        cases = cases[:limit]

    model_name = model or config.MODEL_DEV
    rows = []
    graded = []

    for i, case in enumerate(cases, start=1):
        prompt = llm.CANDIDATE_PROMPT.format(desc=case["입력"])
        result = llm.ask_json(prompt, model=model_name)

        # 파싱 실패해도 멈추지 않는다. 빈 후보로 두고 계속 간다.
        if result["data"]:
            candidates = [c["code"] for c in result["data"]["candidates"]]
        else:
            candidates = []

        g = grade(candidates, case["정답"])
        graded.append(g)

        rows.append({
            "no": case["no"],
            "결정례번호": case["결정례번호"],
            "품명유형": case["품명유형"],
            "정답": case["정답"],
            "정답6": case["정답"][:6],
            # join 은 리스트를 구분자로 이어붙인다. Java의 String.join 과 같다.
            "후보": " | ".join(candidates),
            "top1_4": g["top1_4"],
            "top1_6": g["top1_6"],
            "top3_6": g["top3_6"],
            "elapsed": round(result["elapsed"], 1),
            "in_tokens": result["in_tokens"],
            "out_tokens": result["out_tokens"],
            "think_tokens": result["think_tokens"],
            "billed_out": result["billed_out"],
            "parse_error": result["parse_error"],
            "raw": result["text"],
        })

        mark = "O" if g["top1_6"] else ("~" if g["top3_6"] else "X")
        print(f"  [{i}/{len(cases)}] no={case['no']:>2} {mark} "
              f"정답={case['정답'][:6]} 후보={candidates}")

    # 결과를 실행 시각별 폴더에 남긴다. CLAUDE.md 실험 기록 규칙.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = config.RESULTS_DIR / stamp
    # parents=True : 중간 폴더도 함께 생성. exist_ok=True : 있어도 에러 안 냄
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(rows)
    # utf-8-sig : 엑셀에서 열 때 한글이 깨지지 않게 하는 인코딩
    df.to_csv(outdir / "detail.csv", index=False, encoding="utf-8-sig")

    # 이번 실행에 쓴 프롬프트 템플릿을 그대로 남긴다.
    # 이게 없으면 나중에 "이 숫자가 어떤 프롬프트에서 나왔는지"를 알 수 없다.
    (outdir / "prompt.txt").write_text(llm.CANDIDATE_PROMPT, encoding="utf-8")

    summary = summarize(graded)
    meta = {
        "실행시각": stamp,
        "조건": condition,
        "모델": model_name,
        "메모": note,
        "총_elapsed": round(sum(r["elapsed"] for r in rows), 1),
        "총_in_tokens": sum(r["in_tokens"] for r in rows),
        "총_billed_out": sum(r["billed_out"] for r in rows),
        "파싱실패": sum(1 for r in rows if r["parse_error"]),
        **summary,
    }
    # ensure_ascii=False : 한글을 \uXXXX 로 바꾸지 않고 그대로 저장
    (outdir / "summary.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    for k, v in meta.items():
        print(f"  {k}: {v}")
    print(f"\n  저장: {outdir}")
    return meta


def _final_summary(rows):
    """[4] 세번 확정 관련 집계만 따로 만든다.

    10자리를 세 가지로 쪼개 본다. 하나로 뭉치면 누구 잘못인지 알 수 없다.
      top1_10  : 전체 대비. 목표(40%)를 재는 숫자
      전환율    : 6자리를 맞힌 건 중 10자리 정답률. [4] 자체의 실력
      판단분    : 그중 선택지가 2개 이상이라 모델이 실제로 고른 건만
    선택지가 1개뿐인 건은 6자리만 맞으면 공짜로 따라오므로,
    빼고 봐야 [4]가 정말 일을 했는지 알 수 있다.
    """
    total = len(rows)
    맞힌6 = [r for r in rows if r["top1_6"]]
    판단 = [r for r in 맞힌6 if not r["자동확정"]]

    def ratio(부분, 전체):
        if not 전체:
            return "0/0"
        return f"{len(부분) / len(전체) * 100:.1f}% ({len(부분)}/{len(전체)})"

    맞힌10 = [r for r in rows if r["top1_10"]]
    return {
        "after_A_top1_10": ratio(맞힌10, rows),
        "전환율_6to10": ratio([r for r in 맞힌6 if r["top1_10"]], 맞힌6),
        "판단분_전환율": ratio([r for r in 판단 if r["top1_10"]], 판단),
        "자동확정": f"{sum(1 for r in rows if r['자동확정'])}/{total}",
        "확정위반건수": sum(1 for r in rows if r["확정위반"]),
        "선택지_평균": round(sum(r["선택지수"] for r in rows) / total, 2),
    }


def run_rerank_batch(condition="A", model=None, top_k=5, limit=None, note="",
                     use_cache=True, with_final=True, cases=None):
    """[1]후보 → [2]검색 → [3]재정렬 → [4]세번확정 전체를 돌려 채점한다.

    run_batch() 와 나란히 두는 이유 — 저쪽은 [1]단계만 재는 함수이고
    이건 [3]까지 재는 함수다. 하나로 합치면 분기가 늘어 읽기 어려워진다.

    한 번 실행으로 세 가지 숫자를 남긴다.
      before  : 1차 후보 그대로 (재정렬을 안 했다면 어땠을까)
      after_A : 재정렬 결과 그대로  ← 공식 숫자
      after_B : 재정렬이 규칙을 어긴 건만 1차 후보 순서로 되돌린 것

    before 를 같은 실행에서 함께 재는 게 중요하다. 다른 날 잰 숫자와
    비교하면 temperature 1 의 실행 간 변동이 개선분에 섞인다.

    with_final=True 면 [4]까지 돌려 10자리를 채점한다. 10자리는 두 가지로 본다.
      top1_10  : 전체 대비 10자리 정답률          ← 목표 40% 를 재는 숫자
      전환율    : **6자리를 맞힌 건 중** 10자리 정답률 ← [4] 자체의 실력
    [3]이 6자리를 틀리면 [4]는 무슨 짓을 해도 틀리므로, 둘을 나눠야
    '[4]가 못한 것'과 '[3]이 못한 것'을 구분할 수 있다.
    """
    from src import hsk, pipeline, search

    # cases 를 직접 넘기면 그걸 쓴다. 테스트셋(load_testset)이나
    # 평가셋 일부(일관성 10건)를 돌릴 때 쓴다.
    if cases is None:
        cases = load_cases(condition)
    if limit:
        cases = cases[:limit]

    model_name = model or config.MODEL_DEV

    # 인덱스를 한 번만 읽어 재사용한다. 29건마다 3천 건짜리 파일을
    # 다시 읽을 이유가 없다.
    index = search.load_index()
    # 후보 캐시도 마찬가지로 한 번만 읽는다.
    cache = pipeline.load_candidate_cache()
    # 10자리 사전(11,327행)도 한 번만 읽는다.
    hsk_table = hsk.load_hsk() if with_final else None

    rows = []
    for i, case in enumerate(cases, start=1):
        desc = case["입력"]
        ans6 = case["정답"][:6]

        # --- [1] 후보 생성 (같은 조건이면 캐시에서 꺼낸다) ---
        first = pipeline.generate_candidates(
            desc, model=model_name, use_cache=use_cache, cache=cache
        )
        candidates = first["candidates"]
        codes_before = [c["code"] for c in candidates]

        # --- [2] 결정례 검색 ---
        hits = search.search(desc, top_k=top_k, index=index)
        hit6 = [h["결정세번"][:6] for h in hits]

        # --- [3] 재정렬 ---
        # 후보가 없으면 재정렬할 게 없다. API를 낭비하지 않는다.
        if candidates:
            third = pipeline.rerank(desc, candidates, hits, model=model_name)
        else:
            third = {"codes": [], "위반": "1차 후보 없음", "data": None,
                     "elapsed": 0, "in_tokens": 0, "billed_out": 0,
                     "parse_error": first["parse_error"], "text": ""}

        codes_a = third["codes"]
        # 안 B: 규칙을 어겼으면 재정렬을 없던 일로 하고 1차 순서를 쓴다.
        codes_b = codes_before if third["위반"] else codes_a

        g_before = grade(codes_before, case["정답"])
        g_a = grade(codes_a, case["정답"])
        g_b = grade(codes_b, case["정답"])

        # --- [4] 세번 확정 ---
        # 재정렬 1순위 6자리를 그대로 받는다. 그게 오답이면 10자리도 반드시
        # 오답이지만, 실제 파이프라인이 그렇게 동작하므로 그대로 잰다.
        if with_final and codes_a:
            fourth = pipeline.finalize(desc, codes_a[0], table=hsk_table,
                                       model=model_name)
        else:
            fourth = {"code": "", "선택지수": 0, "auto": True, "위반": "",
                      "elapsed": 0, "in_tokens": 0, "billed_out": 0,
                      "parse_error": None, "data": None, "text": ""}

        top1_10 = bool(fourth["code"]) and fourth["code"] == normalize_code(case["정답"])

        rows.append({
            "no": case["no"],
            "결정례번호": case["결정례번호"],
            "품명유형": case["품명유형"],
            "정답": case["정답"],
            "정답6": ans6,
            "후보1차": " | ".join(codes_before),
            "후보캐시": first["cached"],
            "검색세번": " | ".join(hit6),
            # 검색이 정답을 물어왔는지. 재정렬이 실패했을 때
            # 원인이 검색인지 판단인지 가르는 열이다.
            "검색적중": ans6 in hit6,
            "재정렬": " | ".join(codes_a),
            "위반": third["위반"] or "",
            "확신도": (third["data"] or {}).get("confidence", ""),
            "before_top1_6": g_before["top1_6"],
            "before_top3_6": g_before["top3_6"],
            "top1_4": g_a["top1_4"],
            "top1_6": g_a["top1_6"],
            "top3_6": g_a["top3_6"],
            "b_top1_6": g_b["top1_6"],
            "세번확정": fourth["code"],
            "선택지수": fourth["선택지수"],
            # 선택지가 1개뿐이라 API 없이 결정된 건. 평가셋 29건 중 8건이 여기다.
            # [4]의 성적에서 이 건들은 '모델이 잘한 것'이 아니다.
            "자동확정": fourth["auto"] and bool(fourth["code"]),
            "확정위반": fourth["위반"] or "",
            "top1_10": top1_10,
            "elapsed": round(first["elapsed"] + third["elapsed"] + fourth["elapsed"], 1),
            "in_tokens": first["in_tokens"] + third["in_tokens"] + fourth["in_tokens"],
            "billed_out": (first["billed_out"] + third["billed_out"]
                           + fourth["billed_out"]),
            "parse_error": third["parse_error"] or fourth["parse_error"],
            "raw": third["text"],
            "raw_확정": fourth["text"],
        })

        # 화살표로 재정렬이 무엇을 바꿨는지 한눈에 본다.
        mark_b = "O" if g_before["top1_6"] else "X"
        mark_a = "O" if g_a["top1_6"] else "X"
        moved = "  " if mark_b == mark_a else ("↑" if mark_a == "O" else "↓")
        mark_10 = ("O" if top1_10 else "X") if with_final else "-"
        print(f"  [{i}/{len(cases)}] no={case['no']:>2} {mark_b}→{mark_a} {moved} "
              f"10자리={mark_10} "
              f"{'[캐시]' if first['cached'] else '[신규]'} "
              f"정답={ans6} 검색적중={'Y' if ans6 in hit6 else 'N'} "
              f"{codes_before} → {codes_a} {third['위반'] or ''}")
        if with_final:
            print(f"        [4] {fourth['code'] or '-'} / 정답 {case['정답']} "
                  f"(선택지 {fourth['선택지수']}개"
                  f"{', 자동' if fourth['auto'] else ''}) {fourth['위반'] or ''}")

    # --- 저장 ---
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = config.RESULTS_DIR / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(rows)
    df.to_csv(outdir / "detail.csv", index=False, encoding="utf-8-sig")

    # 프롬프트 두 개를 모두 남긴다. 한쪽만 남기면 재현이 안 된다.
    # f""" ... """ 는 여러 줄 f-string. 줄바꿈을 그대로 쓸 수 있어
    # 이스케이프 문자 없이 파일 모양을 눈으로 확인하며 쓸 수 있다.
    (outdir / "prompt.txt").write_text(
        f"""=== [1] 후보 생성 ===
{llm.CANDIDATE_PROMPT}

=== [3] 재정렬 ===
{pipeline.RERANK_PROMPT}

=== [4] 세번 확정 ===
{pipeline.FINALIZE_PROMPT}""",
        encoding="utf-8",
    )

    total = len(rows)

    def pct(key):
        """열 하나의 True 비율을 '41.4% (12/29)' 형태로 만든다."""
        hit = sum(1 for r in rows if r[key])
        return f"{hit / total * 100:.1f}% ({hit}/{total})"

    meta = {
        "실행시각": stamp,
        "조건": condition,
        "모델": model_name,
        "top_k": top_k,
        "메모": note,
        "건수": total,
        "before_top1_6": pct("before_top1_6"),
        "before_top3_6": pct("before_top3_6"),
        "after_A_top1_6": pct("top1_6"),
        "after_A_top1_4": pct("top1_4"),
        "after_A_top3_6": pct("top3_6"),
        "after_B_top1_6": pct("b_top1_6"),
        "검색적중률": pct("검색적중"),
        "위반건수": sum(1 for r in rows if r["위반"]),
        # ** 는 dict 를 펼쳐 넣는 문법이다. [4]를 안 돌렸으면 빈 dict 를 펼쳐
        # 아무 키도 안 생긴다. 괄호는 필수 — 없으면 조건식이 어디까지인지 헷갈린다.
        **(_final_summary(rows) if with_final else {}),
        # 캐시가 몇 건을 아꼈는지. 토큰 합계가 왜 줄었는지 설명해 준다.
        "후보캐시_적중": f"{sum(1 for r in rows if r['후보캐시'])}/{total}",
        "총_elapsed": round(sum(r["elapsed"] for r in rows), 1),
        "총_in_tokens": sum(r["in_tokens"] for r in rows),
        "총_billed_out": sum(r["billed_out"] for r in rows),
        "파싱실패": sum(1 for r in rows if r["parse_error"]),
    }
    (outdir / "summary.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    for k, v in meta.items():
        print(f"  {k}: {v}")

    diff_baseline(rows)
    print()
    print(f"  저장: {outdir}")
    return meta


# baseline 일관성 측정에 쓴 10건. 같은 건으로 재야 비교가 성립한다.
일관성_케이스 = ["6", "8", "9", "28", "29", "2", "12", "15", "17", "23"]


def run_consistency(nos=None, repeat=3, model=None, condition="A", reuse=()):
    """같은 질문을 여러 번 돌려 답이 흔들리는지 잰다.

    **일관성 = 3회가 서로 같은가**이지, 정답인가가 아니다. 틀린 답을
    3번 똑같이 해도 일관성은 1이다. baseline(6자리 90% / 10자리 60%)도
    같은 정의로 집계됐다.

    **캐시를 반드시 끈다.** 캐시를 켜면 [1]단계가 첫 실행 값에 고정돼
    3회 모두 같은 후보에서 출발한다. 그러면 일관성이 좋아 보이지만
    그건 캐시가 만든 착시다.

    reuse : 이미 돌려 둔 회차의 타임스탬프 목록(예: ["20260819_171305"]).
            도중에 끊겼을 때 남은 회차만 이어서 돌리려고 쓴다.
            **같은 조건에서 나온 것만 넣을 것** — 모델이나 대상이 다른 실행을
            섞으면 '일관성'이 아니라 서로 다른 것의 비교가 된다.
    """
    all_cases = load_cases(condition)
    골라낼 = set(nos or 일관성_케이스)
    cases = [c for c in all_cases if str(c["no"]) in 골라낼]

    if len(cases) != len(골라낼):
        찾음 = {str(c["no"]) for c in cases}
        raise ValueError(f"평가셋에서 못 찾은 번호: {sorted(골라낼 - 찾음)}")

    runs = []
    for stamp in reuse:
        path = config.RESULTS_DIR / stamp / "detail.csv"
        df = pd.read_csv(path, dtype=str).fillna("")
        if len(df) != len(cases):
            raise ValueError(f"{stamp} 는 {len(df)}건인데 이번 대상은 {len(cases)}건이다")
        runs.append(df)
        print(f"  재사용: {stamp} ({len(df)}건)")

    # 이미 있는 회차 다음부터 돌린다.
    for i in range(len(reuse), repeat):
        print(f"\n===== 일관성 {i + 1}/{repeat} 회차 =====")
        meta = run_rerank_batch(
            cases=cases, model=model, use_cache=False,
            note=f"일관성 {i + 1}/{repeat}회차",
        )
        # 회차별 원문은 results/{stamp}/ 에 그대로 남는다. 다시 읽어 비교한다.
        path = config.RESULTS_DIR / meta["실행시각"] / "detail.csv"
        runs.append(pd.read_csv(path, dtype=str).fillna(""))

    print(f"\n===== 일관성 집계 ({len(cases)}건 × {repeat}회) =====")
    rows = []
    for case in cases:
        no = str(case["no"])
        정답 = case["정답"]

        # 회차마다 이 건의 6자리·10자리 답을 모은다.
        답6, 답10 = [], []
        for run in runs:
            r = run[run["no"] == no]
            첫코드 = r.iloc[0]["재정렬"].split(" | ")[0] if len(r) else ""
            답6.append(normalize_code(첫코드)[:6])
            답10.append(r.iloc[0]["세번확정"] if len(r) else "")

        # set 의 크기가 1이면 전부 같다는 뜻이다.
        rows.append({
            "no": no,
            "정답": 정답,
            "6자리_답": " | ".join(답6),
            "10자리_답": " | ".join(답10),
            "일치_6": len(set(답6)) == 1,
            "일치_10": len(set(답10)) == 1,
            "정답일치_6": sum(1 for a in 답6 if a == 정답[:6]),
            "정답일치_10": sum(1 for a in 답10 if a == 정답),
        })

    n = len(rows)
    일치6 = sum(1 for r in rows if r["일치_6"])
    일치10 = sum(1 for r in rows if r["일치_10"])

    for r in rows:
        print(f"  no={r['no']:>2} 6자리{'O' if r['일치_6'] else 'X'} "
              f"10자리{'O' if r['일치_10'] else 'X'}  "
              f"정답 {r['정답']}  [{r['10자리_답']}]")

    meta = {
        "건수": n,
        "반복": repeat,
        "모델": model or config.MODEL_DEV,
        "일관성_6자리": f"{일치6 / n * 100:.0f}% ({일치6}/{n})",
        "일관성_10자리": f"{일치10 / n * 100:.0f}% ({일치10}/{n})",
        "정답재현_6자리": round(sum(r["정답일치_6"] for r in rows) / n, 1),
        "정답재현_10자리": round(sum(r["정답일치_10"] for r in rows) / n, 1),
    }
    print()
    for k, v in meta.items():
        print(f"  {k}: {v}")
    print("\n  baseline: 일관성 6자리 90% / 10자리 60% · "
          "정답재현 6자리 1.9 / 10자리 1.3 (pro, 10건 3회)")
    return meta, rows



if __name__ == "__main__":
    for cond in ("A", "B"):
        cases = load_cases(cond)
        print(f"[조건 {cond}] 유효 {len(cases)}건")

        # 품명유형 분포를 세어 본다. dict 를 카운터로 쓰는 가장 단순한 방법.
        counts = {}
        for c in cases:
            counts[c["품명유형"]] = counts.get(c["품명유형"], 0) + 1
        print(f"  품명유형: {counts}")

        first = cases[0]
        print(f"  1번째: no={first['no']} 정답={first['정답']}")
        print(f"         입력={first['입력'][:50]}...")
        print()

    print("--- normalize_code 확인 ---")
    for sample in ["3207.20-9000", "3207209000", " 0902\xa010-0000 "]:
        print(f"{sample!r:28} -> {normalize_code(sample)!r}")

    print()
    print("--- grade 확인 (API 안 부름) ---")
    # (설명, 후보, 정답, 기대하는 top1_6/top3_6)
    cases = [
        ("1순위 정답",       ["3207.20", "320890", "382499"], "3207209000", (True, True)),
        ("2순위 정답",       ["320890", "320720", "382499"],  "3207209000", (False, True)),
        ("전부 오답",        ["320890", "382499", "320990"],  "3207209000", (False, False)),
        ("4순위는 안 본다",  ["320890", "382499", "320990", "320720"], "3207209000", (False, False)),
    ]
    graded = []
    for label, cands, ans, expect in cases:
        g = grade(cands, ans)
        got = (g["top1_6"], g["top3_6"])
        mark = "OK" if got == expect else "!! 기대와 다름"
        print(f"  {label:16} top1_6={g['top1_6']!s:5} top3_6={g['top3_6']!s:5} {mark}")
        graded.append(g)

    print()
    print("--- summarize 확인 ---")
    print(" ", summarize(graded))
