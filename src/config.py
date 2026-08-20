"""프로젝트 전역 설정.

다른 모듈은 설정값을 직접 읽지 않고 항상 여기서 가져온다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 이 파일은 src/config.py 이므로, 두 단계 위가 프로젝트 루트다.
ROOT = Path(__file__).resolve().parent.parent

# .env 파일을 읽어 os 환경변수에 올린다.
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError(".env 파일에 GEMINI_API_KEY가 없습니다.")

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
