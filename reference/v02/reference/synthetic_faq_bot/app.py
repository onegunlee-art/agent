"""합성 FAQ 챗봇 (WorkOrder D) — 실제 모델이 '수정'할 대상 산출물.

설계 원칙
- 모델을 쓰지 않는 결정적 챗봇: 같은 질문 → 같은 답. 평가가 흔들리지 않는다.
- 자료(faq_data.json) 밖 질문은 추측하지 않고 정해진 거절문으로 답한다.
- 답에는 항상 출처(source)를 붙인다.
- internal_note 등 비공개 필드는 어떤 경로로도 출력하지 않는다.
- 서버는 127.0.0.1에만 바인딩한다 (외부 공개 금지).

실행:  python app.py --data faq_data.json --port 8765
평가:  python run_eval.py   (서버 없이 answer()를 직접 호출)
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MATCH_THRESHOLD = 0.3    # 이보다 낮으면 거절. 키워드 1회 일치 = 1.0, 어절 겹침은 최대 0.5.
PUBLIC_ITEM_FIELDS = ("id", "question", "answer", "source")
STOPWORDS = {"어떻게", "되나요", "하나요", "있나요", "인가요", "가능한가요", "가능해요", "돼요",
             "되요", "알려주세요", "알려", "주세요", "혹시", "좀", "저", "제가", "요"}


def load_data(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _clean(text: str) -> str:
    return re.sub(r"[^\w가-힣]+", " ", (text or "").lower()).strip()


def _words(text: str) -> set[str]:
    return {w for w in _clean(text).split() if w not in STOPWORDS}


def _score(question: str, item: dict) -> float:
    """1순위: 키워드가 질문에 부분 문자열로 포함 (공백 무시). 2순위: 어절 겹침 비율."""
    q_compact = _clean(question).replace(" ", "")
    if not q_compact:
        return 0.0
    hits = sum(1 for k in item.get("keywords", []) if _clean(k).replace(" ", "") in q_compact)
    qw = _words(question)
    coverage = len(qw & _words(item["question"])) / len(qw) if qw else 0.0
    return hits + 0.5 * coverage


def answer(question: str, data: dict) -> dict:
    """반환 형식은 rubric_verifier의 answer 형식과 같다."""
    best, best_score = None, 0.0
    for item in data["items"]:
        s = _score(question, item)
        if s > best_score:
            best, best_score = item, s
    if best is None or best_score < MATCH_THRESHOLD:
        return {"text": data["refusal_text"], "sources": [], "refused": True,
                "matched_id": None, "score": round(best_score, 3)}
    return {
        "text": f"{best['answer']}\n\n출처: {best['source']}",
        "sources": [best["id"]],
        "refused": False,
        "matched_id": best["id"],
        "score": round(best_score, 3),
    }


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} 상담 챗봇 (합성 미리보기)</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem}}
 #log{{border:1px solid #ccc;border-radius:8px;padding:1rem;min-height:200px;white-space:pre-wrap}}
 .q{{color:#555;margin-top:.8rem}} .a{{margin:.3rem 0 0 .5rem}} .src{{font-size:.85em;color:#777}}
 form{{display:flex;gap:.5rem;margin-top:1rem}} input{{flex:1;padding:.6rem}}
</style></head><body>
<h2>{name} 상담 챗봇 <small>(합성 고객 미리보기)</small></h2>
<div id="log"></div>
<form id="f"><input id="q" placeholder="질문을 입력하세요" autocomplete="off"><button>질문</button></form>
<script>
const log=document.getElementById('log');
document.getElementById('f').addEventListener('submit',async e=>{{
  e.preventDefault(); const q=document.getElementById('q').value.trim(); if(!q)return;
  log.insertAdjacentHTML('beforeend',`<div class="q">Q. ${{q}}</div>`);
  const r=await fetch('/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q}})}});
  const d=await r.json();
  log.insertAdjacentHTML('beforeend',`<div class="a">${{d.text.replace(/\\n/g,'<br>')}}</div>`);
  document.getElementById('q').value='';
}});
</script></body></html>"""


def make_handler(data: dict):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            elif self.path == "/":
                self._send(200, PAGE.format(name=data["customer_name"]).encode("utf-8"),
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path != "/ask":
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                question = str(payload.get("question", ""))[:500]
            except json.JSONDecodeError:
                return self._send(400, b'{"error":"bad json"}', "application/json")
            body = json.dumps(answer(question, data), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")

        def log_message(self, *_):  # 조용히
            pass

    return Handler


def serve(data_path: str, port: int) -> None:
    data = load_data(data_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data))
    print(f"미리보기: http://127.0.0.1:{port}  (Ctrl+C로 종료)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(Path(__file__).with_name("faq_data.json")))
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    serve(a.data, a.port)
