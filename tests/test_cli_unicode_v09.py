from __future__ import annotations

from io import BytesIO, TextIOWrapper
import json

from company_os import cli


def test_cli_reconfigures_windows_style_text_stream_to_utf8(monkeypatch):
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp949", newline="\n")
    monkeypatch.setattr(cli.sys, "stdout", stream)

    cli._configure_text_streams()
    cli._print({"speech": "구조 — 사용자 흐름"})
    stream.flush()

    rendered = raw.getvalue().decode("utf-8")
    assert json.loads(rendered)["speech"] == "구조 — 사용자 흐름"
