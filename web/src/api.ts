// 서버와 이야기하는 자리를 여기 한 곳에 모은다.
// 화면(App.tsx)은 fetch 를 직접 부르지 않는다.

import type {
  CatalogOut, ClassifyIn, ClassifyOut, FeedbackIn, GateIn, GateOut, QuotaOut,
  단계이벤트,
} from "./types";

// 개발 중에는 localhost, 배포에서는 Render 주소.
// import.meta.env 는 Vite 가 빌드할 때 값을 박아 넣는 자리다.
// **API 키 같은 비밀은 절대 여기 두지 않는다** — 번들은 누구나 열어본다.
const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

// ---- 세션 식별 ----

// 브라우저마다 하나씩 만들어 두는 익명 uuid4. 사람을 식별하지 않는다.
// 서버는 이 값으로 runs 표를 세서 세션 상한(5회)을 건다.
//
// Streamlit 은 서버 메모리에 세션을 들고 있었지만 FastAPI 에는 그게 없다.
// 브라우저가 기억하는 수밖에 없어서 localStorage 를 쓴다.
export function 세션id(): string {
  const 키 = "hsk_세션id";
  let v = localStorage.getItem(키);
  if (!v) {
    v = crypto.randomUUID();
    localStorage.setItem(키, v);
  }
  return v;
}

// 주소 뒤의 ?u=... 를 읽는다. 링크마다 다른 값을 주면 누가 어느 경로로
// 들어왔는지 갈린다. 길이를 자르는 것은 서버도 하지만 여기서도 한다.
export function 유입(): string | null {
  const v = new URLSearchParams(location.search).get("u");
  return v ? v.trim().slice(0, 20) || null : null;
}

// ---- 공통 ----

/** 서버가 4xx/5xx 를 주면 detail 문자열을 담아 던진다. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 응답이 실패면 서버가 준 안내 문구를 담아 던진다.
 *
 *  FastAPI 는 { "detail": "..." } 로 준다. pydantic 검증 실패(422)면
 *  detail 이 배열이라 문자열이 아니다 — 그건 우리 버그이므로 뭉뚱그린다.
 */
async function 실패면_던지기(r: Response): Promise<void> {
  if (r.ok) return;
  const d = await r.json().catch(() => ({}));
  const 말 = typeof d.detail === "string" ? d.detail : "요청이 거부되었습니다.";
  throw new ApiError(r.status, 말);
}

async function post<T>(경로: string, 본문: unknown): Promise<T> {
  const r = await fetch(BASE + 경로, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(본문),
  });
  await 실패면_던지기(r);
  return r.json();
}

// ---- 엔드포인트 ----

/** 남은 횟수를 읽는다. 화면이 처음 뜰 때와 분류가 끝난 뒤에 부른다.
 *
 *  **GET 이라 본문이 없다.** 세션id 를 주소 뒤에 붙이는데, URLSearchParams
 *  가 한글 파라미터 이름까지 알아서 인코딩해 준다.
 */
export async function 잔여(): Promise<QuotaOut> {
  const 질의 = new URLSearchParams({ 세션id: 세션id() });
  const r = await fetch(`${BASE}/api/quota?${질의}`);
  if (!r.ok) throw new ApiError(r.status, "남은 횟수를 읽지 못했습니다.");
  return r.json();
}

/** [0-a] 카탈로그 파일을 올려 물품설명 초안을 받는다.
 *
 *  **Content-Type 헤더를 직접 넣지 않는다.** FormData 를 주면 브라우저가
 *  multipart/form-data 와 함께 boundary(각 파일의 경계 표시)까지 붙여 준다.
 *  헤더를 손으로 쓰면 그 boundary 가 빠져서 서버가 본문을 못 읽는다.
 */
export async function 카탈로그(파일들: File[]): Promise<CatalogOut> {
  const 폼 = new FormData();
  폼.append("세션id", 세션id());
  // 같은 이름으로 여러 번 append 하면 서버에서 배열(list[UploadFile])이 된다.
  for (const f of 파일들) 폼.append("files", f);

  const r = await fetch(BASE + "/api/catalog", { method: "POST", body: 폼 });
  await 실패면_던지기(r);
  return r.json();
}

export function 게이트(desc: string): Promise<GateOut> {
  const 본문: GateIn = { desc, 세션id: 세션id() };
  return post<GateOut>("/api/gate", 본문);
}

export function 피드백(run_id: number, 평가: "up" | "down" | null,
                     메모: string | null): Promise<{ ok: boolean }> {
  // **평가와 메모를 매번 둘 다 보낸다.** 서버가 두 열을 한꺼번에 덮어쓰므로
  // 하나만 보내면 다른 하나가 지워진다.
  const 본문: FeedbackIn = { run_id, 세션id: 세션id(), 평가, 메모 };
  return post("/api/feedback", 본문);
}

/**
 * 분류를 SSE 로 돌린다. 단계가 끝날 때마다 onStage 가 불리고,
 * 마지막에 결과를 돌려준다.
 *
 * **EventSource 를 쓰지 않는 이유** — 그건 GET 만 된다. 물품설명을 본문에
 * 실어 POST 해야 하므로 fetch 로 직접 읽고 쪼갠다.
 */
export async function 분류(
  입력: Omit<ClassifyIn, "세션id" | "유입">,
  onStage: (e: 단계이벤트) => void,
): Promise<ClassifyOut> {
  const 본문: ClassifyIn = { ...입력, 세션id: 세션id(), 유입: 유입() };

  const r = await fetch(BASE + "/api/classify/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(본문),
  });
  await 실패면_던지기(r);
  if (!r.body) throw new Error("스트림을 열 수 없습니다.");

  // 응답을 조금씩 읽는다. reader.read() 는 덩어리 하나를 기다렸다 준다.
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();

  // **덩어리 경계가 이벤트 경계와 다르다.** 네트워크는 아무 데서나 끊어서
  // 준다. 그래서 받은 것을 버퍼에 쌓아 두고, 빈 줄(\n\n)이 나올 때까지
  // 기다렸다 그 앞까지만 잘라 쓴다. 남은 꼬리는 다음 덩어리와 이어 붙인다.
  let 버퍼 = "";
  let 결과: ClassifyOut | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    버퍼 += value;

    let i: number;
    while ((i = 버퍼.indexOf("\n\n")) !== -1) {
      const 덩어리 = 버퍼.slice(0, i);
      버퍼 = 버퍼.slice(i + 2);

      const 줄들 = 덩어리.split("\n");
      const 종류 = 줄들.find((l) => l.startsWith("event: "))?.slice(7);
      const 데이터 = 줄들.find((l) => l.startsWith("data: "))?.slice(6);
      if (!종류 || !데이터) continue;

      if (종류 === "단계") onStage(JSON.parse(데이터) as 단계이벤트);
      else if (종류 === "결과") 결과 = JSON.parse(데이터) as ClassifyOut;
      else if (종류 === "오류") throw new Error(JSON.parse(데이터).detail);
    }
  }

  // 스트림이 결과 없이 끝났다는 것은 서버나 중간 프록시가 끊었다는 뜻이다.
  // 조용히 넘어가면 화면이 영원히 '처리 중'으로 남는다.
  if (!결과) throw new Error("연결이 끊겼습니다. 다시 시도해 주세요.");
  return 결과;
}
