"""결정례 유사 검색 — 임베딩 + 코사인 유사도.

임베딩이란 문장을 숫자 배열(벡터)로 바꾸는 것이다.
뜻이 비슷한 문장은 비슷한 방향의 벡터가 되므로, 방향이 얼마나 겹치는지를
재면 "비슷한 정도"가 나온다. 그 각도 비교가 코사인 유사도다.

벡터DB는 쓰지 않는다. 수백 건 규모에서는 numpy 배열 하나로 충분하다.
"""

import google.generativeai as genai
import numpy as np
import pandas as pd

from src import config

genai.configure(api_key=config.GEMINI_API_KEY)

CORPUS_CSV = config.DATA_DIR / "결정례.csv"
INDEX_META = config.DATA_DIR / "embeddings_meta.csv"

# 한 번에 보낼 건수. 너무 크면 요청이 무거워지고 실패 시 손해가 크다.
BATCH = 50

# 임베딩 차원. 모델은 3072차원을 주지만 앞쪽으로 잘라 쓸 수 있다.
# 파일 크기와 검색 속도를 좌우하므로 7주차 배포까지 고려해 정한다.
DIM = 768


def index_path(dim=None):
    """차원마다 다른 파일에 저장한다. 섞이면 검색이 조용히 틀린다."""
    return config.DATA_DIR / f"embeddings_{dim or DIM}.npy"


def embed(texts, task_type, dim=None):
    """문장 리스트를 벡터 배열로 바꾼다.

    task_type 은 용도를 알려주는 값이다. 코퍼스를 담을 때와 질문할 때
    서로 다른 값을 쓰면 검색 정확도가 올라간다.
      retrieval_document : 검색 대상(결정례)
      retrieval_query    : 찾는 쪽(물품설명 질의)

    반환값은 '길이 1로 맞춘(정규화한)' 벡터들이다.
    이렇게 해두면 코사인 유사도가 그냥 내적 한 번으로 끝난다.
    """
    vectors = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        result = genai.embed_content(
            model=config.MODEL_EMBED, content=chunk, task_type=task_type,
            output_dimensionality=dim or DIM,
        )
        vectors.extend(result["embedding"])
        print(f"    임베딩 {min(i + BATCH, len(texts))}/{len(texts)}")

    arr = np.array(vectors, dtype=np.float32)
    # 각 행의 길이(L2 노름)로 나눠 길이를 1로 만든다.
    # keepdims=True 는 나눗셈 모양을 맞추기 위한 것이다.
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def _doc_text(row):
    """검색 대상으로 삼을 문장을 만든다.

    품명과 물품설명만 쓴다. 결정사유는 법령 인용이라 물품 자체보다
    분류 논리를 설명하는 글이어서, 물품이 비슷한 사례를 찾는 데는 오히려 방해가 된다.
    (나중에 재정렬 단계에서 근거로 따로 넣는다)
    """
    return f"{row['품명']}\n{row['물품설명']}"


def build_index(dim=None):
    """코퍼스를 임베딩해 .npy 파일로 저장한다. 한 번만 돌리면 된다."""
    df = pd.read_csv(CORPUS_CSV, dtype=str).fillna("")
    texts = [_doc_text(row) for _, row in df.iterrows()]

    print(f"코퍼스 {len(texts)}건 임베딩 ({config.MODEL_EMBED}, {dim or DIM}차원)")
    vectors = embed(texts, "retrieval_document", dim=dim)

    path = index_path(dim)
    np.save(path, vectors)
    df.to_csv(INDEX_META, index=False, encoding="utf-8-sig")
    mb = path.stat().st_size / 1024 / 1024
    print(f"저장 완료: {vectors.shape} → {path.name} ({mb:.1f}MB)")
    return vectors


def load_index(dim=None):
    """저장해 둔 벡터와 원본 표를 읽는다."""
    path = index_path(dim)
    if not path.exists():
        raise FileNotFoundError(f"먼저 build_index({dim or DIM}) 를 실행하세요.")
    return np.load(path), pd.read_csv(INDEX_META, dtype=str).fillna("")


def search(query, top_k=5, include_eval=False, index=None, dim=None):
    """질의문과 비슷한 결정례를 상위 top_k 건 돌려준다.

    include_eval=False 가 기본이다. 평가셋 29건은 검색 대상에서 빼고 찾는다.
    이걸 켜는 건 '누수가 있으면 숫자가 얼마나 부풀려지는가'를 측정할 때뿐이다.

    **회귀 세트 20건은 include_eval 과 무관하게 항상 뺀다.** 자기 결정례가
    검색되면 정답을 그대로 베끼게 되어, 회귀 세트가 재려던 것('쉬운 건이
    안 깨지는가')이 아니라 누수를 재게 된다. include_eval 이 평가셋에만
    걸리는 이유는 그 스위치의 용도가 누수 격차 측정 하나뿐이기 때문이다.
    """
    vectors, meta = index if index else load_index(dim)

    # mask 는 True/False 배열이고, 이걸로 행을 골라낸다.
    # & 는 두 불리언 배열을 원소별로 AND 한다. 파이썬 and 는 배열에 못 쓴다.
    회귀제외 = (meta["회귀셋"] == "N").values
    mask = 회귀제외 if include_eval else 회귀제외 & (meta["평가셋포함"] == "N").values
    vectors, meta = vectors[mask], meta[mask].reset_index(drop=True)

    qvec = embed([query], "retrieval_query", dim=dim)[0]

    # 둘 다 길이 1이므로 내적이 곧 코사인 유사도다.
    scores = vectors @ qvec

    # argsort 는 작은 순 인덱스를 준다. [::-1] 로 뒤집어 큰 순으로 만든다.
    order = np.argsort(scores)[::-1][:top_k]

    return [
        {
            "score": float(scores[i]),
            "참조번호": meta.iloc[i]["참조번호"],
            "결정세번": meta.iloc[i]["결정세번_정규화"],
            "품명": meta.iloc[i]["품명"],
            "물품설명": meta.iloc[i]["물품설명"],
            "결정사유": meta.iloc[i]["결정사유"],
        }
        for i in order
    ]


def measure_coverage(top_ks=(1, 3, 5, 10, 20), main_k=5, condition="A", dim=None):
    """평가셋 29건에 대해 검색이 정답을 물어오는지 측정한다.

    두 가지를 반드시 나눠서 본다.
      존재 : 정답 6자리가 코퍼스에 아예 들어 있는가   → 커버리지 문제
      회수 : 들어 있는 것을 top-k 안에 실제로 물어왔는가 → 검색 정밀도 문제

    이 둘을 합쳐 보면 '검색이 나쁘다'와 '자료가 없다'를 구분할 수 없다.
    452건 코퍼스에서 k를 50까지 늘려도 회수가 안 오른 것이
    커버리지 문제였다는 증거였고, 그래서 품목분류사례를 추가 수집했다.

    누수 조건(평가셋 포함)도 함께 잰다. CLAUDE.md 홀드아웃 규칙이
    "포함 / 제외 두 조건 모두 측정"을 요구하기 때문이다.
    """
    from src import evaluate

    vectors, meta = load_index(dim)
    cases = evaluate.load_cases(condition)

    # 코퍼스 각 행의 6자리 코드. .values 는 pandas Series 를 numpy 배열로 바꾼다.
    codes6 = meta["결정세번_정규화"].str[:6].values
    is_eval = (meta["평가셋포함"] == "Y").values
    is_reg = (meta["회귀셋"] == "Y").values
    # ~ 는 numpy 불리언 배열의 원소별 부정이다. 파이썬 not 은 배열에 못 쓴다.
    # 회귀 세트는 '포함' 조건에서도 뺀다. 누수 격차는 평가셋만의 이야기다.
    정직 = ~is_eval & ~is_reg

    존재6 = set(codes6[정직])
    존재4 = {c[:4] for c in 존재6}

    # baseline 에서 재정렬 사정권(Top1 오답 + Top3 정답)을 뽑는다.
    base = evaluate.load_baseline()
    사정권 = {no for no, b in base.items() if not b["top1_6"] and b["top3_6"]}

    # 질의 29건을 한 번에 임베딩한다. 건마다 부르면 요청이 29번 나간다.
    qvecs = embed([c["입력"] for c in cases], "retrieval_query", dim=dim)

    maxk = max(top_ks)
    조건들 = (("제외", 정직), ("포함", ~is_reg))

    rows = []
    for case, qvec in zip(cases, qvecs):  # zip 은 두 리스트를 나란히 묶는다
        ans6 = case["정답"][:6]
        row = {
            "no": str(case["no"]),
            "정답6": ans6,
            "존재6": ans6 in 존재6,
            "존재4": ans6[:4] in 존재4,
            "사정권": str(case["no"]) in 사정권,
        }
        for label, mask in 조건들:
            order = np.argsort(vectors[mask] @ qvec)[::-1][:maxk]
            hit6 = codes6[mask][order]
            for k in top_ks:
                row[f"{label}_top{k}"] = ans6 in hit6[:k]
        rows.append(row)

    n = len(rows)

    def cnt(key):
        return sum(1 for r in rows if r[key])

    def pct(key):
        return f"{cnt(key) / n * 100:.1f}% ({cnt(key)}/{n})"

    # 요약에서 대표로 쓸 k. top_ks 에 없으면 아래 집계가 KeyError 로 죽으므로
    # 여기서 먼저 막는다. 조용히 다른 k로 바꿔치기하면 잘못된 숫자를 보게 된다.
    if main_k not in top_ks:
        raise ValueError(f"main_k={main_k} 가 top_ks={top_ks} 에 없습니다.")

    대표 = f"제외_top{main_k}"
    회수건 = [r["no"] for r in rows if r[대표]]
    교집합 = [r["no"] for r in rows if r[대표] and r["사정권"]]

    print(f"코퍼스 {len(meta)}건 (기본 검색 대상 {정직.sum()}건) · 평가셋 {n}건")
    print(f"  정답 6자리가 코퍼스에 존재 : {pct('존재6')}")
    print(f"  정답 4자리가 코퍼스에 존재 : {pct('존재4')}")
    print(f"  검색 top-{main_k} 회수            : {pct(대표)}")
    if cnt("존재6"):
        r = cnt(대표) / cnt("존재6") * 100
        print(f"  존재하는 것 중 회수율      : {r:.1f}% ({cnt(대표)}/{cnt('존재6')})")
    print(f"  재정렬 사정권({len(사정권)}건) 교집합 : {len(교집합)}건  {교집합}")
    print(f"  회수 성공 건               : {회수건}")

    print("\n  top_k 곡선 (제외 / 포함)")
    for k in top_ks:
        print(f"    k={k:<3} {cnt(f'제외_top{k}'):>2}/{n}"
              f"    {cnt(f'포함_top{k}'):>2}/{n}")

    격차 = (cnt(f"포함_top{main_k}") - cnt(대표)) / n * 100
    print(f"\n  누수 격차 (top-{main_k}): {격차:.1f}%p"
          f"  — 포함 조건 숫자는 단독 인용 금지")

    return rows


if __name__ == "__main__":
    if not index_path().exists():
        build_index()

    from src import evaluate

    case = evaluate.load_cases("A")[0]  # 2번 케이스, 정답 3207209000
    print(f"\n질의: {case['입력'][:70]}...")
    print(f"정답 6자리: {case['정답'][:6]}\n")

    for rank, hit in enumerate(search(case["입력"], top_k=5), start=1):
        mark = "★" if hit["결정세번"][:6] == case["정답"][:6] else " "
        print(f"{mark} {rank}. {hit['score']:.3f}  {hit['결정세번']}  {hit['품명'][:45]}")
        print(f"      {hit['물품설명'][:90]}")
