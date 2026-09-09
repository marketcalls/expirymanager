"""The console entry point: argument surface, startup ordering and the fixed bind address."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from expirymanager import __main__ as entry
from expirymanager.version import __version__


class TestFixedBindAddress:
    def test_host_port_and_scheme_are_not_configurable(self) -> None:
        # Fyers matches the registered redirect URI https://127.0.0.1:8000/fyers/callback
        # exactly, so any of these being settable can only ever produce a broken OAuth flow.
        assert entry.HOST == "127.0.0.1"
        assert entry.PORT == 8000

        parser = entry.build_parser()
        flags = {action.dest for action in parser._actions}
        assert "host" not in flags
        assert "port" not in flags

    def test_serve_passes_the_certificate_paths_to_uvicorn(self, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        class FakeUvicorn:
            @staticmethod
            def run(app: str, **kwargs: object) -> None:
                captured["app"] = app
                captured.update(kwargs)

        import sys

        monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

        key = tmp_path / "server.key"
        cert = tmp_path / "server.crt"
        entry.serve((key, cert))

        assert captured["app"] == "expirymanager.app:create_app"
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8000
        assert captured["workers"] == 1
        assert captured["ssl_keyfile"] == str(key)
        assert captured["ssl_certfile"] == str(cert)
        # uvicorn's own dictConfig would replace the redaction filters on the root handlers.
        assert captured["log_config"] is None

    def test_serve_without_a_certificate_omits_the_ssl_arguments(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeUvicorn:
            @staticmethod
            def run(app: str, **kwargs: object) -> None:
                captured.update(kwargs)

        import sys

        monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
        entry.serve(None)

        assert "ssl_keyfile" not in captured
        assert "ssl_certfile" not in captured


class TestCheckRun:
    def test_check_prepares_the_directory_and_exits_zero(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        import logging

        root = logging.getLogger()
        saved = list(root.handlers)
        try:
            data_dir = tmp_path / "data"
            code = entry.main(["--check", "--data-dir", str(data_dir)])
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

        assert code == 0
        assert data_dir.is_dir()
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
        assert (data_dir / "logs").is_dir()

        out = capsys.readouterr().out
        assert __version__ in out
        assert str(data_dir) in out
        assert "self-signed" in out or "no certificate" in out

    def test_a_cloud_sync_root_is_refused_with_exit_code_one(
        self, tmp_path: Path, capsys
    ) -> None:
        code = entry.main(["--check", "--data-dir", str(tmp_path / "Dropbox" / "data")])
        assert code == 1
        assert "Dropbox" in capsys.readouterr().err

    def test_version_flag_exits_zero(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            entry.build_parser().parse_args(["--version"])
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestBanner:
    def test_the_banner_is_plain_text(self) -> None:
        # The dash characters are written as escapes so this file itself stays plain ASCII.
        em_dash, en_dash = chr(0x2014), chr(0x2013)
        for banner in (entry.TLS_BANNER, entry.NO_TLS_BANNER):
            assert banner.isascii()
            assert em_dash not in banner
            assert en_dash not in banner

    def test_the_self_signed_warning_says_it_is_expected(self) -> None:
        assert "self-signed" in entry.TLS_BANNER
        assert "expected" in entry.TLS_BANNER
