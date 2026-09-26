"""Deterministic, localhost-only FAQ preview used by the V0.2 evaluation cycle."""

from __future__ import annotations

import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .rubric import RubricReport, run_rubric

MATCH_THRESHOLD = 0.3
STOPWORDS = {
    "어떻게", "되나요", "하나요", "있나요", "인가요", "가능한가요", "가능해요",
    "돼요", "되요", "알려주세요", "알려", "주세요", "혹시", "좀", "저", "제가", "요",
}


def load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("FAQ data must be a JSON object")
    return payload


def _clean(text: str) -> str:
    return re.sub(r"[^\w가-힣]+", " ", (text or "").lower()).strip()


def _words(text: str) -> set[str]:
    return {word for word in _clean(text).split() if word not in STOPWORDS}


def _score(question: str, item: dict[str, Any]) -> float:
    compact = _clean(question).replace(" ", "")
    if not compact:
        return 0.0
    keyword_hits = sum(
        1
        for keyword in item.get("keywords", [])
        if _clean(str(keyword)).replace(" ", "") in compact
    )
    words = _words(question)
    coverage = len(words & _words(str(item["question"]))) / len(words) if words else 0
    return keyword_hits + 0.5 * coverage


def answer(question: str, data: dict[str, Any]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in data["items"]:
        score = _score(question, item)
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < MATCH_THRESHOLD:
        return {
            "text": data["refusal_text"],
            "answer_text": data["refusal_text"],
            "source_details": [],
            "sources": [],
            "refused": True,
            "matched_id": None,
            "score": round(best_score, 3),
        }
    return {
        "text": f"{best['answer']}\n\n출처: {best['source']}",
        "answer_text": best["answer"],
        "source_details": [best["source"]],
        "sources": [best["id"]],
        "refused": False,
        "matched_id": best["id"],
        "score": round(best_score, 3),
    }


def evaluate(
    cases_path: str | Path,
    data_path: str | Path,
    *,
    require_approved: bool = False,
    approved_spec_sha256: str | None = None,
) -> RubricReport:
    source = Path(cases_path).resolve()
    if require_approved:
        if approved_spec_sha256 is None:
            raise RuntimeError(
                "official evaluation requires canonical CEO APPROVED SHA-256"
            )
        actual_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_sha256 != approved_spec_sha256:
            raise RuntimeError("evaluation cases changed after CEO approval")
    spec = load_json(source)
    data = load_json(data_path)
    answers = {case["id"]: answer(case["question"], data) for case in spec["cases"]}
    return run_rubric(
        spec["cases"],
        answers,
        float(spec.get("threshold", 0.9)),
        spec.get("critical_forbidden", []),
    )


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>합성 고객 챗봇 미리보기</title><style>
body{font-family:system-ui,sans-serif;max-width:700px;margin:2rem auto;padding:0 1rem}
#log{border:1px solid #bbb;border-radius:10px;min-height:220px;padding:1rem;white-space:pre-wrap}
form{display:flex;gap:.5rem;margin-top:1rem}input{flex:1;padding:.7rem}</style></head>
<body><h1 id="title"></h1><p>로컬 합성 데이터 전용 미리보기입니다.</p><div id="log"></div>
<form id="form"><input id="question" maxlength="500" placeholder="질문을 입력하세요"><button>질문</button></form>
<script>const title=document.getElementById('title'),log=document.getElementById('log');let sourceCount=0;
title.textContent=%CUSTOMER%;document.getElementById('form').addEventListener('submit',async(e)=>{
e.preventDefault();const input=document.getElementById('question'),q=input.value.trim();if(!q)return;
const qn=document.createElement('p');qn.textContent='Q. '+q;log.appendChild(qn);
const response=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
const data=await response.json(),an=document.createElement('p');an.textContent=data.answer_text;log.appendChild(an);
if(!data.refused&&data.source_details.length){
const button=document.createElement('button'),sources=document.createElement('div');
sources.id='sources-'+(++sourceCount);button.type='button';button.textContent='출처 보기';button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls',sources.id);
sources.hidden=true;data.source_details.forEach(detail=>{const item=document.createElement('p');item.textContent=detail;sources.appendChild(item);});
button.addEventListener('click',()=>{const expanded=button.getAttribute('aria-expanded')==='true';button.setAttribute('aria-expanded',String(!expanded));sources.hidden=expanded;button.textContent=expanded?'출처 보기':'출처 접기';});
log.appendChild(button);log.appendChild(sources);}
input.value='';});</script>
</body></html>"""


def make_handler(data: dict[str, Any]):
    page = PAGE.replace("%CUSTOMER%", json.dumps(data["customer_name"], ensure_ascii=False))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            elif self.path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/ask":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                payload = json.loads(self.rfile.read(length) or b"{}")
                question = str(payload.get("question", ""))[:500]
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"bad request"}', "application/json")
                return
            body = json.dumps(answer(question, data), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")

        def log_message(self, *_: object) -> None:
            return

    return Handler


def create_server(data_path: str | Path, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(load_json(data_path)))


def serve(data_path: str | Path, port: int = 8765) -> None:
    server = create_server(data_path, port)
    print(f"preview=http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
