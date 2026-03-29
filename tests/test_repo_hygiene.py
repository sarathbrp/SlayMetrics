"""Tests for tools/check_repo_hygiene.py."""

from __future__ import annotations

from pathlib import Path

from tools.check_repo_hygiene import (
    _check_file,
    _is_text_file,
    _should_skip_secret_value,
    main,
)


def test_is_text_file_py():
    assert _is_text_file(Path("foo.py"))


def test_is_text_file_unknown():
    assert not _is_text_file(Path("foo.bin"))


def test_is_text_file_config_yaml():
    assert _is_text_file(Path("config.yaml"))


def test_should_skip_short_secret():
    assert _should_skip_secret_value("abc")


def test_should_skip_placeholder():
    assert _should_skip_secret_value("your-api-key-here-placeholder")


def test_should_not_skip_real_looking_secret():
    assert not _should_skip_secret_value("sk-abc123def456xyz789")


def test_check_file_nonexistent(tmp_path):
    result = _check_file(str(tmp_path / "nope.py"))
    assert result == []


def test_check_file_clean(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n")
    assert _check_file(str(f)) == []


def test_check_file_detects_raw_ip_url(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('url = "http://10.0.0.5:8080/api"\n')
    errors = _check_file(str(f))
    assert any("raw IP URL blocked" in e for e in errors)


def test_check_file_allows_localhost_ip(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text('url = "http://127.0.0.1:8080/api"\n')
    assert _check_file(str(f)) == []


def test_check_file_detects_bearer_token(tmp_path):
    f = tmp_path / "tok.py"
    f.write_text('auth = "Bearer sk-abcdefghijklmnopqrstuvwxyz"\n')
    errors = _check_file(str(f))
    assert any("bearer token" in e for e in errors)


def test_check_file_detects_secret_assignment(tmp_path):
    f = tmp_path / "sec.yaml"
    f.write_text("API_KEY: sk_live_abcdef123456\n")
    errors = _check_file(str(f))
    assert any("secret-like assignment" in e for e in errors)


def test_check_file_skips_env_key(tmp_path):
    f = tmp_path / "ok.yaml"
    f.write_text("API_KEY_ENV: SOME_VARIABLE\n")
    assert _check_file(str(f)) == []


def test_check_file_skips_comments(tmp_path):
    f = tmp_path / "ok.yaml"
    f.write_text("# API_KEY: sk_live_abcdef123456\n")
    assert _check_file(str(f)) == []


def test_main_clean(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n")
    assert main(["prog", str(f)]) == 0


def test_main_dirty(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('url = "http://10.0.0.5:8080/api"\n')
    assert main(["prog", str(f)]) == 1
