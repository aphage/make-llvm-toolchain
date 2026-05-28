from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from make_llvm_toolchain.cli import (  # noqa: E402
    SourceBootstrapConfig,
    _build_config_from_args,
    _build_parser,
    _build_source_bootstrap_config,
    _ensure_source_checkout,
)
from make_llvm_toolchain.planner import BuildConfig, PlannerError, build_plan  # noqa: E402
from make_llvm_toolchain.releases import ResolvedComponentVersions  # noqa: E402


class SourceBootstrapTests(unittest.TestCase):
    def make_plan(self, source_dir: Path, **overrides: object):
        base = {
            "source_dir": source_dir,
            "build_root": source_dir.parent / "build",
            "install_root": source_dir.parent / "install",
            "projects": ("clang", "lld", "lldb"),
            "runtime_profiles": ("glibc+libstdc++",),
            "build_type": "Release",
            "generator": "Ninja",
            "targets_to_build": "host",
            "host_build_targets": (),
            "jobs": 8,
            "assertions": False,
            "host_c_compiler": None,
            "host_cxx_compiler": None,
            "sysroots": {},
            "gcc_toolchains": {},
            "target_triples": {},
            "cmake_defines": {},
            "profile_cmake_defines": {},
            "linux_version": "6.12.32",
            "glibc_version": "2.39",
            "libstdcxx_version": "14.2.0",
            "musl_version": "1.2.5",
        }
        base.update(overrides)
        return build_plan(BuildConfig(**base))

    def _write_checkout(self, source_dir: Path, include_runtimes: bool) -> None:
        for project in ("llvm", "clang", "lld", "lldb"):
            project_dir = source_dir / project
            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
        if include_runtimes:
            runtimes_dir = source_dir / "runtimes"
            runtimes_dir.mkdir(parents=True, exist_ok=True)
            (runtimes_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")

    def test_glibc_libstdcxx_does_not_require_runtimes_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "llvm-project"
            self._write_checkout(source_dir, include_runtimes=False)
            plan = self.make_plan(source_dir)
            _ensure_source_checkout(plan, SourceBootstrapConfig(auto_download=False))

    def test_missing_checkout_triggers_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "llvm-project"
            plan = self.make_plan(source_dir, runtime_profiles=("glibc+libc++",))

            def fake_clone(clone_dir: Path, bootstrap: SourceBootstrapConfig, verbose: bool = False) -> None:
                self.assertEqual(clone_dir, source_dir)
                self.assertEqual(bootstrap.git_url, "https://example.com/llvm-project.git")
                self._write_checkout(clone_dir, include_runtimes=True)

            with mock.patch("make_llvm_toolchain.cli._clone_llvm_project", side_effect=fake_clone) as clone:
                _ensure_source_checkout(
                    plan,
                    SourceBootstrapConfig(git_url="https://example.com/llvm-project.git"),
                )
                clone.assert_called_once()

    def test_missing_checkout_resolves_latest_llvm_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "llvm-project"
            plan = self.make_plan(source_dir, runtime_profiles=("glibc+libc++",))

            def fake_clone(clone_dir: Path, bootstrap: SourceBootstrapConfig, verbose: bool = False) -> None:
                self.assertEqual(clone_dir, source_dir)
                self.assertEqual(bootstrap.git_ref, "llvmorg-22.1.6")
                self._write_checkout(clone_dir, include_runtimes=True)

            with mock.patch(
                "make_llvm_toolchain.cli.resolve_latest_llvm_release_version",
                return_value="22.1.6",
            ) as latest_release:
                with mock.patch(
                    "make_llvm_toolchain.cli._clone_llvm_project",
                    side_effect=fake_clone,
                ) as clone:
                    _ensure_source_checkout(
                        plan,
                        SourceBootstrapConfig(git_url="https://example.com/llvm-project.git"),
                    )
                    latest_release.assert_called_once()
                    clone.assert_called_once()

    def test_non_empty_invalid_directory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "llvm-project"
            source_dir.mkdir(parents=True)
            (source_dir / "README.txt").write_text("not llvm-project\n")
            plan = self.make_plan(source_dir)
            with self.assertRaises(PlannerError):
                _ensure_source_checkout(plan, SourceBootstrapConfig())


class CliArgumentParsingTests(unittest.TestCase):
    def test_cmake_define_preserves_cmake_key_casing(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "plan",
                "--cmake-define",
                "LLVM_INCLUDE_TESTS=OFF",
                "--sysroot",
                "llvm-libc+libc++=/tmp/sysroot",
                "--linux-version",
                "6.12.32",
                "--glibc-version",
                "2.39",
                "--libstdcxx-version",
                "14.2.0",
                "--musl-version",
                "1.2.5",
            ]
        )

        config = _build_config_from_args(args)

        self.assertEqual(config.cmake_defines, {"LLVM_INCLUDE_TESTS": "OFF"})
        self.assertEqual(config.sysroots, {"llvm-libc-libcxx": Path("/tmp/sysroot")})

    def test_sysroot_version_and_clean_arguments_are_captured(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "build",
                "--cache-root",
                "/tmp/cache",
                "--clean",
                "--linux-version",
                "6.6.30",
                "--glibc-version",
                "2.38",
                "--libstdcxx-version",
                "13.3.0",
                "--musl-version",
                "1.2.4",
            ]
        )

        config = _build_config_from_args(args)

        self.assertEqual(config.cache_root, Path("/tmp/cache"))
        self.assertTrue(config.clean)
        self.assertEqual(config.linux_version, "6.6.30")
        self.assertEqual(config.glibc_version, "2.38")
        self.assertEqual(config.libstdcxx_version, "13.3.0")
        self.assertEqual(config.musl_version, "1.2.4")

    def test_default_component_versions_are_resolved(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["build"])
        resolved_versions = ResolvedComponentVersions(
            linux_version="7.0.10",
            glibc_version="2.43",
            libstdcxx_version="16.1.0",
            musl_version="1.2.6",
        )

        with mock.patch(
            "make_llvm_toolchain.cli.resolve_component_versions",
            return_value=resolved_versions,
        ) as resolver:
            config = _build_config_from_args(args)

        resolver.assert_called_once_with(
            linux_version=None,
            glibc_version=None,
            libstdcxx_version=None,
            musl_version=None,
        )
        self.assertEqual(config.linux_version, "7.0.10")
        self.assertEqual(config.glibc_version, "2.43")
        self.assertEqual(config.libstdcxx_version, "16.1.0")
        self.assertEqual(config.musl_version, "1.2.6")

    def test_llvm_version_argument_sets_bootstrap_git_ref(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["build", "--llvm-version", "22.1.6"])

        bootstrap = _build_source_bootstrap_config(args)

        self.assertEqual(bootstrap.git_ref, "llvmorg-22.1.6")

    def test_rejects_llvm_version_with_explicit_git_ref(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "build",
                "--llvm-version",
                "22.1.6",
                "--llvm-git-ref",
                "main",
            ]
        )

        with self.assertRaises(PlannerError):
            _build_source_bootstrap_config(args)


if __name__ == "__main__":
    unittest.main()