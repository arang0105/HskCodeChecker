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
CACHE_FIELDS = ["key", "모델", "온도", "생성시각", "후보json", "in_tokens", "billed_out",
                "elapsed", "입력앞60"]

# 결정례 원문은 길다(물품설명 최대 3,137자, 결정사유 최대 2,505자).
# 5건을 통째로 넣으면 프롬프트가 1만 자를 넘어 비용도 커지고,
# 정작 봐야 할 물품설명이 긴 인용문에 묻힌다. 그래서 잘라서 넣는다.
DESC_CUT = 400
REASON_CUT = 600


# ---------------------------------------------------------------- [0-a] 카탈로그
#
# **이 단계는 파이프라인 앞에 붙는다. [0]~[4] 는 손대지 않는다.**
# 카탈로그를 읽어 '물품설명 초안'을 만들어 주기만 하고, 그 뒤는 사람이 고친
# 텍스트가 지금까지와 똑같은 길을 간다. 그래야 봉인을 열어 얻은 숫자
# (6자리 56.7% / 10자리 50.0%)가 그대로 유효하다. 파이프라인 안으로
# 이미지를 넣으면 그 숫자가 전부 무효가 되고 다시 잴 방법이 없다.
#
# 받을 수 있는 파일. 상한을 두는 이유는 비용이 아니라 품질이다 —
# 30쪽짜리 종합 카탈로그를 통째로 주면 제품이 수십 개라 초안이 뭉개진다.
# (실측: 3쪽 5.3MB PDF 가 입력 1,659 토큰. 용량이 아니라 쪽수로 계산된다)
CATALOG_MIME = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
CATALOG_MAX_MB = 10
CATALOG_MAX_PRODUCTS = 10

CATALOG_PROMPT = """당신은 관세 품목분류를 준비하는 사람을 돕고 있습니다.

첨부한 카탈로그(제품 안내서·사양표·제품 사진)를 읽고, 그 안의 제품마다
**HS 품목분류에 쓸 물품설명 초안**을 작성하세요. 분류는 하지 마세요.
HS 코드를 추측해서 적지 마세요.

물품설명에 담을 것 — 이 순서로 한 문단으로 씁니다.
1. 물품의 종류 (무엇인가)
2. 재질·성분
3. 용도·기능
4. 규격·형태 (치수, 용량, 정격 등)

**가장 중요한 규칙 — 카탈로그에 없는 정보를 지어내지 마세요.**
재질이 안 적혀 있으면 재질을 쓰지 마세요. "플라스틱으로 보입니다" 같은
추측도 쓰지 마세요. 대신 그 항목을 "빠진정보" 배열에 넣으세요.
지어낸 재질 하나가 분류 결과를 통째로 바꿉니다.

홍보 문구(감성적인 카피, 사용 후기)는 물품설명에 넣지 마세요.
분류에 쓸 수 있는 사실만 남기세요.

같은 제품의 모델 변형(색상·용량만 다른 것)은 **하나로 묶고** 모델명을
규격에 나열하세요. 서로 다른 물품일 때만 따로 나누세요.
제품이 {최대}개를 넘으면 대표적인 것 {최대}개만 쓰세요.

반드시 다음 JSON 형식으로만 답하세요.
{{
  "제품들": [
    {{
      "이름": "카탈로그에 적힌 제품명",
      "물품설명": "위 1~4를 담은 한 문단",
      "빠진정보": ["재질", "용량"]
    }}
  ],
  "읽기실패": false
}}
카탈로그를 전혀 읽을 수 없으면 제품들을 빈 배열 [] 로 두고 읽기실패를 true 로 하세요."""


def catalog_extract(파일들, model=None, temperature=1.0):
    """[0-a] 카탈로그 파일에서 물품설명 초안을 뽑는다.

    파일들 : [(bytes, mime_type), ...]  — 한 번에 여러 장을 보낼 수 있다.
             제품 사진 앞뒤 두 장처럼 한 제품이 여러 파일에 걸칠 수 있어서다.

    **호출은 1회다.** 제품 목록과 각 제품의 초안을 한꺼번에 받는다.
    "목록 먼저 받고 → 고른 뒤 → 초안 받기" 로 나누면 파일을 두 번 보내야 하는데,
    이미지·PDF 는 입력 토큰을 많이 먹으므로 그게 더 비싸다.

    돌려주는 dict 는 llm.ask_json() 결과에 다음을 더한 것이다.
      제품들   : [{"이름", "물품설명", "빠진정보"}, ...]
      읽기실패 : True 면 파일을 못 읽었다

    **gate() 와 달리 fail-open 하지 않는다.** 게이트는 고장 나도 분류를
    진행시키는 게 맞지만(안전장치이지 관문이 아니다), 이쪽은 실패하면
    초안이 없는 것이므로 사용자가 직접 타이핑하면 된다. 빈 결과를 돌려주고
    화면에서 안내하는 게 맞다.
    """
    parts = [llm.file_part(내용, mime) for 내용, mime in 파일들]
    prompt = CATALOG_PROMPT.format(최대=CATALOG_MAX_PRODUCTS)

    result = llm.ask_json(prompt, model=model, temperature=temperature,
                          parts=parts)
    result["prompt"] = prompt

    data = result["data"]
    if not data:
        result["제품들"] = []
        result["읽기실패"] = True
        return result

    # 모델이 키를 빠뜨리거나 형식을 어길 수 있다. 여기서 걸러 두면
    # 부르는 쪽(app.py)은 항상 같은 모양을 받는다.
    제품들 = []
    for p in (data.get("제품들") or [])[:CATALOG_MAX_PRODUCTS]:
        if not isinstance(p, dict):
            continue
        설명 = (p.get("물품설명") or "").strip()
        if not 설명:
            continue  # 설명이 없으면 초안으로 쓸 수 없다
        제품들.append({
            "이름": (p.get("이름") or "이름 없음").strip(),
            "물품설명": 설명,
            "빠진정보": p.get("빠진정보") or [],
        })

    result["제품들"] = 제품들
    result["읽기실패"] = bool(data.get("읽기실패", False)) or not 제품들
    return result


GATE_PROMPT = """당신은 관세 품목분류 전문가입니다.

아래 입력을 보고 **HS 품목분류를 시도할 수 있는지**만 판단하세요. 분류하지는 마세요.

[입력]
{desc}

판단 기준 — 이것 하나만 봅니다.
**입력만 읽고 이 물품이 무엇인지 특정할 수 있는가?**

- 특정할 수 있으면 "충분"입니다. 재질·용도 같은 세부 정보가 더 있으면 좋겠다는
  이유로 불충분이라고 하지 마세요. **거의 모든 물품이 그렇습니다.** 그런 항목은
  분류한 뒤에 사람이 확인할 사항으로 넘기면 됩니다.
- 품번·모델명·상표명·약어뿐이어서 **물품의 정체를 짐작조차 할 수 없을 때만**
  "불충분"입니다.

예시
- "AX-2200" → 불충분 (기호뿐이라 무엇인지 알 수 없다)
- "부품 A형; SKU 88213; CN" → 불충분 (품번 위주, 정체 불명)
- "스테인리스 보온병" → 충분
- "벽걸이 에어컨 (모델 SW07BAKWAS)" → 충분 (모델명이 있어도 물품은 특정된다)
- "면 티셔츠" → 충분 (편물인지 여부는 분류 뒤에 확인할 사항이다)

반드시 다음 JSON 형식으로만 답하세요.
{{
  "충분": true 또는 false,
  "부족항목": ["재질", "용도"],
  "질문": ["사용자에게 물어볼 한 문장", "..."]
}}
충분하면 부족항목과 질문은 빈 배열 [] 로 두세요."""


def gate(desc, model=None, temperature=1.0):
    """[0] 분류에 착수하기 전에 정보가 충분한지만 판단한다.

    baseline 에서 가장 뼈아픈 발견이 **정보부족 인지율 3.6%**(28건 중 1건)였다.
    품번만 던져도 모델은 되묻지 않고 확신에 차서 코드를 찍는다.
    그리고 조건 A(상세 설명)와 조건 B(품명 원문) 사이에 정확도 차이가
    관측되지 않았으므로, 이 단계의 목적은 **정확도가 아니라 안전성**이다.
    (CLAUDE.md "조건 A vs 조건 B" 참조)

    돌려주는 dict 는 llm.ask_json() 결과에 다음을 더한 것이다.
      충분     : True 면 분류를 진행해도 된다
      부족항목 : 무엇이 비었는가
      질문     : 사용자에게 되물을 문장

    **파싱에 실패하면 충분=True 로 둔다(fail-open).** 게이트가 깨졌다고
    분류까지 막으면 도구가 아무 답도 못 하는 상태가 된다. 게이트는 안전장치이지
    관문이 아니다. 대신 parse_error 가 남으므로 집계에서 드러난다.
    """
    prompt = GATE_PROMPT.format(desc=desc)
    result = llm.ask_json(prompt, model=model, temperature=temperature)
    result["prompt"] = prompt

    data = result["data"]
    if not data:
        result["충분"] = True
        result["부족항목"] = []
        result["질문"] = []
        return result

    # .get(키, 기본값) 은 키가 없어도 터지지 않는다. 모델이 키를 빠뜨릴 수 있으므로
    # dict[키] 대신 이걸 쓴다. Java 의 map.getOrDefault 와 같다.
    result["충분"] = bool(data.get("충분", True))
    result["부족항목"] = data.get("부족항목", []) or []
    result["질문"] = data.get("질문", []) or []
    return result


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


def _cache_key(desc, model, temperature):
    """모델 · 프롬프트 · 온도 · 입력이 모두 같을 때만 같은 키가 나오게 만든다.

    셋을 이어붙인 문자열을 해시(sha256)한다. 해시는 내용이 1글자만 달라도
    전혀 다른 값이 나오는 함수다. Java의 hashCode 와 쓰임새는 같지만
    충돌 가능성이 사실상 없다는 점이 다르다.

    **프롬프트와 온도를 키에 넣는 게 핵심이다.** 둘 중 하나만 고쳐도 키가
    저절로 달라져 캐시가 무효가 된다. 이게 없으면 옛 조건으로 만든 후보를
    새 조건의 결과인 양 쓰게 되고, 그 순간 측정이 거짓말이 된다.

    온도는 2026-08-20에 키에 넣었다. 그전까지 [1]단계는 llm.ask_json 기본값
    1.0으로만 돌았고 온도를 받을 방법 자체가 없었다. 그대로 온도만 낮춰
    재측정했다면 [3][4]만 낮은 온도로 돌고 [1]은 1.0짜리 후보가 캐시에서
    나왔을 것이다.
    """
    재료 = f"{model}\n{temperature}\n{llm.CANDIDATE_PROMPT}\n{desc}"
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


def generate_candidates(desc, model=None, use_cache=True, cache=None,
                        temperature=1.0):
    """[1] 후보 3개를 만든다. 같은 조건이면 캐시에서 꺼낸다.

    use_cache : False 면 캐시를 **읽지도 쓰지도** 않는다. 매번 새로 부른다.
                일관성 측정처럼 실행 간 변동 자체를 재야 할 때 쓴다.
    cache : load_candidate_cache() 결과를 미리 읽어 넘기면 파일을 반복해
            읽지 않는다. 29건 배치에서 29번 읽을 이유가 없다.

    돌려주는 dict 의 cached 가 True 면 API를 부르지 않은 것이고,
    그때 토큰과 시간은 실제로 0이다.
    """
    model_name = model or config.MODEL_DEV
    key = _cache_key(desc, model_name, temperature)

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

    result = llm.ask_json(llm.CANDIDATE_PROMPT.format(desc=desc), model=model_name,
                          temperature=temperature)
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
            "온도": temperature,
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


def _format_cases(hits, reason_cut=None):
    """검색된 결정례를 프롬프트에 넣을 문자열로 만든다.

    유사도 점수를 함께 보여 준다. 모델이 '이건 좀 먼 사례구나'를
    판단할 근거가 되기 때문이다.

    reason_cut 은 결정사유를 몇 글자까지 넣을지다. None 이면 모듈 기본값
    REASON_CUT(600). 0 을 주면 결정사유를 통째로 뺀다 — 기여도를 재기 위한
    ablation(한 부분만 빼고 같은 실험을 다시 돌려 그 부분의 몫을 보는 방법)용이다.
    상수를 직접 고치지 않고 인자로 받는 이유는, 같은 실행 안에서 두 조건을
    비교해야 모델의 실행 간 변동이 섞이지 않기 때문이다.
    """
    cut = REASON_CUT if reason_cut is None else reason_cut
    if not hits:
        return "(유사한 결정례를 찾지 못했습니다)"

    blocks = []
    for rank, h in enumerate(hits, start=1):
        blocks.append(
            f"({rank}) 유사도 {h['score']:.3f} | 참조번호 {h['참조번호']} | "
            f"결정세번 {h['결정세번']}\n"
            f"    품명: {h['품명']}\n"
            f"    물품설명: {h['물품설명'][:DESC_CUT]}"
            + (f"\n    결정사유: {h['결정사유'][:cut]}" if cut else "")
        )
    return "\n\n".join(blocks)


def rerank(desc, candidates, hits, model=None, temperature=1.0,
           reason_cut=None):
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
        cases=_format_cases(hits, reason_cut=reason_cut),
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
