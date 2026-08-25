// 이 브라우저에서 앞서 물어본 것들.
//
// **서버에 목록 API 를 만들지 않는다.** runs 표에 다 남아 있지만, 그걸 다시
// 꺼내 주려면 "이 목록이 누구 것인가"를 서버가 판정해야 한다. 그러면 익명
// uuid 였던 세션id 가 신원 노릇을 하기 시작한다. 브라우저가 자기 것만
// 기억하면 그런 판정 자체가 필요 없다.
//
// Streamlit 은 이걸 st.session_state 에 뒀다(app.py:168). 서버 메모리라
// 새로고침하면 사라졌다. localStorage 는 남는다 — 세션id 를 이미 여기
// 두고 있으므로 저장하는 곳이 하나 더 늘지는 않는다.

const 키 = "hsk_이력";
const 최대 = 20;      // 세션 상한이 5회라 넘칠 일은 없지만, 무한히 쌓지는 않는다

export type 이력항목 = {
  때: number;              // Date.now() 밀리초
  desc: string;            // 앞부분만 자른 물품설명
  코드: string;
  확신도: string | null;
};

/** 저장된 목록. 값이 깨졌으면 빈 배열로 친다. */
export function 이력읽기(): 이력항목[] {
  try {
    const v = JSON.parse(localStorage.getItem(키) ?? "[]");
    // localStorage 는 문자열만 담는다. 누가 손으로 고쳐 넣었을 수도 있어서
    // 배열인지 한 번 본다. **목록 하나 때문에 앱 전체가 죽으면 안 된다.**
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

/** 맨 앞에 하나 끼워 넣고 새 목록을 돌려준다. */
export function 이력쓰기(항목: 이력항목): 이력항목[] {
  const 새것 = [항목, ...이력읽기()].slice(0, 최대);
  try {
    localStorage.setItem(키, JSON.stringify(새것));
  } catch {
    // 용량 초과이거나 사생활 보호 모드다. 못 남겨도 방금 결과는 화면에 있다.
  }
  return 새것;
}

/** 목록을 비운다. 남의 화면에서 쓰고 지우고 싶을 때가 있다. */
export function 이력비우기(): void {
  try {
    localStorage.removeItem(키);
  } catch { /* 위와 같은 이유로 삼킨다 */ }
}

/** 1756... → "8/25 14:03". 날짜만 필요하지 연도까지는 필요 없다. */
export function 때표기(ms: number): string {
  const d = new Date(ms);
  const 두자리 = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${두자리(d.getHours())}:${두자리(d.getMinutes())}`;
}
