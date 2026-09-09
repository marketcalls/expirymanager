"""Data directory resolution, permissions, the umask ordering rule and the advisory lock."""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest

from expirymanager import paths as paths_module
from expirymanager.paths import (
    DIR_MODE,
    UMASK,
    CloudSyncRootError,
    InstanceLock,
    Paths,
    PathsError,
    SingleInstanceError,
    assert_private_file,
    ensure,
    is_cloud_sync_root,
    resolve,
)

MAIN_MODULE = Path(__file__).resolve().parents[1] / "expirymanager" / "__main__.py"


@pytest.fixture
def restore_umask():
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


class TestUmaskOrdering:
    """The umask must be set before anything can create a file, not repaired afterwards."""

    def test_umask_is_the_first_executable_statement_of_the_entry_point(self) -> None:
        tree = ast.parse(MAIN_MODULE.read_text(encoding="utf-8"))
        body = list(tree.body)

        assert isinstance(body[0], ast.Expr)
        assert isinstance(body[0].value, ast.Constant)
        assert isinstance(body[0].value.value, str), "the module docstring must come first"

        assert isinstance(body[1], ast.Import)
        assert [alias.name for alias in body[1].names] == ["os"]

        umask_call = body[2]
        assert isinstance(umask_call, ast.Expr)
        assert isinstance(umask_call.value, ast.Call)
        func = umask_call.value.func
        assert isinstance(func, ast.Attribute)
        assert func.attr == "umask"
        assert isinstance(func.value, ast.Name)
        assert func.value.id == "os"
        assert umask_call.value.args[0].value == UMASK

    def test_no_other_import_precedes_the_umask(self) -> None:
        tree = ast.parse(MAIN_MODULE.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                break
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                assert names == ["os"], f"import of {names} precedes the umask call"

    def test_set_process_umask_returns_the_previous_value(self, restore_umask) -> None:
        os.umask(0o022)
        previous = paths_module.set_process_umask()
        assert previous == 0o022
        current = os.umask(0o000)
        assert current == UMASK

    def test_ensure_sets_the_umask_before_creating_anything(
        self, tmp_path: Path, restore_umask
    ) -> None:
        os.umask(0o000)
        paths = ensure(tmp_path / "data", ensure_tls=False)

        # A file created afterwards with a permissive mode is masked down to 0600 by the umask
        # alone. That is the property SQLite's sidecars depend on, since nothing ever chmods them.
        sidecar = paths.sqlite_db.with_suffix(".sqlite3-wal")
        fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT, 0o666)
        os.close(fd)
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


class TestDirectoryModes:
    def test_every_directory_is_created_0700(self, tmp_path: Path, restore_umask) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        for directory in paths.directories():
            assert directory.is_dir(), directory
            assert stat.S_IMODE(directory.stat().st_mode) == DIR_MODE, directory

    def test_a_pre_existing_loose_directory_is_tightened(
        self, tmp_path: Path, restore_umask
    ) -> None:
        root = tmp_path / "data"
        root.mkdir(mode=0o755)
        root.chmod(0o755)

        paths = ensure(root, ensure_tls=False)
        assert stat.S_IMODE(paths.root.stat().st_mode) == DIR_MODE

    def test_ensure_is_idempotent(self, tmp_path: Path, restore_umask) -> None:
        first = ensure(tmp_path / "data", ensure_tls=False)
        second = ensure(tmp_path / "data", ensure_tls=False)
        assert first == second
        assert stat.S_IMODE(second.root.stat().st_mode) == DIR_MODE


class TestPathLayout:
    def test_children_sit_under_the_root(self, tmp_path: Path) -> None:
        paths = resolve(tmp_path / "data")
        assert paths.sqlite_db.name == "config.sqlite3"
        assert paths.duckdb_file.name == "market.duckdb"
        assert paths.master_key.name == "master.key"
        assert paths.lock_file.name == "expirymanager.lock"
        assert paths.tls_key == paths.root / "tls" / "server.key"
        assert paths.tls_cert == paths.root / "tls" / "server.crt"
        assert paths.log_file == paths.root / "logs" / "expirymanager.log"
        for child in (paths.exports_dir, paths.raw_dir, paths.logs_dir, paths.tmp_dir):
            assert child.parent == paths.root

    def test_raw_day_dir_is_zero_padded(self, tmp_path: Path) -> None:
        paths = resolve(tmp_path / "data")
        assert paths.raw_day_dir(2026, 3, 7) == paths.raw_dir / "2026" / "03" / "07"

    def test_default_root_honours_the_home_override(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(paths_module.HOME_ENV_VAR, str(tmp_path / "elsewhere"))
        assert paths_module.default_root() == tmp_path / "elsewhere"

    def test_default_root_is_the_dotted_home_directory(self, monkeypatch) -> None:
        monkeypatch.delenv(paths_module.HOME_ENV_VAR, raising=False)
        assert paths_module.default_root() == Path.home() / ".expirymanager"


class TestCloudSyncRefusal:
    @pytest.mark.parametrize(
        "fragment",
        [
            "Dropbox",
            "OneDrive - Contoso",
            "Google Drive",
            "Library/CloudStorage/GoogleDrive-user",
            "Library/Mobile Documents/com~apple~CloudDocs",
        ],
    )
    def test_known_sync_roots_are_detected(self, tmp_path: Path, fragment: str) -> None:
        candidate = tmp_path / fragment / ".expirymanager"
        assert is_cloud_sync_root(candidate) is not None

    def test_a_plain_directory_is_accepted(self, tmp_path: Path) -> None:
        assert is_cloud_sync_root(tmp_path / "work" / ".expirymanager") is None

    def test_ensure_refuses_and_creates_nothing(self, tmp_path: Path, restore_umask) -> None:
        root = tmp_path / "Dropbox" / ".expirymanager"
        with pytest.raises(CloudSyncRootError) as excinfo:
            ensure(root, ensure_tls=False)

        assert "Dropbox" in str(excinfo.value)
        assert paths_module.HOME_ENV_VAR in str(excinfo.value)
        assert not root.exists(), "the directory must not be left behind on a refusal"

    def test_cloud_sync_error_is_a_paths_error(self) -> None:
        assert issubclass(CloudSyncRootError, PathsError)


class TestPrivateFileAssertion:
    def test_a_0600_file_passes(self, tmp_path: Path) -> None:
        secret = tmp_path / "master.key"
        secret.write_bytes(b"synthetic")
        secret.chmod(0o600)
        assert_private_file(secret)

    def test_a_group_readable_file_is_refused(self, tmp_path: Path) -> None:
        secret = tmp_path / "master.key"
        secret.write_bytes(b"synthetic")
        secret.chmod(0o640)
        with pytest.raises(PathsError) as excinfo:
            assert_private_file(secret)
        assert "0640" in str(excinfo.value)

    def test_the_message_never_contains_the_file_contents(self, tmp_path: Path) -> None:
        secret = tmp_path / "master.key"
        secret.write_bytes(b"synthetic-key-material")
        secret.chmod(0o644)
        with pytest.raises(PathsError) as excinfo:
            assert_private_file(secret)
        assert "synthetic-key-material" not in str(excinfo.value)


class TestInstanceLock:
    def test_the_lock_file_is_created_0600(self, tmp_path: Path, restore_umask) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        with InstanceLock(paths.lock_file):
            assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600

    def test_a_second_holder_is_refused(self, tmp_path: Path, restore_umask) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        with InstanceLock(paths.lock_file):
            with pytest.raises(SingleInstanceError) as excinfo:
                InstanceLock(paths.lock_file).acquire()
        message = str(excinfo.value)
        assert "already" in message.lower() or "holds" in message.lower()

    def test_the_lock_is_reacquirable_after_release(
        self, tmp_path: Path, restore_umask
    ) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        lock = InstanceLock(paths.lock_file).acquire()
        lock.release()
        second = InstanceLock(paths.lock_file).acquire()
        second.release()

    def test_release_is_idempotent(self, tmp_path: Path, restore_umask) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        lock = InstanceLock(paths.lock_file).acquire()
        lock.release()
        lock.release()


class TestTlsSeam:
    """`security/tls.py` owns certificate generation. This asserts only the seam."""

    @staticmethod
    def _install_stub_tls(monkeypatch, stub_module) -> None:
        # `from expirymanager.security import tls` resolves through the package attribute once
        # the real module has been imported, so sys.modules alone is not enough to stub it.
        import sys

        from expirymanager import security as security_pkg

        if stub_module is None:
            monkeypatch.delattr(security_pkg, "tls", raising=False)
            monkeypatch.setitem(sys.modules, "expirymanager.security.tls", None)
            return
        monkeypatch.setattr(security_pkg, "tls", stub_module, raising=False)
        monkeypatch.setitem(sys.modules, "expirymanager.security.tls", stub_module)

    @staticmethod
    def _stub(func) -> object:
        import types

        module = types.ModuleType("expirymanager.security.tls")
        module.ensure_tls_material = func
        return module

    def test_an_unimportable_module_returns_none_rather_than_raising(
        self, tmp_path: Path, restore_umask, monkeypatch
    ) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)
        self._install_stub_tls(monkeypatch, None)
        assert paths_module.ensure_tls_material(paths) is None

    def test_generated_material_is_reported(
        self, tmp_path: Path, restore_umask, monkeypatch
    ) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)

        class Material:
            def __init__(self, key_path: Path, cert_path: Path) -> None:
                self.key_path = key_path
                self.cert_path = cert_path

        def ensure_tls_material(target):
            target.tls_key.write_text("synthetic key placeholder", encoding="utf-8")
            target.tls_cert.write_text("synthetic certificate placeholder", encoding="utf-8")
            return Material(target.tls_key, target.tls_cert)

        self._install_stub_tls(monkeypatch, self._stub(ensure_tls_material))
        assert paths_module.ensure_tls_material(paths) == (paths.tls_key, paths.tls_cert)

    def test_a_failing_generator_does_not_propagate(
        self, tmp_path: Path, restore_umask, monkeypatch
    ) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)

        def ensure_tls_material(_target):
            raise RuntimeError("synthetic generation failure")

        self._install_stub_tls(monkeypatch, self._stub(ensure_tls_material))
        assert paths_module.ensure_tls_material(paths) is None

    def test_a_generator_that_writes_nothing_falls_back_to_none(
        self, tmp_path: Path, restore_umask, monkeypatch
    ) -> None:
        paths = ensure(tmp_path / "data", ensure_tls=False)

        def ensure_tls_material(_target):
            return None

        self._install_stub_tls(monkeypatch, self._stub(ensure_tls_material))
        assert paths_module.ensure_tls_material(paths) is None

    def test_the_real_module_produces_a_usable_pair(
        self, tmp_path: Path, restore_umask
    ) -> None:
        # Integration with the certificate generator that actually ships. Skipped while that
        # module is still being built, so this item is verifiable on its own.
        pytest.importorskip("expirymanager.security.tls")
        paths = ensure(tmp_path / "data", ensure_tls=False)

        material = paths_module.ensure_tls_material(paths)
        assert material is not None
        key_path, cert_path = material
        assert key_path.exists() and cert_path.exists()
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
