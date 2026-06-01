from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from make_llvm_toolchain.planner import BuildConfig, PlannerError, build_plan  # noqa: E402


class PlannerTests(unittest.TestCase):
    def make_config(self, **overrides: object) -> BuildConfig:
        base = {
            "source_dir": Path("/tmp/llvm-project"),
            "build_root": Path("/tmp/build"),
            "install_root": Path("/tmp/install"),
            "cache_root": Path("/tmp/cache"),
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
        return BuildConfig(**base)

    def test_glibc_libstdcxx_uses_host_tools_only(self) -> None:
        plan = build_plan(self.make_config())
        sysroot_steps = [
            step for step in plan.steps if step.profile == "glibc-libstdcxx" and step.kind == "sysroot"
        ]
        runtime_commands = [
            step for step in plan.steps if step.profile == "glibc-libstdcxx" and step.kind == "command"
        ]
        wrapper_steps = [
            step for step in plan.steps if step.profile == "glibc-libstdcxx" and step.kind == "wrappers"
        ]
        self.assertEqual(len(sysroot_steps), 1)
        self.assertEqual(runtime_commands, [])
        self.assertEqual(len(wrapper_steps), 1)
        self.assertEqual(sysroot_steps[0].metadata["root_path"], "/tmp/cache/sysroots/glibc-libstdcxx")
        self.assertEqual(sysroot_steps[0].metadata["toolchain_dir"], "/tmp/cache/gcc-toolchains/glibc-libstdcxx")
        self.assertEqual(wrapper_steps[0].metadata["sysroot"], "/tmp/cache/sysroots/glibc-libstdcxx")
        self.assertEqual(
            wrapper_steps[0].metadata["gcc_toolchain"],
            "/tmp/cache/gcc-toolchains/glibc-libstdcxx",
        )

    def test_musl_auto_managed_sysroot_is_planned(self) -> None:
        plan = build_plan(self.make_config(runtime_profiles=("musl+libc++",)))
        sysroot_step = next(step for step in plan.steps if step.name == "prepare-sysroot-musl-libcxx")
        configure = next(step for step in plan.steps if step.name == "configure-runtimes-musl-libcxx")
        assert configure.command is not None
        self.assertEqual(sysroot_step.metadata["root_path"], "/tmp/cache/sysroots/musl-libcxx")
        self.assertIn("-DCMAKE_SYSROOT=/tmp/cache/sysroots/musl-libcxx", configure.command)
        self.assertIn("-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY", configure.command)

    def test_glibc_libcxx_runtime_flags_are_emitted(self) -> None:
        plan = build_plan(self.make_config(runtime_profiles=("glibc+libc++",)))
        configure = next(step for step in plan.steps if step.name == "configure-runtimes-glibc-libcxx")
        assert configure.command is not None
        self.assertIn(
            "-DLLVM_ENABLE_RUNTIMES=compiler-rt;libunwind;libcxxabi;libcxx",
            configure.command,
        )
        self.assertIn("-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF", configure.command)
        self.assertIn("-DCMAKE_C_COMPILER=/tmp/install/host-tools/bin/clang", configure.command)
        self.assertIn("-DCMAKE_SYSROOT=/tmp/cache/sysroots/glibc-libcxx", configure.command)
        self.assertIn("-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY", configure.command)
        self.assertIn("-DCOMPILER_RT_CXX_LIBRARY=libcxx", configure.command)
        self.assertIn("-DCOMPILER_RT_USE_BUILTINS_LIBRARY=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_USE_LLVM_UNWINDER=ON", configure.command)
        self.assertIn("-DLIBUNWIND_USE_COMPILER_RT=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_SANITIZERS=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_MEMPROF=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_PROFILE=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_LIBFUZZER=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_ORC=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_GWP_ASAN=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_XRAY=OFF", configure.command)

    def test_musl_libcxx_runtime_flags_are_emitted(self) -> None:
        plan = build_plan(
            self.make_config(
                runtime_profiles=("musl+libc++",),
                sysroots={"musl+libc++": Path("/opt/sysroots/musl")},
            )
        )
        configure = next(step for step in plan.steps if step.name == "configure-runtimes-musl-libcxx")
        assert configure.command is not None
        self.assertIn(
            "-DLLVM_ENABLE_RUNTIMES=compiler-rt;libunwind;libcxxabi;libcxx",
            configure.command,
        )
        self.assertIn("-DCMAKE_SYSROOT=/opt/sysroots/musl", configure.command)
        self.assertIn("-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY", configure.command)
        self.assertIn("-DCOMPILER_RT_CXX_LIBRARY=libcxx", configure.command)
        self.assertIn("-DCOMPILER_RT_USE_BUILTINS_LIBRARY=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_USE_LLVM_UNWINDER=ON", configure.command)
        self.assertIn("-DLIBUNWIND_USE_COMPILER_RT=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_SANITIZERS=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_MEMPROF=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_PROFILE=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_LIBFUZZER=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_ORC=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_GWP_ASAN=OFF", configure.command)
        self.assertIn("-DCOMPILER_RT_BUILD_XRAY=OFF", configure.command)

    def test_llvm_libc_profile_enables_full_build(self) -> None:
        plan = build_plan(
            self.make_config(
                runtime_profiles=("llvm-libc+libc++",),
                sysroots={"llvm-libc+libc++": Path("/opt/sysroots/llvm-libc")},
            )
        )
        sysroot_step = next(step for step in plan.steps if step.name == "prepare-sysroot-llvm-libc-libcxx")
        header_configure = next(
            step for step in plan.steps if step.name == "configure-libc-headers-llvm-libc-libcxx"
        )
        header_build = next(
            step for step in plan.steps if step.name == "build-libc-headers-llvm-libc-libcxx"
        )
        configure = next(step for step in plan.steps if step.name == "configure-runtimes-llvm-libc-libcxx")
        build_step = next(step for step in plan.steps if step.name == "build-runtimes-llvm-libc-libcxx")
        assert header_configure.command is not None
        assert header_build.command is not None
        assert configure.command is not None
        assert build_step.command is not None
        self.assertEqual(sysroot_step.metadata["base_c_library"], "llvm-libc")
        self.assertEqual(header_configure.command[4], "/tmp/llvm-project/runtimes")
        self.assertIn("-DCMAKE_INSTALL_PREFIX=/opt/sysroots/llvm-libc/usr", header_configure.command)
        self.assertIn("-DCMAKE_C_COMPILER=/tmp/install/host-tools/bin/clang", header_configure.command)
        self.assertIn("-DCMAKE_C_COMPILER_TARGET=x86_64-unknown-linux-llvm", header_configure.command)
        self.assertIn(
            "-DCMAKE_SYSROOT=/tmp/cache/bootstrap-sysroots/llvm-libc-libcxx",
            header_configure.command,
        )
        self.assertIn("-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY", header_configure.command)
        self.assertIn("-DLLVM_ENABLE_RUNTIMES=libc", header_configure.command)
        self.assertIn("-DLLVM_LIBC_FULL_BUILD=ON", header_configure.command)
        self.assertIn("-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF", header_configure.command)
        self.assertIn("-DLIBC_TARGET_TRIPLE=x86_64-unknown-linux-llvm", header_configure.command)
        self.assertEqual(
            header_build.command,
            (
                "cmake",
                "--build",
                "/tmp/build/libc-headers-llvm-libc-libcxx",
                "--target",
                "install-libc-headers",
                "--parallel",
                "8",
            ),
        )
        self.assertEqual(configure.command[4], "/tmp/llvm-project/llvm")
        self.assertIn(
            "-DLLVM_RUNTIME_TARGETS=x86_64-unknown-linux-llvm",
            configure.command,
        )
        self.assertIn("-DLLVM_BUILTIN_TARGETS=x86_64-unknown-linux-llvm", configure.command)
        self.assertIn("-DLLVM_ENABLE_PROJECTS=clang", configure.command)
        self.assertIn("-DLLVM_TARGETS_TO_BUILD=host", configure.command)
        self.assertIn("-DCLANG_DEFAULT_CXX_STDLIB=libc++", configure.command)
        self.assertIn("-DCLANG_DEFAULT_RTLIB=compiler-rt", configure.command)
        self.assertIn("-DCLANG_DEFAULT_UNWINDLIB=libunwind", configure.command)
        self.assertIn("-DCMAKE_INSTALL_PREFIX=/opt/sysroots/llvm-libc/usr", configure.command)
        self.assertNotIn("-DCMAKE_SYSROOT=/opt/sysroots/llvm-libc", configure.command)
        self.assertNotIn("-DCMAKE_C_COMPILER_TARGET=x86_64-unknown-linux-llvm", configure.command)
        self.assertNotIn("-DLLVM_PATH=/tmp/llvm-project/llvm", configure.command)
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LLVM_ENABLE_RUNTIMES=libc;compiler-rt;libunwind;libcxxabi;libcxx",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_RUNTIMES_USE_LIBC=llvm-libc",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_CMAKE_SYSROOT=/tmp/cache/bootstrap-sysroots/llvm-libc-libcxx",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_CMAKE_C_FLAGS=--target=x86_64-unknown-linux-llvm",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_CXX_LIBRARY=libcxx",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_USE_BUILTINS_LIBRARY=ON",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_USE_LLVM_UNWINDER=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBUNWIND_USE_COMPILER_RT=ON",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBUNWIND_ENABLE_SHARED=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXXABI_ENABLE_SHARED=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_SHARED=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_BUILD_PROFILE=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_BUILD_SANITIZERS=ON",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_BUILD_LIBFUZZER=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_BUILD_ORC=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_COMPILER_RT_BUILD_XRAY=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXXABI_ENABLE_EXCEPTIONS=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXXABI_ENABLE_THREADS=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXXABI_USE_LLVM_UNWINDER=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_EXCEPTIONS=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_FILESYSTEM=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_LOCALIZATION=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_MONOTONIC_CLOCK=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_RTTI=OFF",
            configure.command,
        )
        self.assertIn(
            "-DRUNTIMES_x86_64-unknown-linux-llvm_LIBCXX_ENABLE_THREADS=OFF",
            configure.command,
        )
        self.assertIn(
            "-DBUILTINS_x86_64-unknown-linux-llvm_CMAKE_SYSROOT=/tmp/cache/bootstrap-sysroots/llvm-libc-libcxx",
            configure.command,
        )
        self.assertIn(
            "-DBUILTINS_x86_64-unknown-linux-llvm_CMAKE_C_FLAGS=--target=x86_64-unknown-linux-llvm",
            configure.command,
        )
        self.assertEqual(
            build_step.command,
            (
                "cmake",
                "--build",
                "/tmp/build/runtimes-llvm-libc-libcxx",
                "--target",
                "install-libc",
                "--target",
                "install-compiler-rt",
                "--target",
                "install-builtins",
                "--target",
                "install-cxx",
                "--target",
                "install-cxxabi",
                "--target",
                "install-unwind",
                "--parallel",
                "8",
            ),
        )

    def test_rejects_llvm_libc_plus_libstdcxx(self) -> None:
        with self.assertRaises(PlannerError):
            build_plan(self.make_config(runtime_profiles=("llvm-libc+libstdc++",)))

    def test_llvm_libc_profile_requires_clang_project(self) -> None:
        with self.assertRaises(PlannerError):
            build_plan(
                self.make_config(
                    projects=("lld",),
                    runtime_profiles=("llvm-libc+libc++",),
                    sysroots={"llvm-libc+libc++": Path("/opt/sysroots/llvm-libc")},
                )
            )

    def test_custom_host_build_targets_are_emitted(self) -> None:
        plan = build_plan(
            self.make_config(host_build_targets=("install-clang", "install-clang-resource-headers"))
        )
        build_host = next(step for step in plan.steps if step.name == "build-host-tools")
        assert build_host.command is not None
        self.assertEqual(
            build_host.command,
            (
                "cmake",
                "--build",
                "/tmp/build/host-tools",
                "--target",
                "install-clang",
                "--target",
                "install-clang-resource-headers",
                "--parallel",
                "8",
            ),
        )

    def test_clean_adds_cleanup_step(self) -> None:
        plan = build_plan(self.make_config(clean=True, runtime_profiles=("glibc+libc++",)))
        cleanup = next(step for step in plan.steps if step.kind == "cleanup")
        self.assertEqual(cleanup.name, "cleanup-build-cache")
        self.assertIn("/tmp/build", cleanup.metadata["paths"])
        self.assertIn("/tmp/cache/sysroots/glibc-libcxx", cleanup.metadata["paths"])


class SysrootPreparationTests(unittest.TestCase):
    def test_llvm_libc_bootstrap_sysroot_skips_foreign_libc_builds(self) -> None:
        from make_llvm_toolchain.sysroot import SysrootBuildSpec, ensure_sysroot

        with mock.patch("make_llvm_toolchain.sysroot._manifest_matches", return_value=False), \
            mock.patch("make_llvm_toolchain.sysroot._reset_directory"), \
            mock.patch("make_llvm_toolchain.sysroot._build_linux_headers") as build_headers, \
            mock.patch("make_llvm_toolchain.sysroot._build_musl") as build_musl, \
            mock.patch("make_llvm_toolchain.sysroot._build_glibc") as build_glibc:
            spec = SysrootBuildSpec(
                profile_key="llvm-libc-libcxx",
                root_path=Path("/tmp/cache/bootstrap-sysroots/llvm-libc-libcxx"),
                managed_root_path=True,
                final_sysroot=Path("/tmp/sysroots/llvm-libc"),
                toolchain_dir=None,
                managed_gcc_toolchain=False,
                downloads_dir=Path("/tmp/cache/downloads"),
                sources_dir=Path("/tmp/cache/sources"),
                build_dir=Path("/tmp/cache/sysroot-builds/llvm-libc-libcxx"),
                target_triple="x86_64-unknown-linux-llvm",
                base_c_library="llvm-libc",
                cpp_library="libc++",
                linux_version="6.12.32",
                glibc_version="2.39",
                libstdcxx_version="14.2.0",
                musl_version="1.2.5",
                jobs=8,
            )

            ensure_sysroot(spec)

        build_headers.assert_called_once_with(spec, False)
        build_musl.assert_not_called()
        build_glibc.assert_not_called()


if __name__ == "__main__":
    unittest.main()