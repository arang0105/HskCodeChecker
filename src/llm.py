"""Gemini 호출 래퍼.

다른 모듈은 google.generativeai를 직접 부르지 않고 여기의 ask()만 쓴다.
그래야 모델 교체나 재시도 처리를 한 곳에서 고칠 수 있다.
"""

import json
import time

import google.generativeai as genai
from google.api_core import exceptions as gexc

from src import config

# API 키 등록. import 시점에 한 번만 실행된다.
genai.configure(api_key=config.GEMINI_API_KEY)

# 다시 걸어 보면 되는 오류들. 서버가 잠깐 느리거나 붐빌 때 나온다.
#   DeadlineExceeded(504)     응답이 제한 시간 안에 안 옴  ← 실제로 겪은 것
#   ServiceUnavailable(503)   서버가 일시적으로 못 받음
#   InternalServerError(500)  서버 내부 오류
#   ResourceExhausted(429)    호출량 제한. 기다리면 풀린다
#
# 여기 없는 오류(예: API 키가 틀린 PermissionDenied)는 **재시도해도 소용없다.**
# 몇 번을 걸어도 같은 답이 오므로 즉시 멈추는 게 맞다.
# 40분짜리 배치가 일시적 504 하나로 통째로 날아가는 걸 막는 게 목적이다.
RETRY_ERRORS = (
    gexc.DeadlineExceeded,
    gexc.ServiceUnavailable,
    gexc.InternalServerError,
    gexc.ResourceExhausted,
)
RETRY = 3        # 총 시도 횟수
RETRY_WAIT = 20  # 첫 대기(초). 재시도할수록 늘린다


def ask(prompt, model=None, temperature=1.0, as_json=False):
    """프롬프트를 보내고 응답과 사용량을 dict로 돌려준다.

    model 을 안 주면 개발용(저렴한) 모델을 쓴다.
    temperature 기본값 1.0 은 baseline 측정 조건과 맞춘 것이다.
    as_json=True 면 모델에게 JSON만 출력하도록 강제한다.
    """
    model_name = model or config.MODEL_DEV

    # generation_config 는 dict 다. 필요할 때만 키를 추가한다.
    gen_config = {"temperature": temperature}
    if as_json:
        # 이 한 줄이 핵심. 모델이 설명文·마크다운 없이 JSON만 뱉게 만든다.
        gen_config["response_mime_type"] = "application/json"

    started = time.time()

    # range(1, RETRY + 1) 은 1, 2, 3 을 준다. 시도 횟수를 1부터 세려는 것.
    for attempt in range(1, RETRY + 1):
        try:
            response = genai.GenerativeModel(model_name).generate_content(
                prompt,
                generation_config=gen_config,
            )
            break  # 성공했으면 반복을 빠져나간다
        except RETRY_ERRORS as e:
            if attempt == RETRY:
                raise  # 마지막 시도까지 실패하면 그대로 터뜨린다
            대기 = RETRY_WAIT * attempt  # 20초 → 40초. 붐빌수록 더 기다린다
            print(f"    ! {type(e).__name__} — {대기}초 후 재시도"
                  f" ({attempt}/{RETRY - 1})")
            time.sleep(대기)

    elapsed = time.time() - started

    usage = response.usage_metadata
    # candidates_token_count 에는 thinking 토큰이 빠져 있다.
    # 과금은 thinking 도 output 으로 계산하므로 total 에서 역산한다.
    think = usage.total_token_count - usage.prompt_token_count - usage.candidates_token_count

    return {
        "text": response.text,
        "model": model_name,
        "elapsed": elapsed,
        "in_tokens": usage.prompt_token_count,
        "out_tokens": usage.candidates_token_count,
        "think_tokens": think,
        # 실제 과금 기준 output = 응답 + thinking
        "billed_out": usage.candidates_token_count + think,
        "total_tokens": usage.total_token_count,
    }


def ask_json(prompt, model=None, temperature=1.0):
    """ask() 와 같지만 응답을 파싱해 'data' 키에 파이썬 dict 로 담아 준다.

    파싱에 실패하면 data=None 이고 'raw' 에 원문이 남는다.
    실패를 예외로 터뜨리지 않는 이유: 29건 배치 중 1건이 깨졌다고
    전체가 멈추면 안 되기 때문이다.
    """
    result = ask(prompt, model=model, temperature=temperature, as_json=True)

    # try/except 는 Java의 try/catch 와 같다. 'as e' 가 catch 변수명.
    try:
        data = json.loads(result["text"])
    except json.JSONDecodeError as e:
        result["data"] = None
        result["parse_error"] = str(e)
        return result

    # JSON 문법이 맞아도 dict 가 아닐 수 있다. 모델이 요청한 객체 대신
    # 배열 [{...}] 을 돌려주는 일이 실제로 있었다(2026-08-20, [4] 세번 확정).
    #
    # 부르는 쪽은 전부 data.get(...) 을 쓴다. 빈 값 검사(if not data)는
    # 원소가 있는 리스트를 통과시키므로 그다음 .get() 에서 AttributeError 가 나고
    # **29건 배치가 통째로 죽는다.** 여기서 파싱 실패로 바꿔 두면 부르는 쪽의
    # 기존 실패 처리 경로를 그대로 타고, 집계의 '파싱실패'에도 잡힌다.
    #
    # 배열을 벗겨서 살려주지 않는 이유 — 모델이 형식을 어긴 것은 측정 대상이다.
    # 조용히 고쳐 주면 그 사실이 숫자에서 사라진다.
    if not isinstance(data, dict):
        result["data"] = None
        result["parse_error"] = "JSON 최상위가 dict 가 아님: %s" % type(data).__name__
        return result

    result["data"] = data
    result["parse_error"] = None
    return result


# 후보 3개를 뽑는 프롬프트. 지금은 여기 두고, pipeline.py 로 옮길 것이다.
CANDIDATE_PROMPT = """당신은 관세 품목분류 전문가입니다.
아래 물품설명을 읽고 HS 6자리 후보를 가능성이 높은 순서로 정확히 3개 제시하세요.

물품설명:
{desc}

반드시 다음 JSON 형식으로만 답하세요.
{{
  "candidates": [
    {{"code": "6자리 숫자", "reason": "한 문장 근거"}},
    {{"code": "6자리 숫자", "reason": "한 문장 근거"}},
    {{"code": "6자리 숫자", "reason": "한 문장 근거"}}
  ]
}}"""


if __name__ == "__main__":
    desc = (
        "Silica sol, Methyltrimethoxysilane, 안료(Titanium dioxide, "
        "Copper chromite black spinel 등), Isopropyl alcohol, Formic acid, "
        "Water로 혼합·조제된 회색계 점조 액상. "
        "용도: 프라이팬 등의 세라믹 코팅(경화 온도 180~300℃)"
    )

    # .format() 으로 {desc} 자리에 값을 끼워 넣는다.
    # 프롬프트 안에 JSON 중괄호가 있어서 f-string 대신 이 방식을 썼다.
    result = ask_json(CANDIDATE_PROMPT.format(desc=desc))

    print(f"model  : {result['model']}")
    print(f"elapsed: {result['elapsed']:.1f}s")
    print(f"tokens : in={result['in_tokens']} out={result['out_tokens']}")
    print(f"parse  : {result['parse_error'] or 'OK'}")
    print()

    if result["data"]:
        for i, c in enumerate(result["data"]["candidates"], start=1):
            print(f"  {i}. {c['code']}  {c['reason']}")
    else:
        print("원문:", result["text"][:300])

    print()
    print("정답 6자리: 320720")
