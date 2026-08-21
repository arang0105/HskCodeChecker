"""프로젝트 전역 설정.

다른 모듈은 설정값을 직접 읽지 않고 항상 여기서 가져온다.
"""

import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# 이 파일은 src/config.py 이므로, 두 단계 위가 프로젝트 루트다.
ROOT = Path(__file__).resolve().parent.parent

# .env 파일을 읽어 os 환경변수에 올린다.
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError(".env 파일에 GEMINI_API_KEY가 없습니다.")

# 실행 기록을 어디에 남길지 정하는 스위치. storage.py 가 이 값 하나로 갈린다.
#   있으면 → Supabase(Postgres). 배포 앱이 재시작돼도 기록이 남는다
#   없으면 → data/runs.db (SQLite 파일). 로컬 개발의 기본값이다
#
# **GEMINI_API_KEY 와 달리 없다고 죽이지 않는다.** 없는 게 정상 경로다.
#
# Streamlit Community Cloud 는 Secrets 의 최상위 문자열을 환경변수로도 올려 준다.
# GEMINI_API_KEY 가 배포에서 그렇게 들어오고 있으므로 이것도 같은 방식이면 된다.
DATABASE_URL = os.getenv("DATABASE_URL")

# 저장하는 모든 시각의 기준.
#
# **datetime.now() 는 "지금"이 아니라 "이 기계가 생각하는 지금"을 준다.**
# 내 노트북은 KST 라 맞게 보였지만, 배포된 앱이 도는 Streamlit Cloud 컨테이너는
# UTC 여서 기록이 9시간 이르게 찍혔다(17:17 에 넣은 건이 08:17 로 남았다).
# 같은 함수가 일일 상한의 날짜 키로도 쓰여서, 상한이 한국 시각 오전 9시에
# 리셋되고 있었다.
#
# 한국은 서머타임이 없어 고정 오프셋으로 충분하다. zoneinfo 를 안 쓴 것은
# Windows 에서 tzdata 패키지를 따로 요구할 수 있어서다.
KST = timezone(timedelta(hours=9))

# 모델 두 개를 분리해 둔다.
#   MAIN : 공식 기록을 남기는 평가용 (유료)
#   DEV  : 코드가 도는지 확인하는 반복 실행용 (저렴)
MODEL_MAIN = "gemini-3.1-pro-preview"
MODEL_DEV = "gemini-3.7-flash"

# 임베딩(문장 → 숫자 벡터) 모델. 입력 한도 8192 토큰, 출력 3072차원.
# 우리 물품설명 최대가 3,137자라 한도 2048짜리 embedding-001 로는 부족하다.
MODEL_EMBED = "models/gemini-embedding-2"

# 경로
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
BASELINE_XLSX = DATA_DIR / "baseline_측정표.xlsx"
