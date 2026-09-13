"""Lane credential resolution, the dotenv loader and the auth probe.

A credential VALUE never appears in output; only env var NAMES are printed.
"""

from __future__ import annotations

import logging

import pytest

from harness.credentials import (
    LANES,
    load_env_file,
    main,
    probe_lane,
    resolve_credentials,
)

PRODUCT_ENV = {"LILY_TOKEN": "tok-product", "LILY_BASE_URL": "https://p.example/v1"}
RESEARCH_ENV = {"JUDGE_GENERATOR_TOKEN": "tok-research"}


# --- load_env_file -------------------------------------------------------


def test_load_env_file_reads_pairs(tmp_path):
    """Quoted and bare values both load; the file is dotenv-shaped."""
    f = tmp_path / ".env"
    f.write_text('A=1\nB="two"\nC=\'three\'\n', encoding="utf-8")
    env: dict[str, str] = {}
    load_env_file(f, env)
    assert env == {"A": "1", "B": "two", "C": "three"}


def test_load_env_file_skips_comments_and_junk(tmp_path):
    """Comments, blanks and lines without '=' are ignored, not errors."""
    f = tmp_path / ".env"
    f.write_text("# a comment\n\nNOT_A_PAIR\n  D = 4 \n", encoding="utf-8")
    env: dict[str, str] = {}
    load_env_file(f, env)
    assert env == {"D": "4"}


def test_load_env_file_never_overrides_the_environment(tmp_path):
    """An exported value wins over the file — the shell stays authoritative."""
    f = tmp_path / ".env"
    f.write_text("A=from-file\n", encoding="utf-8")
    env = {"A": "from-shell"}
    load_env_file(f, env)
    assert env["A"] == "from-shell"


def test_load_env_file_missing_is_a_no_op(tmp_path):
    """A repo without a .env still runs; the loader simply does nothing."""
    env: dict[str, str] = {}
    load_env_file(tmp_path / "absent.env", env)
    assert env == {}


# --- resolve_credentials -------------------------------------------------


def test_resolve_credentials_returns_token_and_lane_base_url():
    assert resolve_credentials("product", PRODUCT_ENV) == (
        "tok-product", "https://p.example/v1"
    )


def test_resolve_credentials_falls_back_to_generic_base_url():
    """A lane without its own base URL uses LLM_BASE_URL — but the TOKEN
    never falls back; only the URL does."""
    env = dict(RESEARCH_ENV, LLM_BASE_URL="https://generic.example/v1")
    token, base = resolve_credentials("research", env)
    assert token == "tok-research"
    assert base == "https://generic.example/v1"


def test_resolve_credentials_base_url_is_none_when_nothing_is_configured():
    token, base = resolve_credentials("research", RESEARCH_ENV)
    assert token == "tok-research"
    assert base is None


def test_missing_lane_token_names_the_var_but_never_a_value():
    """The error must be actionable and leak nothing."""
    env = {"LLM_API_KEY": "some-other-secret"}
    with pytest.raises(RuntimeError) as exc:
        resolve_credentials("product", env)
    message = str(exc.value)
    assert "LILY_TOKEN" in message
    assert "some-other-secret" not in message


def test_unknown_lane_is_rejected():
    with pytest.raises(ValueError, match="unknown lane"):
        resolve_credentials("marketing", PRODUCT_ENV)


def test_resolution_logs_the_var_name_not_the_token(caplog):
    with caplog.at_level(logging.INFO, logger="harness.credentials"):
        resolve_credentials("product", PRODUCT_ENV)
    assert "LILY_TOKEN" in caplog.text
    assert "tok-product" not in caplog.text


def test_every_declared_lane_resolves():
    """LANES and the internal lane map cannot drift apart unnoticed."""
    env = {**PRODUCT_ENV, **RESEARCH_ENV}
    for lane in LANES:
        assert resolve_credentials(lane, env)[0]


# --- probe_lane ----------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_probe_lane_sends_no_content_and_returns_the_base(monkeypatch):
    """The probe is a bare GET /models with a bearer header — no payload."""
    import httpx

    seen: dict = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _Resp(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    base = probe_lane("product", env=PRODUCT_ENV)
    assert base == "https://p.example/v1"
    assert seen["url"] == "https://p.example/v1/models"
    assert seen["kwargs"]["headers"] == {"Authorization": "Bearer tok-product"}
    assert "data" not in seen["kwargs"] and "json" not in seen["kwargs"]


def test_probe_lane_raises_on_non_200(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(401))
    with pytest.raises(RuntimeError, match="401"):
        probe_lane("product", env=PRODUCT_ENV)


def test_probe_lane_raises_on_transport_failure(monkeypatch):
    import httpx

    def boom(url, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(RuntimeError, match="transport"):
        probe_lane("product", env=PRODUCT_ENV)


def test_probe_lane_uses_the_client_default_base_when_unconfigured(monkeypatch):
    import httpx

    from harness.client import DEFAULT_BASE_URL

    seen: dict = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert probe_lane("research", env=RESEARCH_ENV) == DEFAULT_BASE_URL
    assert seen["url"] == f"{DEFAULT_BASE_URL}/models"


# --- the CLI -------------------------------------------------------------


def test_main_reports_presence_without_the_value(monkeypatch, capsys):
    monkeypatch.setenv("LILY_TOKEN", "tok-product")
    assert main(["product"]) == 0
    out = capsys.readouterr().out
    assert "token present (LILY_TOKEN)" in out
    assert "tok-product" not in out


def test_main_returns_one_when_the_token_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("LILY_TOKEN", raising=False)
    assert main(["product"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_check_runs_the_probe(monkeypatch, capsys):
    import httpx

    monkeypatch.setenv("LILY_TOKEN", "tok-product")
    monkeypatch.setenv("LILY_BASE_URL", "https://p.example/v1")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(200))
    assert main(["product", "--check"]) == 0
    assert "auth OK" in capsys.readouterr().out


def test_main_check_reports_a_failed_probe(monkeypatch, capsys):
    import httpx

    monkeypatch.setenv("LILY_TOKEN", "tok-product")
    monkeypatch.setenv("LILY_BASE_URL", "https://p.example/v1")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(500))
    assert main(["product", "--check"]) == 1
    assert "FAILED" in capsys.readouterr().out
