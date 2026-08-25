// 화면 하나짜리 앱이다. 상태에 따라 보이는 것만 달라진다.
//
//   [입력] → [게이트가 되물음] → [진행 ①②③④] → [결과 + 피드백]
//
// **JSP 와 다른 점** — 아래 return 안의 HTML 처럼 생긴 것(JSX)은 문자열이
// 아니라 함수 호출로 바뀐다. 그래서 그 안에 { } 를 열고 자바스크립트 식을
// 그대로 쓸 수 있다. <% %> 없이 값이 박힌다.
//
// **useState** — 이 함수는 화면을 그릴 때마다 처음부터 다시 실행된다.
// 보통 변수에 담으면 다시 실행될 때 사라지므로, 다시 실행돼도 살아남아야
// 하는 값은 useState 에 맡긴다. set... 을 부르면 React 가 "값이 바뀌었으니
// 다시 그려라"를 알아채고 이 함수를 또 부른다. Streamlit 의 rerun 과 닮았다.

import { useEffect, useState } from "react";
import * as api from "./api";
import type { ClassifyOut, GateOut, QuotaOut, 제품, 단계이벤트 } from "./types";
import { 이력읽기, 이력쓰기, 이력비우기, 때표기, type 이력항목 } from "./history";
import "./App.css";

/** 8517130000 → 8517.13-0000. 신고서에서 쓰는 모양이다.
 *
 *  **app.py 의 세번_표기() 와 같은 규칙을 쓴다** — 두 화면이 같은 코드를
 *  다른 모양으로 보여주면 사용자가 다른 값인 줄 안다.
 *
 *  `\D` 는 "숫자가 아닌 글자" 다. /.../g 의 g 는 "처음 하나만 말고 전부".
 *  결정례 데이터에 점·하이픈이 섞여 들어와도 일단 다 걷어내고 다시 붙인다.
 */
function 세번표기(코드: string | null | undefined): string {
  const 숫자 = (코드 ?? "").replace(/\D/g, "");
  if (숫자.length !== 10) return 코드 || "—";   // 10자리가 아니면 손대지 않는다
  return `${숫자.slice(0, 4)}.${숫자.slice(4, 6)}-${숫자.slice(6)}`;
}

// 예시 두 개. **app.py:41-45 와 같은 문장이다** — 두 앱이 다른 예시를 들면
// 지인에게 보여줄 때 같은 도구로 안 보인다.
//
// 두 번째가 일부러 부실한 이유 — 품번만 적힌 입력에서 [0]게이트가 되묻는
// 것을 보여주는 자리다. baseline 에서 품번only 7건의 되묻기율은 14.3% 였고,
// 그걸 고친 게 이 도구가 하는 일 중 하나다.
const 예시_충분 =
  "폴리프로필렌(PP) 재질의 일회용 도시락 용기. 뚜껑 일체형, 용량 700ml, " +
  "전자레인지 사용 가능. 표면 인쇄 없음. 사출 성형품.";
const 예시_부족 = "P/N 4471-BK / 1EA / MADE IN VIETNAM";

// 모델은 'high' | 'medium' | 'low' 로 답한다. 화면에 그대로 내보내지 않는다 —
// 읽는 사람은 관세 실무자이지 이 프로젝트의 독자가 아니다.
//
// 표에 없는 값이 오면 **원래 값을 그대로 보여준다.** 모델이 다른 라벨을 뱉을
// 수 있고, 그때 화면이 비거나 죽는 것보다 낯선 값이 찍히는 편이 낫다.
const 확신도말 = new Map([
  ["high", "높음"],
  ["medium", "보통"],
  ["low", "낮음"],
]);
function 확신도표기(v: string | null | undefined): string {
  if (!v) return "표시 없음";
  return 확신도말.get(v) ?? v;
}

const 단계이름 = [
  "6자리 후보 뽑기",
  "비슷한 결정례 찾기",
  "결정례로 순위 다시 판단",
  "10자리 세번 고르기",
];

export default function App() {
  const [desc, setDesc] = useState("");
  const [처리중, set처리중] = useState(false);
  const [게이트, set게이트] = useState<GateOut | null>(null);
  const [단계들, set단계들] = useState<단계이벤트[]>([]);
  const [결과, set결과] = useState<ClassifyOut | null>(null);
  const [오류, set오류] = useState<string | null>(null);
  const [잠김, set잠김] = useState(false);      // 상한을 다 썼다

  // 피드백. 서버가 평가와 메모를 **한꺼번에** 덮어쓰므로 둘 다 들고 있다가
  // 매번 같이 보낸다. 하나만 보내면 다른 하나가 지워진다.
  const [평가, set평가] = useState<"up" | "down" | null>(null);
  const [메모, set메모] = useState("");
  const [보냈다, set보냈다] = useState(false);
  const [저장중, set저장중] = useState(false);
  const [잔여, set잔여] = useState<QuotaOut | null>(null);
  const [경과, set경과] = useState(0);

  // **useState 에 값이 아니라 함수를 넘겼다.** 그냥 이력읽기() 를 쓰면
  // 화면을 다시 그릴 때마다 localStorage 를 읽는다(결과는 버려진다).
  // 함수를 넘기면 React 가 **맨 처음 한 번만** 부른다.
  const [이력, set이력] = useState<이력항목[]>(이력읽기);

  // --- [0-a] 카탈로그 ---
  const [파일들, set파일들] = useState<File[]>([]);
  const [제품들, set제품들] = useState<제품[]>([]);
  const [고른번호, set고른번호] = useState(0);
  const [읽는중, set읽는중] = useState(false);
  const [카탈로그오류, set카탈로그오류] = useState<string | null>(null);

  // 입력창에 넣은 초안. **원문을 함께 들고 있어야** 사용자가 손봤는지
  // 알 수 있다. 그게 입력출처를 '카탈로그' 와 '카탈로그(수정)' 로 가른다.
  const [초안, set초안] = useState<{ 텍스트: string; 빠진정보: string[] } | null>(null);

  /** 메모를 고치는 순간 "저장했습니다"는 더 이상 사실이 아니다.
   *  초록 문구를 그대로 두면 고친 내용까지 저장된 줄 안다. */
  function 메모바꾸기(v: string) {
    set메모(v);
    set보냈다(false);
  }

  // **처리 중에 창을 닫거나 새로고침하려 하면 경고한다.**
  // 오버레이는 클릭만 막지 F5 는 못 막는다. 멈춘 줄 알고 F5 를 누르는 게
  // 정확히 걱정하는 시나리오다 — 그러면 API 호출이 두 배로 나간다.
  //
  // useEffect 는 "그리고 나서 이걸 해라"를 맡기는 자리다. 두 번째 인자
  // [처리중] 은 "처리중 이 바뀔 때만 다시 해라"는 뜻이다.
  useEffect(() => {
    if (!처리중) return;
    const 막기 = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", 막기);
    // 돌려주는 함수는 뒷정리다. 처리중이 false 가 되면 React 가 이걸 부른다.
    return () => window.removeEventListener("beforeunload", 막기);
  }, [처리중]);

  // 처리 중에는 1초마다 경과 시간을 올린다.
  //
  // **왜 필요한가** — 모델 응답이 3~146초로 튄다(2026-08-25 실측). 단계
  // 이름만 떠 있으면 3분째 같은 화면이라 멈춘 건지 도는 건지 알 수 없다.
  // 숫자가 움직이는 것 자체가 "살아 있다"는 신호다.
  //
  // **카운터를 +1 하지 않고 Date.now() 로 다시 계산하는 이유** — setInterval
  // 은 정확히 1초마다 불리지 않는다. 탭이 뒤로 가면 브라우저가 늦춘다.
  // +1 방식이면 실제 시간과 어긋나 60초가 지났는데 40초라고 뜬다.
  //
  // **돌려주는 함수에서 반드시 치운다.** 안 치우면 분류가 끝난 뒤에도
  // 타이머가 계속 돌며 1초마다 화면을 다시 그린다.
  useEffect(() => {
    if (!처리중) return;
    set경과(0);
    const 시작 = Date.now();
    const 타이머 = setInterval(
      () => set경과(Math.floor((Date.now() - 시작) / 1000)), 1000);
    return () => clearInterval(타이머);
  }, [처리중]);

  // 화면이 처음 뜰 때 남은 횟수를 읽는다. **[] 는 "한 번만" 이라는 뜻이다.**
  //
  // 실패해도 조용히 넘어간다 — 이 숫자가 없다고 분류를 못 하는 것은 아니고,
  // 잠들어 있던 API 가 깨어나는 20여 초 동안 화면에 오류를 띄울 이유가 없다.
  // (겸사겸사 API 를 미리 깨우는 효과도 있다. 화면과 API 를 나눠 배포한
  //  이유가 화면이 API 를 기다리지 않게 하려는 것이었다.)
  useEffect(() => {
    api.잔여().then(set잔여).catch(() => {});
  }, []);

  function 잔여갱신() {
    api.잔여().then(set잔여).catch(() => {});
  }

  function 오류처리(e: unknown) {
    const 말 = e instanceof Error ? e.message : "알 수 없는 오류입니다.";
    set오류(말);
    if (e instanceof api.ApiError && e.status === 429) set잠김(true);
  }

  async function 분류시작(강행: boolean, 게이트값: GateOut | null) {
    set단계들([]);
    set결과(null);
    set평가(null);
    set메모("");
    set보냈다(false);
    try {
      // **초안을 그대로 썼는지 손봤는지 가른다.** 카탈로그 경로는 봉인을
      // 다 써서 정확도를 잴 수 없으므로, 이게 추출 품질을 짐작할 유일한
      // 단서다(app.py:703-712 와 같은 판정).
      const 출처 = !초안 ? "텍스트"
        : desc.trim() === 초안.텍스트.trim() ? "카탈로그" : "카탈로그(수정)";

      const r = await api.분류(
        {
          desc: desc.trim(),
          강행,
          입력출처: 출처,
          카탈로그_빠진정보: 초안?.빠진정보 ?? [],
          게이트_충분: 게이트값?.충분 ?? true,
          게이트_부족항목: 게이트값?.부족항목 ?? [],
          게이트_질문: 게이트값?.질문 ?? [],
        },
        // 단계 이벤트가 올 때마다 목록에 덧붙인다.
        // set단계들(이전 => ...) 처럼 함수를 넘기는 이유 — 바깥의 단계들
        // 변수는 이 함수가 만들어질 때의 옛 값이다. 함수를 넘기면 React 가
        // **가장 최근 값**을 넣어 준다.
        (e) => set단계들((이전) => [...이전, e]),
      );
      set결과(r);
      if (r.남은횟수 <= 0) set잠김(true);
      // 오류가 아닐 때만 목록에 남긴다. 실패한 건까지 "이전 결과" 로
      // 쌓이면 목록을 믿을 수 없게 된다.
      if (!r.오류 && r.코드) {
        set이력(이력쓰기({
          때: Date.now(),
          desc: desc.trim().slice(0, 80),
          코드: r.코드,
          확신도: r.확신도,
        }));
      }
      잔여갱신();
    } catch (e) {
      오류처리(e);
    }
  }

  async function 카탈로그읽기() {
    if (파일들.length === 0 || 읽는중) return;
    set읽는중(true);
    set카탈로그오류(null);
    set제품들([]);
    try {
      const r = await api.카탈로그(파일들);
      set제품들(r.제품들);
      set고른번호(0);
      // 잔여 전체를 다시 읽지 않고 추출 몫만 갈아 끼운다.
      // 이전 => ... 로 넘기는 이유는 단계 이벤트 때와 같다 — 바깥의 잔여는
      // 이 함수가 만들어질 때의 옛 값이다.
      set잔여((이전) => (이전 ? { ...이전, 추출남은: r.남은추출 } : 이전));
    } catch (e) {
      set카탈로그오류(e instanceof api.ApiError ? e.message
                                              : "카탈로그를 읽지 못했습니다.");
    } finally {
      // finally 는 성공이든 실패든 지나간다. 여기서 안 풀면 버튼이 영영 잠긴다.
      set읽는중(false);
    }
  }

  function 초안넣기(p: 제품) {
    setDesc(p.물품설명);
    set초안({ 텍스트: p.물품설명, 빠진정보: p.빠진정보 });
    set게이트(null);
    set오류(null);
  }

  // 예시를 넣으면 **앞 입력에 대한 되물음과 오류는 지운다.** 남겨 두면
  // 방금 바꾼 글에 대한 안내인 줄 읽힌다. 직전 결과는 그대로 둔다 —
  // 아직 새로 분류한 게 아니므로 지울 이유가 없다.
  function 예시넣기(글: string) {
    setDesc(글);
    set초안(null);          // 이제 카탈로그에서 온 글이 아니다
    set게이트(null);
    set오류(null);
  }

  async function 눌림() {
    if (처리중 || 잠김 || !desc.trim()) return;
    set처리중(true);
    set오류(null);
    set게이트(null);
    try {
      const g = await api.게이트(desc.trim());
      set게이트(g);
      // 되물어야 하면 여기서 멈춘다. **차감하지 않는다** — 되물을수록
      // 손해면 사용자가 게이트를 우회하게 되고, 안전장치가 벌칙이 된다.
      if (g.충분) await 분류시작(false, g);
    } catch (e) {
      오류처리(e);
    } finally {
      set처리중(false);
    }
  }

  async function 강행눌림() {
    set처리중(true);
    set오류(null);
    await 분류시작(true, 게이트);
    set처리중(false);
  }

  async function 의견보내기(새평가: "up" | "down" | null, 새메모: string) {
    if (!결과?.run_id || 저장중) return;
    set저장중(true);
    set보냈다(false);
    set오류(null);
    try {
      await api.피드백(결과.run_id, 새평가, 새메모.trim() || null);
      set보냈다(true);
    } catch (e) {
      set오류(e instanceof Error ? e.message : "저장하지 못했습니다.");
    } finally {
      // finally 는 성공하든 실패하든 반드시 돈다. 자바의 try/finally 와 같다.
      // 여기서 안 풀면 한 번 실패한 뒤로 버튼이 영영 잠긴다.
      set저장중(false);
    }
  }

  const 되물음 = 게이트 !== null && !게이트.충분 && 결과 === null;

  // 남은 횟수는 두 곳에서 온다. **분류가 끝났으면 그쪽이 더 최신이다** —
  // /api/quota 는 화면이 뜰 때 읽은 값이라 방금 쓴 1회가 빠져 있다.
  const 남은 = 결과?.남은횟수 ?? 잔여?.세션남은 ?? null;

  // 방금 그린 결과가 목록 맨 위에 또 있으면 같은 것이 두 번 보인다.
  // app.py:531 이 이력[1:] 을 쓴 것과 같은 이유다.
  const 지난것 = 결과 && !결과.오류 ? 이력.slice(1) : 이력;

  // 고른 제품. 목록이 새로 오면 고른번호가 범위를 넘을 수 있어 첫 것으로 눕힌다.
  const 고른것: 제품 | undefined = 제품들[고른번호] ?? 제품들[0];

  return (
    // <> </> 는 Fragment 다. JSX 는 최상위 요소가 하나여야 하는데, 그것 때문에
    // 의미 없는 <div> 를 하나 더 두고 싶지 않을 때 쓴다. 화면에는 안 남는다.
    <>
      {/* 처리 중에는 화면 전체를 덮어 다른 데를 못 만지게 한다.
          Streamlit 은 rerun 도는 동안 위젯을 알아서 잠가 줬는데,
          React 로 옮기면 그 공짜가 사라진다. */}
      {처리중 && <div className="덮개" />}

      {/* 상단 알약(로고 띠)은 뺐다. h1 이 이름 역할을 하고 브라우저 탭 제목에도
          같은 문구가 있어 중복이었다. 세로도 줄어 입력창이 더 빨리 보인다. */}
      <section className="히어로">
        <div className="쪽 히어로속">
          <h1>HS 세번을,<br />근거와 함께.</h1>
          <p className="설명">
            물품설명을 넣으면 과거 결정례 5,872건에서 비슷한 사례를 찾아
            6자리 후보의 순위를 다시 매기고, 관세청 공식 목록에서 10자리를 고릅니다.
          </p>

          {/* 카드에 무엇을 잰 숫자인지 붙여 둔다. 숫자만 크게 걸고 어떤
              문제에서 나온 값인지 안 적으면 실무 평균으로 읽힌다. */}
          <p className="숫자제목"><b>일반 건</b> · 선례가 명확한 사전심사 20건</p>
          <div className="숫자들">
            <div className="숫자"><b>85.0%</b><span>앞 6자리 적중</span></div>
            <div className="숫자"><b>80.0%</b><span>10자리 전체 적중</span></div>
          </div>

          {/* **이 줄을 빼지 않는다.** 이것까지 빠지면 위의 85.0% 가 이 도구의
              유일한 성적처럼 읽힌다. 어려운 건에서는 절반 남짓이라는 사실이
              화면에 남아 있어야 한다. 자세한 조건은 README 에 있다. */}
          <p className="단서">
            전문가끼리도 판단이 갈려 협의회에 올라간 어려운 건 30건에서는
            6자리 56.7% / 10자리 50.0% 였습니다.
          </p>
        </div>
      </section>

      <main className="쪽 도구" id="도구">
      <p className="도구제목">물품설명 넣기</p>

      {/* **기본은 접힘.** 첫 화면을 깔끔하게 두려는 것이고, 측정된 경로는
          여전히 글로 넣는 쪽이다. 카탈로그가 있는 사람만 펼쳐서 쓴다. */}
      <details className="카탈로그">
        <summary>📄 카탈로그(PDF·사진)에서 물품설명 뽑기</summary>

        <p className="캡션">
          제품 안내서·사양표·제품 사진을 올리면 물품설명 초안을 만들어 드립니다.<br />
          <b>카탈로그에 없는 정보는 채우지 않습니다.</b> 비어 있는 항목은 따로 알려드립니다.
        </p>

        {/* e.target.files 는 배열이 아니라 FileList 다. map 이 없으므로
            Array.from 으로 진짜 배열을 만든다. 취소하면 null 이 온다. */}
        <input
          type="file"
          multiple
          accept=".pdf,.png,.jpg,.jpeg,.webp"
          disabled={읽는중}
          onChange={(e) => {
            set파일들(Array.from(e.target.files ?? []));
            set카탈로그오류(null);
          }}
        />

        <div className="줄">
          <span className="남은">
            파일 3개 · 합계 10MB 까지
            {잔여 && ` · 남은 횟수 ${잔여.추출남은}/${잔여.추출상한}`}
          </span>
          <button onClick={카탈로그읽기}
                  disabled={읽는중 || 파일들.length === 0 || 잔여?.추출남은 === 0}>
            {읽는중 ? "읽는 중…" : "카탈로그 읽기"}
          </button>
        </div>

        {카탈로그오류 && <div className="경고">{카탈로그오류}</div>}

        {고른것 && (
          <>
            {제품들.length > 1 ? (
              <label className="고르기">
                분류할 제품을 고르세요
                <select value={고른번호}
                        onChange={(e) => set고른번호(Number(e.target.value))}>
                  {제품들.map((p, i) => (
                    <option key={i} value={i}>{p.이름}</option>
                  ))}
                </select>
              </label>
            ) : (
              <p className="제품이름"><b>{고른것.이름}</b></p>
            )}

            <div className="초안상자">{고른것.물품설명}</div>

            {고른것.빠진정보.length > 0 && (
              <p className="빠진">
                카탈로그에 없어서 비워 둔 항목: <b>{고른것.빠진정보.join(", ")}</b>
              </p>
            )}

            <div className="줄">
              <button onClick={() => 초안넣기(고른것)}>
                이 초안을 물품설명에 넣기
              </button>
            </div>
          </>
        )}
      </details>

      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        disabled={처리중 || 잠김}
        rows={6}
        placeholder="예: 폴리에스터 100% 직물로 만든 성인용 반팔 티셔츠. 무게 180g/m2, 편물이 아닌 직물."
      />

      {/* 위 상자 안에도 같은 내용이 있지만 그건 접으면 사라진다.
          "재질은 내가 채워야 한다"가 안 보이면 비어 있는 채로 분류가 돈다. */}
      {초안 && 초안.빠진정보.length > 0 && (
        <p className="빠진">
          카탈로그에 없어서 비워 둔 항목: <b>{초안.빠진정보.join(", ")}</b>
          {" — "}아시면 위에 직접 채워 주세요
        </p>
      )}

      {/* .줄 은 오른쪽 정렬이다. 그래서 **남은 횟수를 버튼보다 먼저** 둔다 —
          CSS 로 순서를 뒤집을 수도 있지만, 그러면 눈에 보이는 순서와
          Tab 키가 도는 순서가 어긋난다. */}
      <div className="줄">
        {/* 예시 버튼은 곁들이라 **왼쪽으로 민다**(CSS margin-right: auto).
            채운 버튼 셋을 나란히 두면 무엇을 눌러야 할지 알 수 없다. */}
        <div className="예시들">
          <button className="보조" onClick={() => 예시넣기(예시_충분)}
                  disabled={처리중 || 잠김}>예시: 상세 설명</button>
          <button className="보조" onClick={() => 예시넣기(예시_부족)}
                  disabled={처리중 || 잠김}>예시: 품번만</button>
        </div>
        {남은 !== null && <span className="남은">남은 횟수 {남은}회</span>}
        <button onClick={눌림} disabled={처리중 || 잠김 || !desc.trim()}>
          {처리중 ? "분류하는 중…" : "분류하기"}
        </button>
      </div>

      {오류 && <div className="경고">{오류}</div>}

      {되물음 && (
        <div className="되물음">
          <b>정보가 부족합니다.</b>
          <p>부족한 항목: {게이트!.부족항목.join(", ") || "—"}</p>
          <ul>{게이트!.질문.map((q, i) => <li key={i}>{q}</li>)}</ul>
          <button onClick={강행눌림} disabled={처리중}>
            그래도 분류하기
          </button>
        </div>
      )}

      {/* **조건이 단계들 이 아니라 처리중 이다.** [0]게이트도 LLM 호출이라
          느릴 수 있는데, 그동안 단계 이벤트가 하나도 없어 화면이 비어 있었다.
          처리중을 쓰면 네 단계가 회색 대기 상태로 먼저 보인다. */}
      {처리중 && (
        <div className="진행">
          {단계이름.map((이름, i) => {
            const 번호 = i + 1;
            const 완료 = 단계들.find((s) => s.번호 === 번호 && s.상태 === "완료");
            const 시작 = 단계들.some((s) => s.번호 === 번호);
            return (
              <div key={번호} className={완료 ? "끝" : 시작 ? "중" : "전"}>
                {완료 ? "✓" : 시작 ? "▶" : "　"} {번호}. {이름}
                {완료?.초 !== undefined && <span className="초"> {완료.초}초</span>}
              </div>
            );
          })}

          <div className="경과">
            {경과}초 경과
            {/* 30초는 "정상 범위를 벗어났다"는 눈금이다. 무료 티어에서
                모델 응답 중앙값이 41초까지 올라간 날이 있었다. */}
            {경과 >= 30 && (
              <span className="느림">
                {" — "}지금 모델 응답이 느립니다. 3분까지 걸릴 수 있습니다.
              </span>
            )}
          </div>
        </div>
      )}

      {결과 && <결과화면
        결과={결과}
        평가={평가} set평가={set평가}
        메모={메모} set메모={메모바꾸기}
        보냈다={보냈다} 저장중={저장중} 보내기={의견보내기} />}

      {지난것.length > 0 && (
        <details className="이력">
          <summary>이전 결과 {지난것.length}건</summary>
          {지난것.map((h) => (
            <div key={h.때} className="사례">
              <b>{세번표기(h.코드)}</b>
              <span className="초"> {h.확신도 ?? "—"} · {때표기(h.때)}</span>
              <div className="사례품명">{h.desc}</div>
            </div>
          ))}
          <div className="줄">
            <button className="보조"
                    onClick={() => { 이력비우기(); set이력([]); }}>
              목록 지우기
            </button>
          </div>
        </details>
      )}
      </main>

      <footer className="쪽 바닥">
        {잔여 && (
          <p className="사용량">
            남은 횟수 — 이 브라우저 <b>{잔여.세션남은}/{잔여.세션상한}</b>
            {" · "}오늘 전체 <b>{잔여.일일남은}/{잔여.일일상한}</b>
          </p>
        )}
      </footer>
    </>
  );
}

// 결과가 길어서 따로 뺐다. props 는 부모가 내려주는 값이다 —
// 자바로 치면 생성자 인자에 가깝고, **자식이 마음대로 못 바꾼다.**
// 바꿔야 하면 부모가 준 set... 함수를 부른다.
function 결과화면({ 결과, 평가, set평가, 메모, set메모, 보냈다, 저장중, 보내기 }: {
  결과: ClassifyOut;
  평가: "up" | "down" | null;
  set평가: (v: "up" | "down") => void;
  메모: string;
  set메모: (v: string) => void;
  보냈다: boolean;
  저장중: boolean;
  보내기: (평가: "up" | "down" | null, 메모: string) => void;
}) {
  if (결과.오류) return <div className="경고">{결과.오류}</div>;

  // 앞 6자리는 [3]재정렬이 정한 1순위다. 최종 코드에서 잘라 쓰지 않는 이유 —
  // 두 값이 어긋나면 그 사실이 화면에 드러나야 한다.
  const 앞6 = 결과.순위[0] ?? "";

  // 두 단계의 확인 포인트를 합치고 중복만 없앤다.
  // **Set 은 넣은 순서를 기억한다** — 파이썬 dict.fromkeys 와 같은 일을 하고,
  // [...집합] 은 그걸 다시 배열로 편다(스프레드).
  const 포인트 = [...new Set([...결과.확인포인트, ...결과.확정확인포인트])];

  function 엄지(v: "up" | "down") {
    set평가(v);
    보내기(v, 메모);          // 누르는 즉시 저장. 가장 값싼 신호를 놓치지 않는다
  }

  return (
    <div className="결과">
      <div className="코드">{세번표기(결과.코드)}</div>
      <div className="확신도">
        확신도 {확신도표기(결과.확신도)}
        {결과.확정확신도 && ` / 세번 확정 ${확신도표기(결과.확정확신도)}`}
        <span className="초"> · {결과.elapsed}초</span>
      </div>

      {/* **확신도는 한쪽 방향으로만 믿을 수 있다.** 봉인 30건을 두 번 열어
          얻은 비대칭이다 — 높음이 아닌 답 10건은 정답이 하나도 없었지만,
          높음인 답도 68% 만 정답이었다. 게다가 2회차에서 최종 코드는 30건
          전건 그대로였는데 오답 3건의 라벨만 보통 → 높음으로 올라갔다.
          라벨 자체가 흔들리므로 "높음 = 안전"으로 읽히게 두면 안 된다.

          **숫자는 빼지 않고 말투만 눕혔다.** "3건 중 1건은 오답" 대신
          "정답률 68%" 로 적는 식이다 — 같은 사실인데 겁을 주지 않는다.
          숫자까지 빼면 확신도가 보증처럼 읽히고, 그건 두 번의 열람이
          부정한 쪽이다(EXPERIMENTS.md:1119).

          app.py 는 동결이라 옛 문구가 남아 있다. 두 앱의 말이 갈리지만
          고치는 쪽은 이쪽 하나로 둔다. */}
      {결과.확신도 === "high" ? (
        <p className="경고 높음">
          모델이 <b>확신도 높음</b>으로 답했습니다.<br />
          시험 30건에서 확신도가 높음인 답의 정답률은 <b>68%</b>였습니다.<br />
          아래 확인 사항과 근거 결정례를 함께 봐 주세요.
        </p>
      ) : (
        <p className="경고 낮음">
          모델이 <b>확신도 {확신도표기(결과.확신도)}</b>으로 답했습니다.<br />
          시험 30건에서 확신도가 높음이 아닌 답 10건 중 <b>맞은 것은 없었습니다.</b><br />
          근거 결정례를 확인하시거나, 물품설명을 더 자세히 적어 다시 물어봐 주세요.
        </p>
      )}

      {/* **코드를 앞 6자리와 뒤 4자리로 나눠 보여준다.** 서로 다른 단계가 정한
          값이고 실무에서 틀리는 자리도 다르다 — baseline 에서 호 경합(6자리)
          41.7%, 한국 고유 세번(10자리) 25.0% 로 원인이 갈렸다.
          app.py:396-411 과 같은 구성이다. */}
      {(결과.top근거 || 결과.확정근거 || 결과.자동확정) && (
        <div className="근거">
          <b>왜 이 코드인가</b>
          <ul>
            {결과.top근거 && (
              <li>
                <b>앞 6자리 {앞6}</b> — {결과.top근거}
                {결과.top결정례 && 결과.top결정례 !== "없음" && (
                  <div className="사례품명">근거로 삼은 결정례: {결과.top결정례}</div>
                )}
              </li>
            )}
            {결과.확정근거 ? (
              <li><b>뒤 4자리</b> — {결과.확정근거}</li>
            ) : 결과.자동확정 ? (
              <li>
                <b>뒤 4자리</b> — 이 6자리 아래 신고 가능한 10자리가
                하나뿐이라 자동으로 정해졌습니다.
              </li>
            ) : null}
          </ul>
        </div>
      )}

      {포인트.length > 0 && (
        <div className="확인">
          <b>사람이 확인할 점</b>
          <ul>{포인트.map((p, i) => <li key={i}>{p}</li>)}</ul>
        </div>
      )}

      {/* 선택지가 하나뿐이면 고른 게 아니라 정해진 것이므로 안 띄운다. */}
      {!결과.자동확정 && 결과.선택지수 > 0 && (
        <p className="캡션">
          10자리 선택지 {결과.선택지수}개 중에서 골랐습니다.
          {결과.확정확신도 && ` (세번 확정 확신도 ${확신도표기(결과.확정확신도)})`}
        </p>
      )}

      <details>
        <summary>6자리 후보 3개와 판단 근거</summary>
        {결과.ranked.length > 0 ? (
          결과.ranked.map((r, i) => (
            <div key={i} className="사례">
              <b>{i + 1}. {r.code}</b>{r.reason && <> — {r.reason}</>}
              <div className="사례품명">근거 결정례: {r.근거결정례 || "없음"}</div>
            </div>
          ))
        ) : (
          // 재정렬이 ranked 를 못 준 경우다. 순위 배열은 그때도 채워진다.
          <div className="사례">{결과.순위.join(" · ") || "—"}</div>
        )}
      </details>

      <details>
        <summary>근거 결정례 {결과.결정례.length}건</summary>
        {결과.결정례.map((h) => (
          <div key={h.참조번호} className="사례">
            <b>{세번표기(h.결정세번)}</b> · {h.참조번호} · 유사도 {h.score.toFixed(3)}
            <div className="사례품명">{h.품명}</div>
          </div>
        ))}
      </details>

      {/* **이 문장을 빼지 않는다.** 이 도구는 검증 보조지 판정이 아니다.
          app.py:525-527 과 같은 문장을 쓴다. */}
      <p className="참고">
        <b>참고용입니다.</b> 신고 전 관세사 확인 또는 관세청 품목분류
        사전심사를 받으세요.
      </p>

      <div className="피드백">
        <b>이 결과가 맞았나요?</b>
        <div className="줄">
          <button className={평가 === "up" ? "고름" : ""} disabled={저장중}
                  onClick={() => 엄지("up")}>👍</button>
          <button className={평가 === "down" ? "고름" : ""} disabled={저장중}
                  onClick={() => 엄지("down")}>👎</button>
        </div>
        <input
          value={메모}
          onChange={(e) => set메모(e.target.value)}
          disabled={저장중}
          placeholder="틀렸다면 정답이나 이유를 적어주세요 (선택)"
        />
        <div className="줄">
          <button onClick={() => 보내기(평가, 메모)}
                  disabled={저장중 || (!평가 && !메모.trim())}>
            {저장중 ? "보내는 중…" : "의견 보내기"}
          </button>
        </div>

        {/* **저장됐는지 확실히 보이게 한다.** 버튼 옆 작은 글씨로는
            눌렀는지 안 눌렀는지 알 수 없다는 지적을 받았다.
            줄 아래 초록 상자로 빼고, 메모를 고치면 다시 사라진다. */}
        {보냈다 && (
          <p className="보냄">
            ✓ 저장했습니다. 감사합니다.
            {평가 && <> — {평가 === "up" ? "맞음" : "틀림"}
              {메모.trim() && " · 메모 함께 저장"}</>}
          </p>
        )}
      </div>
    </div>
  );
}
