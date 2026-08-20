"""관세법령정보포털(CLIP) 품목분류 국내사례 수집.

요청 구조 해독 기록은 _grill/clip_요청구조.md 에 있다.
핵심: 카테고리마다 URL 경로 조각과 코드값이 다르다. 섞이면 빈 응답이 온다.
"""

import csv
import re
import time
from datetime import datetime

import requests

from src import config
from src.evaluate import normalize_code

BASE = "https://unipass.customs.go.kr"

# 카테고리별 접속 정보.
#   seg      : URL 경로 조각 (품목분류사례만 없음)
#   screen   : 세션을 여는 화면. 이걸 먼저 GET 해야 목록이 동작한다
#   tpcd     : prlstClsfCaseTpcd (목록 조회용)
#   mttr     : mttrTpcd (상세 조회용). 품목분류사례는 필요 없다
#   dtl      : 상세 엔드포인트 이름 (품목분류사례만 Dtl, 나머지는 Dtl2)
CATEGORIES = {
    "협의회": {
        "seg": "cncidtrm", "screen": "openULS0203005S.do",
        "tpcd": "03", "mttr": "02", "dtl": "retrieveDmstPrlstClsfCaseDtl2.do",
    },
    "위원회": {
        "seg": "cmitdtrm", "screen": "openULS0203008S.do",
        "tpcd": "04", "mttr": "01", "dtl": "retrieveDmstPrlstClsfCaseDtl2.do",
    },
    "품목분류사례": {
        "seg": "", "screen": "openULS0203002S.do",
        "tpcd": "01", "mttr": None, "dtl": "retrieveDmstPrlstClsfCaseDtl.do",
    },
}

# 상대방 서버에 대한 예의. 요청 사이에 이만큼 쉰다.
DELAY = 1.0


def _path(category, name):
    """카테고리별 URL을 만든다. seg 가 빈 문자열이면 경로 조각을 넣지 않는다."""
    seg = CATEGORIES[category]["seg"]
    middle = f"/{seg}" if seg else ""
    return f"{BASE}/clip/prlstclsfsrch{middle}/{name}"


def open_session(category):
    """검색 화면을 한 번 열어 세션(쿠키)을 만든다.

    requests.Session 은 쿠키를 자동으로 기억하는 객체다.
    isAjax 헤더가 없으면 서버가 HTML 오류 페이지를 돌려준다 (CLIP 내부 규약).
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "isAjax": "true",
        "Referer": _path(category, CATEGORIES[category]["screen"]),
    })
    session.get(_path(category, CATEGORIES[category]["screen"]), timeout=30)
    return session


def _post(session, url, payload, tries=4):
    """POST 하고, 실패하면 잠깐 쉬었다 다시 시도한다.

    상대 서버가 일시적으로 오류를 내는 경우가 있다.
    한 건 때문에 500건짜리 작업이 죽으면 안 되므로 여기서 흡수한다.
    대기 시간을 점점 늘리는 방식(backoff)을 쓴다 — 서버가 바쁠 때 더 몰아붙이지 않기 위해서다.
    """
    last = None
    for attempt in range(tries):
        if attempt:
            time.sleep(DELAY * (2 ** attempt))  # 2, 4, 8초...
        try:
            return session.post(url, data=payload, timeout=60)
        except requests.RequestException as e:
            last = e
    raise RuntimeError(f"{tries}회 재시도 실패: {last}")


def fetch_list(session, category, st, ed, page=1, per_page=100):
    """목록 한 페이지를 가져온다. (총건수, 항목리스트) 를 돌려준다.

    시행일자 범위(st~ed)는 반드시 준다. 조건 없이 전체를 조회하면 서버가 버틴다.
    날짜 형식은 '2026-01-01'.
    """
    payload = {
        "srchYn": "Y", "scrnTp": "WDTH",
        "sortColm": "", "sortOrdr": "", "atntSrchTp": "",
        "rrdcNo": "", "docId": "", "scrnId": "",
        "reffNo": "", "dtrmHsSgn": "", "cmdtNm": "", "cmdtDesc": "",
        "dtrmRsnCn": "", "srwr": "",
        "stDt": st, "edDt": ed,
        # 화면의 폼 필드 이름은 initPageIndex/pagePerRecord 지만,
        # 실제로 서버가 읽는 이름은 pageIndex/pageUnit 이다. (kcs4g_table_common.js)
        "pageIndex": str(page), "pageUnit": str(per_page),
        "initPageIndex": str(page), "pagePerRecord": str(per_page),
        "prlstClsfCaseTpcd": CATEGORIES[category]["tpcd"],
    }
    url = _path(category, "retrieveDmstPrlstClsfCaseLst2.do")

    # 서버가 오류를 JSON으로 돌려줄 때가 있다(uls_dmst 자체가 없다).
    # 실제로 2,800건째에서 한 번 겪었다. 잠깐 뒤엔 정상으로 돌아온다.
    # 2,4,8,16,32,64초까지 기다린다 — 총 2분이면 대개 회복된다.
    for attempt in range(7):
        if attempt:
            wait = DELAY * (2 ** attempt)
            print(f"    목록 응답 이상 — {wait:.0f}초 후 재시도 ({attempt}/6)")
            time.sleep(wait)
        body = _post(session, url, payload).json()
        if "uls_dmst" in body:
            data = body["uls_dmst"]
            return data["thisTotalCount"], data["itemList"]

    raise RuntimeError(f"목록 조회 실패: {category} p{page}")


# 상세 HTML에서 <th>라벨</th><td>값</td> 쌍을 뽑는 정규식.
_ROW = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _clean(html_fragment):
    """HTML 태그를 지우고 공백을 정리한다."""
    text = _TAG.sub(" ", html_fragment)
    return re.sub(r"\s+", " ", text).strip()


def fetch_detail(session, category, rrdc_no):
    """상세를 가져와 {라벨: 값} dict 로 돌려준다.

    목록의 물품설명·결정사유는 176자에서 잘려 있다. 원문은 여기서만 얻는다.
    """
    payload = {"rrdcNo": rrdc_no}
    mttr = CATEGORIES[category]["mttr"]
    if mttr:
        payload["mttrTpcd"] = mttr

    url = _path(category, CATEGORIES[category]["dtl"])
    html = _post(session, url, payload).text

    return {_clean(label): _clean(value) for label, value in _ROW.findall(html)}


def count_by_year(category, years):
    """연도별 건수를 세어 본다. 수집 범위를 정하기 위한 사전 조사다."""
    session = open_session(category)
    total = 0
    print(f"[{category}]  (prlstClsfCaseTpcd={CATEGORIES[category]['tpcd']})")
    for year in years:
        time.sleep(DELAY)
        count, _ = fetch_list(session, category, f"{year}-01-01", f"{year}-12-31", per_page=1)
        total += count
        print(f"  {year}: {count:>6}건")
    print(f"  합계: {total}건")
    return total


RAW_CSV = config.DATA_DIR / "결정례_raw.csv"

FIELDS = [
    "rrdc_no", "계열", "참조번호", "시행일자", "시행기관",
    "결정세번", "결정세번_정규화", "품명", "물품설명", "결정사유",
    "이미지건수", "수집시각",
]


def _done_keys():
    """이미 수집한 rrdc_no 집합을 돌려준다. 중단 후 이어받기 위한 것.

    set 은 '있는지 확인'이 빠른 자료구조다. Java의 HashSet 과 같다.
    """
    if not RAW_CSV.exists():
        return set()
    with open(RAW_CSV, encoding="utf-8-sig", newline="") as f:
        return {row["rrdc_no"] for row in csv.DictReader(f)}


def collect(category, st, ed, per_page=100):
    """한 계열을 기간 범위로 수집해 CSV에 이어붙인다.

    이미 받은 건은 건너뛴다. 중간에 끊겨도 다시 실행하면 이어서 받는다.
    """
    done = _done_keys()
    session = open_session(category)

    total, first_page = fetch_list(session, category, st, ed, page=1, per_page=per_page)
    print(f"[{category}] {st}~{ed}  총 {total}건  (이미 보유 {len(done)}건)")

    # 전체 페이지 수. -(-a//b) 는 올림 나눗셈 관용구다.
    pages = -(-total // per_page)

    # newline="" 는 CSV 쓸 때 빈 줄이 끼는 윈도우 문제를 막는다.
    # "a" 는 append 모드. 파일이 없으면 만들고 헤더를 쓴다.
    is_new = not RAW_CSV.exists()
    with open(RAW_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()

        got = skipped = failed = 0
        for page in range(1, pages + 1):
            items = first_page if page == 1 else None
            if items is None:
                time.sleep(DELAY)
                try:
                    _, items = fetch_list(session, category, st, ed, page=page, per_page=per_page)
                except RuntimeError as e:
                    # 이 페이지만 건너뛴다. 다시 실행하면 이 페이지부터 회수된다.
                    # 한 페이지 때문에 남은 수천 건을 포기할 이유가 없다.
                    print(f"  page {page} 건너뜀: {e}")
                    continue

            for item in items:
                rrdc = item["RRDC_NO"]
                if rrdc in done:
                    skipped += 1
                    continue

                time.sleep(DELAY)
                try:
                    detail = fetch_detail(session, category, rrdc)
                except Exception as e:
                    # 이 한 건만 포기하고 계속 간다. 다시 실행하면 이 건부터 재시도된다.
                    failed += 1
                    print(f"    실패 {rrdc}: {e}")
                    continue

                # 결정세번 칸에는 '3207.20-9000 관세율표 해설서' 처럼
                # 버튼 글자가 붙어 온다. 앞쪽 코드만 잘라낸다.
                hs_raw = detail.get("결정세번", "").split(" ")[0]

                writer.writerow({
                    "rrdc_no": rrdc,
                    "계열": category,
                    "참조번호": detail.get("참조번호", item.get("REFF_NO", "")),
                    "시행일자": detail.get("시행일자", ""),
                    "시행기관": detail.get("시행기관", ""),
                    "결정세번": hs_raw,
                    "결정세번_정규화": normalize_code(hs_raw),
                    "품명": detail.get("품명", ""),
                    "물품설명": detail.get("물품설명", ""),
                    "결정사유": detail.get("결정사유", ""),
                    "이미지건수": detail.get("이미지건수", ""),
                    "수집시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                f.flush()  # 중간에 끊겨도 여기까지는 파일에 남는다
                done.add(rrdc)
                got += 1

            print(f"  page {page}/{pages}  누적 신규 {got} / 건너뜀 {skipped} / 실패 {failed}")

    print(f"[{category}] 완료 — 신규 {got}, 건너뜀 {skipped}, 실패 {failed}")
    return got


# 결정세번 칸에서 HS 코드를 뽑는 정규식.
# '제2208.90-9000호', '6109.10-1000(상의),' 같은 표기에서 코드만 건진다.
_HS = re.compile(r"\d{4}\.?\d{2}-?\d{4}")


def extract_hs(text):
    """결정세번 문자열에서 10자리 코드를 뽑는다. 못 뽑으면 빈 문자열."""
    match = _HS.search(str(text))
    return normalize_code(match.group(0)) if match else ""


TEST_CSV = config.DATA_DIR / "테스트셋.csv"


def _test_ids(df, test_size, seed):
    """테스트셋 30건의 rrdc_no 를 정한다.

    파일이 이미 있으면 그 30건을 그대로 쓴다. 없을 때만 새로 뽑는다.

    왜 이렇게 하나 — 홀드아웃은 '같은 30건'을 두고 하는 약속이다.
    돌릴 때마다 다시 뽑으면 마음에 드는 조합이 나올 때까지 반복할 수 있게 되고,
    그 순간 홀드아웃은 아무것도 지켜주지 못한다.
    """
    import pandas as pd

    if TEST_CSV.exists():
        saved = set(pd.read_csv(TEST_CSV, dtype=str)["rrdc_no"])
        missing = saved - set(df["rrdc_no"])
        if missing:
            # 조용히 29건으로 진행하면 홀드아웃이 깨진 걸 모르고 지나간다.
            raise RuntimeError(
                f"테스트셋 {len(missing)}건이 코퍼스에서 사라졌습니다: {sorted(missing)}"
            )
        print(f"테스트셋 {len(saved)}건 재사용 (기존 파일 유지)")
        return saved

    # --- 신규 선정 ---
    # 후보는 협의회이면서 평가셋이 아닌 것. 평가셋과 성격을 맞추기 위해서다.
    pool = df[(df["계열"] == "협의회") & (df["평가셋포함"] == "N")].copy()
    pool["류"] = pool["결정세번_정규화"].str[:2]

    # 류가 한쪽에 몰리지 않게 류별로 한 건씩 돌아가며 뽑는다(round-robin).
    # sample(frac=1, random_state=seed) 은 순서를 섞되 매번 같은 결과를 준다.
    pool = pool.sample(frac=1, random_state=seed)
    buckets = {}
    for _, row in pool.iterrows():
        buckets.setdefault(row["류"], []).append(row["rrdc_no"])

    picked = []
    while len(picked) < test_size:
        added = False
        for ryu in sorted(buckets):
            if buckets[ryu] and len(picked) < test_size:
                picked.append(buckets[ryu].pop(0))
                added = True
        if not added:
            break

    print(f"테스트셋 {len(picked)}건 신규 선정 (후보 {len(pool)}건)")
    return set(picked)


def build_corpus(test_size=30, seed=20260818):
    """수집 원본을 코퍼스와 테스트셋으로 나눈다.

    - 테스트셋: 협의회에서 30건. 코퍼스에서 완전히 제외한다
    - 코퍼스  : 나머지. 평가셋 29건은 '평가셋포함' 플래그로 표시만 한다
    """
    import pandas as pd

    from src import evaluate

    df = pd.read_csv(RAW_CSV, dtype=str).fillna("")
    df["결정세번_정규화"] = df["결정세번"].map(extract_hs)

    # 세번을 못 뽑았거나 물품설명이 빈 행은 검색에 쓸 수 없다.
    usable = (df["결정세번_정규화"].str.len() == 10) & (df["물품설명"].str.len() > 0)
    print(f"수집 {len(df)}건 → 사용 가능 {usable.sum()}건 (제외 {(~usable).sum()}건)")
    df = df[usable].copy()

    # 평가셋 29건 표시 — 참조번호 + 계열(협의회) + 결정세번 세 조건이 모두 맞아야 한다.
    eval_keys = {
        (c["결정례번호"].strip(), c["정답"]) for c in evaluate.load_cases("A")
    }
    df["평가셋포함"] = [
        "Y" if (ref.strip(), hs) in eval_keys and gye == "협의회" else "N"
        for ref, hs, gye in zip(df["참조번호"], df["결정세번_정규화"], df["계열"])
    ]
    print(f"평가셋 매칭: {(df['평가셋포함'] == 'Y').sum()}건 / {len(eval_keys)}")

    test_ids = _test_ids(df, test_size, seed)

    # rrdc_no 로 나눈다. 인덱스 라벨로 나누면 수집 순서가 바뀔 때 조용히 어긋난다.
    is_test = df["rrdc_no"].isin(test_ids)
    test, corpus = df[is_test], df[~is_test]

    cols = ["rrdc_no", "계열", "참조번호", "시행일자", "결정세번_정규화", "품명", "물품설명", "결정사유"]
    # 행(rrdc_no)은 고정이지만 열 구성은 바뀔 수 있으므로 매번 다시 쓴다.
    test[cols].to_csv(TEST_CSV, index=False, encoding="utf-8-sig")
    corpus[cols + ["평가셋포함"]].to_csv(
        config.DATA_DIR / "결정례.csv", index=False, encoding="utf-8-sig"
    )

    print(f"테스트셋: {len(test)}건 (류 {test['결정세번_정규화'].str[:2].nunique()}종)")
    print(f"코퍼스  : {len(corpus)}건 "
          f"(평가셋포함 Y={(corpus['평가셋포함'] == 'Y').sum()}건 — 기본 검색에서는 제외)")
    return corpus, test


def collect_years(category, years, per_page=100):
    """연도별로 쪼개서 수집한다.

    한 번에 넓은 기간을 요청하면 뒤쪽 페이지에서 서버가 못 버틴다.
    실제로 5,507건(56페이지)을 요청했더니 29페이지부터 계속 오류가 났다.
    깊은 페이지를 요구하지 않도록 범위를 잘라 주는 게 해법이다.
    """
    total = 0
    for year in years:
        try:
            total += collect(category, f"{year}-01-01", f"{year}-12-31", per_page)
        except Exception as e:
            # 한 해가 실패해도 나머지 해는 마저 받는다.
            print(f"[{category} {year}] 중단: {e}")
        print()
    return total


if __name__ == "__main__":
    YEARS = range(2022, 2027)
    for cat in ("협의회", "위원회", "품목분류사례"):
        collect_years(cat, YEARS)
