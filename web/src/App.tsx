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
import type { ClassifyOut, GateOut, 단계이벤트 } from "./types";
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
      const r = await api.분류(
        {
          desc: desc.trim(),
          강행,
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
    } catch (e) {
      오류처리(e);
    }
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

      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        disabled={처리중 || 잠김}
        rows={6}
        placeholder="예: 폴리에스터 100% 직물로 만든 성인용 반팔 티셔츠. 무게 180g/m2, 편물이 아닌 직물."
      />

      {/* .줄 은 오른쪽 정렬이다. 그래서 **남은 횟수를 버튼보다 먼저** 둔다 —
          CSS 로 순서를 뒤집을 수도 있지만, 그러면 눈에 보이는 순서와
          Tab 키가 도는 순서가 어긋난다. */}
      <div className="줄">
        {결과 && <span className="남은">남은 횟수 {결과.남은횟수}회</span>}
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

      {단계들.length > 0 && !결과 && (
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
        </div>
      )}

      {결과 && <결과화면
        결과={결과}
        평가={평가} set평가={set평가}
        메모={메모} set메모={메모바꾸기}
        보냈다={보냈다} 저장중={저장중} 보내기={의견보내기} />}
      </main>
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

  function 엄지(v: "up" | "down") {
    set평가(v);
    보내기(v, 메모);          // 누르는 즉시 저장. 가장 값싼 신호를 놓치지 않는다
  }

  return (
    <div className="결과">
      <div className="코드">{세번표기(결과.코드)}</div>
      <div className="확신도">
        확신도 {결과.확신도 ?? "—"}
        {결과.확정확신도 && ` / 세번 확정 ${결과.확정확신도}`}
        <span className="초"> · {결과.elapsed}초</span>
      </div>

      {/* **확신도는 한쪽 방향으로만 믿을 수 있다.** 봉인 30건을 두 번 열어
          얻은 비대칭이다 — high 가 아닌 답 10건은 전부 오답이었지만, high 인
          답도 65.4% 만 정답이었다. 게다가 2회차에서 최종 코드는 30건 전건
          그대로였는데 오답 3건의 라벨만 medium → high 로 올라갔다.
          라벨 자체가 흔들리므로 "high = 안전"으로 읽히게 두면 안 된다.
          app.py:382-393 과 **같은 문구를 쓴다** — 두 화면이 다른 말을 하면
          어느 쪽을 믿어야 할지 알 수 없다. */}
      {결과.확신도 === "high" ? (
        <p className="경고 높음">
          모델이 <b>확신도 high</b> 로 답했습니다.<br />
          다만 시험 30건에서 high 인 답도 <b>3건 중 1건은 오답</b>이었습니다.<br />
          아래 확인 사항과 근거 결정례를 보세요.
        </p>
      ) : (
        <p className="경고 낮음">
          모델이 <b>확신도 {결과.확신도 ?? "미상"}</b> 로 답했습니다.{" "}
          <b>이 답은 틀렸을 가능성이 높습니다.</b><br />
          시험 30건을 두 번 돌렸을 때 high 가 아닌 답 10건은 <b>전부 오답</b>이었습니다.
        </p>
      )}

      {결과.top근거 && <p className="근거"><b>순위 근거</b><br />{결과.top근거}</p>}
      {결과.확정근거 && <p className="근거"><b>세번 근거</b><br />{결과.확정근거}</p>}

      {결과.확인포인트.length > 0 && (
        <div className="확인">
          <b>사람이 확인할 점</b>
          <ul>{결과.확인포인트.map((p, i) => <li key={i}>{p}</li>)}</ul>
        </div>
      )}

      <details>
        <summary>근거 결정례 {결과.결정례.length}건</summary>
        {결과.결정례.map((h) => (
          <div key={h.참조번호} className="사례">
            <b>{세번표기(h.결정세번)}</b> · {h.참조번호} · 유사도 {h.score.toFixed(3)}
            <div className="사례품명">{h.품명}</div>
          </div>
        ))}
      </details>

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
