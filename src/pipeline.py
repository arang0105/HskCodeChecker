"""[1]~[4] 단계 파이프라인.

지금은 [3] 재정렬만 있다. 나머지 단계는 필요해질 때 여기에 붙인다.

[3]이 하는 일은 하나다 — 1차 후보 3개의 **순위만** 바꾼다.
새 코드를 만들지 않는 이유는 CLAUDE.md의 설계 근거에 있다.
후보 안에 정답이 들어 있는 비율이 이미 89.7%이므로, 회수할 게 남아 있는 건
'생성'이 아니라 '순서'다. 후보 밖 코드를 허용하면 개선이 재정렬 덕인지
후보 확장 덕인지 구분할 수 없게 된다.
"""

import csv
import hashlib
import json
from datetime import datetime

from src import config, evaluate, hsk, llm, search

# [1] 후보 생성 결과를 담아 두는 파일.
# 재정렬 프롬프트를 고쳐가며 실험할 때, [1]단계는 매번 똑같은 것을 다시 사는 셈이다.
# 캐시하면 실험 1회 비용이 절반이 되고, [1]이 고정되므로 재정렬 효과만 깨끗하게 남는다.
CACHE_CSV = config.DATA_DIR / "후보_캐시.csv"
CACHE_FIELDS = ["key", "모델", "생성시각", "후보json", "in_tokens", "billed_out",
                "elapsed", "입력앞60"]

# 결정례 원문은 길다(물품설명 최대 3,137자, 결정사유 최대 2,505자).
# 5건을 통째로 넣으면 프롬프트가 1만 자를 넘어 비용도 커지고,
# 정작 봐야 할 물품설명이 긴 인용문에 묻힌다. 그래서 잘라서 넣는다.
DESC_CUT = 400
REASON_CUT = 600


RERANK_PROMPT = """당신은 관세 품목분류 전문가입니다.

아래 물품에 대해 1차로 뽑은 HS 6자리 후보 3개와, 유사한 과거 결정례가 있습니다.
결정례를 근거로 후보 3개의 순위를 다시 판단하세요.

[물품설명]
{desc}

[1차 후보]
{candidates}

[유사 결정례]
{cases}

규칙
- 후보 3개를 모두 쓰고 순서만 바꿉니다. 후보에 없는 코드를 새로 만들지 마세요.
- 결정례의 결정세번이 후보 어디에도 없다면, 그 결정례는 근거로만 쓰고 답에는 넣지 마세요.
- 결정례가 이 물품과 무관하면 무시하세요. 억지로 끼워 맞추지 마세요.
- 순위를 바꿀 이유가 없으면 1차 순서를 그대로 두세요.

반드시 다음 JSON 형식으로만 답하세요.
{{
  "ranked": [
    {{"code": "6자리 숫자", "reason": "한 문장 근거", "근거결정례": "참조번호 또는 없음"}},
    {{"code": "6자리 숫자", "reason": "한 문장 근거", "근거결정례": "참조번호 또는 없음"}},
    {{"code": "6자리 숫자", "reason": "한 문장 근거", "근거결정례": "참조번호 또는 없음"}}
  ],
  "confidence": "high 또는 medium 또는 low",
  "check_points": ["사람이 직접 확인해야 할 점", "..."]
}}"""


def _cache_key(desc, model):
    """모델 · 프롬프트 · 입력이 모두 같을 때만 같은 키가 나오게 만든다.

    셋을 이어붙인 문자열을 해시(sha256)한다. 해시는 내용이 1글자만 달라도
    전혀 다른 값이 나오는 함수다. Java의 hashCode 와 쓰임새는 같지만
    충돌 가능성이 사실상 없다는 점이 다르다.

    **프롬프트를 키에 넣는 게 핵심이다.** 프롬프트를 고치면 키가 저절로
    달라져 캐시가 무효가 된다. 이게 없으면 옛 프롬프트로 만든 후보를
    새 프롬프트의 결과인 양 쓰게 되고, 그 순간 측정이 거짓말이 된다.
    """
    재료 = f"{model}\n{llm.CANDIDATE_PROMPT}\n{desc}"
    # encode("utf-8") : 해시 함수는 글자가 아니라 바이트를 먹는다
    return hashlib.sha256(재료.encode("utf-8")).hexdigest()[:16]


def load_candidate_cache():
    """캐시 파일을 {key: 행} 으로 읽는다. 없으면 빈 dict."""
    if not CACHE_CSV.exists():
        return {}
    with open(CACHE_CSV, encoding="utf-8-sig", newline="") as f:
        return {row["key"]: row for row in csv.DictReader(f)}


def _append_cache(row):
    """캐시에 한 줄 덧붙인다. 파일 전체를 다시 쓰지 않는다."""
    is_new = not CACHE_CSV.exists()
    with open(CACHE_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def generate_candidates(desc, model=None, use_cache=True, cache=None):
    """[1] 후보 3개를 만든다. 같은 조건이면 캐시에서 꺼낸다.

    use_cache : False 면 캐시를 **읽지도 쓰지도** 않는다. 매번 새로 부른다.
                일관성 측정처럼 실행 간 변동 자체를 재야 할 때 쓴다.
    cache : load_candidate_cache() 결과를 미리 읽어 넘기면 파일을 반복해
            읽지 않는다. 29건 배치에서 29번 읽을 이유가 없다.

    돌려주는 dict 의 cached 가 True 면 API를 부르지 않은 것이고,
    그때 토큰과 시간은 실제로 0이다.
    """
    model_name = model or config.MODEL_DEV
    key = _cache_key(desc, model_name)

    # cache is not None 으로 검사하는 이유 — 빈 dict {} 는 거짓으로 취급되므로
    # if cache: 라고 쓰면 "캐시가 비었다"와 "캐시를 안 넘겼다"를 구분하지 못한다.
    표 = cache if cache is not None else load_candidate_cache()

    hit = 표.get(key) if use_cache else None
    if hit:
        return {
            "candidates": json.loads(hit["후보json"]),
            "cached": True,
            "elapsed": 0.0, "in_tokens": 0, "billed_out": 0,
            "parse_error": None,
        }

    result = llm.ask_json(llm.CANDIDATE_PROMPT.format(desc=desc), model=model_name)
    candidates = result["data"]["candidates"] if result["data"] else []

    # 저장하지 않는 경우가 둘 있다.
    #
    # 1) 파싱에 실패해 후보가 빈 경우 — 실패를 저장해 두면 다시 돌려도
    #    영원히 실패한 채로 남는다.
    # 2) use_cache=False — '캐시를 아예 쓰지 않는다'는 뜻이므로 읽지도 쓰지도
    #    않는다. 일관성 측정이 이 모드로 돈다. 3회 반복분이 캐시에 쌓이면
    #    나중에 캐시를 켰을 때 마지막 회차 값이 조용히 쓰이게 된다.
    if candidates and use_cache:
        row = {
            "key": key,
            "모델": model_name,
            "생성시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # ensure_ascii=False : 한글을 \uXXXX 로 바꾸지 않고 그대로 저장
            "후보json": json.dumps(candidates, ensure_ascii=False),
            "in_tokens": result["in_tokens"],
            "billed_out": result["billed_out"],
            "elapsed": round(result["elapsed"], 1),
            "입력앞60": desc[:60],  # 사람이 캐시 파일을 열어봤을 때 알아보라고
        }
        _append_cache(row)
        표[key] = row  # 같은 실행 안에서 두 번 부르지 않도록 메모리에도 반영

    return {
        "candidates": candidates,
        "cached": False,
        "elapsed": result["elapsed"],
        "in_tokens": result["in_tokens"],
        "billed_out": result["billed_out"],
        "parse_error": result["parse_error"],
    }


def _format_candidates(candidates):
    """1차 후보를 프롬프트에 넣을 문자열로 만든다.

    candidates 는 {"code","reason"} dict 의 리스트다.
    enumerate(x, start=1) 는 (1, 첫원소), (2, 두번째)... 를 돌려준다.
    Java 로 치면 인덱스를 직접 세는 for 문을 대신하는 관용구다.
    """
    lines = []
    for rank, c in enumerate(candidates, start=1):
        lines.append(f"{rank}. {c['code']} — {c.get('reason', '')}")
    return "\n".join(lines)


def _format_cases(hits):
    """검색된 결정례를 프롬프트에 넣을 문자열로 만든다.

    유사도 점수를 함께 보여 준다. 모델이 '이건 좀 먼 사례구나'를
    판단할 근거가 되기 때문이다.
    """
    if not hits:
        return "(유사한 결정례를 찾지 못했습니다)"

    blocks = []
    for rank, h in enumerate(hits, start=1):
        blocks.append(
            f"({rank}) 유사도 {h['score']:.3f} | 참조번호 {h['참조번호']} | "
            f"결정세번 {h['결정세번']}\n"
            f"    품명: {h['품명']}\n"
            f"    물품설명: {h['물품설명'][:DESC_CUT]}\n"
            f"    결정사유: {h['결정사유'][:REASON_CUT]}"
        )
    return "\n\n".join(blocks)


def rerank(desc, candidates, hits, model=None, temperature=1.0):
    """1차 후보 3개를 결정례를 근거로 재정렬한다.

    desc       : 물품설명 (평가셋 조건 A 입력)
    candidates : [1]단계가 준 [{"code","reason"}, ...]
    hits       : search.search() 가 준 결정례 리스트

    돌려주는 dict 는 llm.ask_json() 결과에 다음을 더한 것이다.
      codes  : 재정렬된 6자리 코드 리스트 (채점은 이것만 본다)
      위반    : 후보에 없는 코드를 만들었거나 개수가 안 맞으면 사유 문자열

    '위반'을 예외로 터뜨리지 않는 이유 — 29건 배치가 한 건 때문에
    멈추면 안 되고, 규칙을 얼마나 어기는지도 측정 대상이기 때문이다.
    """
    prompt = RERANK_PROMPT.format(
        desc=desc,
        candidates=_format_candidates(candidates),
        cases=_format_cases(hits),
    )

    result = llm.ask_json(prompt, model=model, temperature=temperature)
    result["prompt"] = prompt

    if not result["data"]:
        result["codes"] = []
        result["위반"] = "파싱 실패"
        return result

    ranked = result["data"].get("ranked", [])
    codes = [str(c.get("code", "")) for c in ranked]
    result["codes"] = codes

    # 규칙을 지켰는지 검사한다. set 은 순서를 무시하고 원소만 비교한다.
    #
    # 반드시 정규화한 뒤에 비교해야 한다. 모델은 '3210.00' 과 '321000' 을
    # 섞어 쓰기 때문에, 날 문자열로 비교하면 같은 코드를
    # '후보 밖 코드 생성' 으로 오탐한다. 채점(grade)이 정규화를 거치는 것과
    # 같은 이유다.
    원본 = {evaluate.normalize_code(c["code"]) for c in candidates}
    정규 = [evaluate.normalize_code(c) for c in codes]
    새코드 = [c for c in 정규 if c not in 원본]

    if 새코드:
        result["위반"] = f"후보 밖 코드 생성: {새코드}"
    elif len(codes) != len(candidates):
        result["위반"] = f"개수 불일치: {len(candidates)}개 → {len(codes)}개"
    elif set(정규) != 원본:
        result["위반"] = f"후보 누락: {sorted(원본 - set(정규))}"
    else:
        result["위반"] = None

    return result


FINALIZE_PROMPT = """당신은 관세 품목분류 전문가입니다.

아래 물품의 HS 6자리는 {hs6} 으로 확정되었습니다.
그 아래 신고 가능한 10자리 세번 중 하나를 고르세요.

[물품설명]
{desc}

[{hs6} 이 속한 계층]
{parent}

[선택지 — 이 안에서만 고릅니다]
{options}

규칙
- 선택지에 있는 10자리 코드 하나를 그대로 고릅니다. 새로 만들거나 고치지 마세요.
- 선택지를 위에서부터 하나씩 물품에 대보고, 어느 것에도 해당하지 않을 때 '기타'를 고릅니다.
- 물품설명에 판단 근거가 없으면 confidence 를 low 로 하고, 무엇을 더 알아야
  하는지 check_points 에 적으세요. 근거 없이 그럴듯한 쪽을 고르지 마세요.

반드시 다음 JSON 형식으로만 답하세요.
{{
  "code": "10자리 숫자",
  "reason": "한 문장 근거",
  "confidence": "high 또는 medium 또는 low",
  "check_points": ["사람이 직접 확인해야 할 점", "..."]
}}"""


def _format_options(rows):
    """선택지를 (공통 상위설명, 줄 목록) 두 조각으로 나눠 만든다.

    같은 6자리 밑이면 상위설명이 대개 같지만, 국내 중간 마디(7·8·9단위)가
    있는 6자리는 행마다 갈린다(평가셋 29건 중 8건, 전체 5,613종 중 604종).
    그래서 첫 행만 헤더로 쓰면 그 8건에서 정보가 사라진다.
    **모든 행이 공유하는 앞부분만 헤더로 빼고, 갈리는 꼬리는 각 줄에 붙인다.**
    같은 긴 문장을 후보 수만큼 반복해 넣지 않으면서 손실도 없다.
    """
    부분 = [r["상위설명"].split(" > ") for r in rows]

    # zip(*부분) : * 는 리스트를 인자 여러 개로 펼치는 것이다(Java 의 varargs 전달과 비슷).
    # 그러면 zip 이 '각 행의 1번째끼리, 2번째끼리...' 를 묶어 준다.
    # 조각 개수가 행마다 달라도 zip 은 가장 짧은 것에서 멈추므로 안전하다.
    공통 = []
    for 자리 in zip(*부분):
        if len(set(자리)) != 1:   # 한 자리라도 갈리면 거기서 공통은 끝이다
            break
        공통.append(자리[0])

    n = len(공통)
    lines = []
    for r, p in zip(rows, 부분):
        꼬리 = " > ".join(p[n:])
        lines.append(f"- {r['hs10']}  {r['품목명']}" + (f"   [{꼬리}]" if 꼬리 else ""))

    return " > ".join(공통) or "(상위 설명 없음)", "\n".join(lines)


def finalize(desc, hs6, table=None, model=None, temperature=1.0):
    """[4] 확정된 6자리 아래에서 10자리 하나를 고른다.

    desc  : 물품설명
    hs6   : [3] 재정렬이 1순위로 놓은 6자리
    table : hsk.load_hsk() 결과. 배치에서 CSV 를 건마다 읽지 않으려고 넘긴다

    돌려주는 dict
      code    : 확정 10자리 (실패하면 빈 문자열)
      선택지수 : 후보가 몇 개였는가
      auto    : True 면 API 를 부르지 않고 결정했다
      위반    : 목록 밖 코드를 골랐으면 사유 문자열

    **선택지가 1개면 API 를 부르지 않는다.** 평가셋 29건 중 8건이 여기 해당한다.
    고를 게 하나뿐인데 모델에게 묻는 건 돈과 시간만 쓰는 일이고,
    '모델이 잘 골랐다'는 착시도 만든다.
    """
    표 = table if table is not None else hsk.load_hsk()
    key = evaluate.normalize_code(hs6)[:6]
    rows = hsk.codes_under(key, 표)

    빈결과 = {"code": "", "선택지수": len(rows), "auto": True, "위반": None,
              "elapsed": 0.0, "in_tokens": 0, "billed_out": 0,
              "parse_error": None, "data": None, "prompt": "", "text": ""}

    if not rows:
        빈결과["위반"] = f"목록에 없는 6자리: {key}"
        return 빈결과

    if len(rows) == 1:
        빈결과["code"] = rows[0]["hs10"]
        return 빈결과

    parent, options = _format_options(rows)
    prompt = FINALIZE_PROMPT.format(hs6=key, desc=desc, parent=parent, options=options)

    result = llm.ask_json(prompt, model=model, temperature=temperature)
    result["prompt"] = prompt
    result["선택지수"] = len(rows)
    result["auto"] = False

    if not result["data"]:
        result["code"] = ""
        result["위반"] = "파싱 실패"
        return result

    code = evaluate.normalize_code(result["data"].get("code", ""))
    result["code"] = code
    허용 = {r["hs10"] for r in rows}
    result["위반"] = None if code in 허용 else f"목록 밖 코드 생성: {code}"
    return result


if __name__ == "__main__":
    from src import evaluate

    case = evaluate.load_cases("A")[0]
    desc = case["입력"]

    print(f"정답 6자리: {case['정답'][:6]}\n")

    # [1] 후보 생성
    first = llm.ask_json(llm.CANDIDATE_PROMPT.format(desc=desc))
    candidates = first["data"]["candidates"]
    print("[1] 1차 후보:", [c["code"] for c in candidates])

    # [2] 결정례 검색
    hits = search.search(desc, top_k=5)
    print("[2] 검색 결정례:", [h["결정세번"][:6] for h in hits])

    # [3] 재정렬
    result = rerank(desc, candidates, hits)
    print("[3] 재정렬 후:", result["codes"])
    print("    확신도:", result["data"]["confidence"] if result["data"] else "-")
    print("    위반:", result["위반"] or "없음")
    print()
    for r in result["data"]["ranked"]:
        print(f"  {r['code']}  ({r.get('근거결정례', '-')})  {r['reason']}")
