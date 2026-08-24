// api/main.py 의 pydantic 모델과 **같은 모양**을 손으로 적어 둔 것이다.
//
// OpenAPI 스키마에서 자동 생성하는 도구가 있지만 쓰지 않는다. 도구 하나를 더
// 배우고 빌드 단계를 얹는 값이 이 규모에서는 나오지 않는다. 손으로 맞추고,
// 어긋나면 화면에 undefined 가 떠서 바로 드러난다.
//
// 필드 이름이 한글인 것은 서버가 그렇게 주기 때문이다. DB 열 이름부터
// 한글이라 어디서 영어로 바꾸든 그 지점에 번역표가 하나 생긴다.
// 자바스크립트는 한글 식별자를 그대로 받으므로 data.충분 처럼 쓰면 된다.

// ---- 요청 ----

export type GateIn = {
  desc: string;
  세션id: string;
};

export type ClassifyIn = {
  desc: string;
  세션id: string;
  유입: string | null;
  강행: boolean;
  // 방금 /api/gate 가 돌려준 값을 그대로 되돌려 보낸다. 기록용이다 —
  // 서버는 여기서 게이트를 다시 부르지 않는다(돈이 두 번 든다).
  게이트_충분: boolean;
  게이트_부족항목: string[];
  게이트_질문: string[];
};

export type FeedbackIn = {
  run_id: number;
  세션id: string;
  평가: "up" | "down" | null;
  메모: string | null;
};

// ---- 응답 ----

export type GateOut = {
  충분: boolean;
  부족항목: string[];
  질문: string[];
};

export type 결정례 = {
  참조번호: string;
  score: number;
  품명: string;
  결정세번: string;
  물품설명: string;
};

export type Ranked = {
  code: string;
  reason: string | null;
  근거결정례: string | null;
};

export type ClassifyOut = {
  run_id: number | null;
  저장실패: string | null;
  오류: string | null;

  코드: string;
  순위: string[];
  ranked: Ranked[];

  // 서버는 'high' | 'medium' | 'low' 를 주지만 string 으로 받는다.
  // 모델이 다른 값을 뱉을 수 있고, 그때 화면이 죽는 것보다 그대로 찍히는
  // 편이 낫다. pipeline.gate 를 fail-open 으로 둔 것과 같은 판단이다.
  확신도: string | null;
  확인포인트: string[];
  확정근거: string | null;
  확정확신도: string | null;
  확정확인포인트: string[];
  top근거: string | null;
  top결정례: string | null;

  결정례: 결정례[];
  선택지수: number;
  자동확정: boolean;
  elapsed: number;
  남은횟수: number;
};

// ---- SSE 이벤트 ----
// event: 단계 / event: 결과 / event: 오류 세 가지가 온다.

export type 단계이벤트 = {
  번호: number;          // 1~4
  이름: string;
  상태: "시작" | "완료";
  초?: number;           // '완료' 일 때만 온다. ? 는 없을 수 있다는 표시다
};
