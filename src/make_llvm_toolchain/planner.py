from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence


SUPPORTED_PROJECTS = ("clang", "lld", "lldb")

_RUNTIME_ALIAS_OVERRIDES = {
    "glibc+libstdc++": "glibc-libstdcxx",
    "glibc+libc++": "glibc-libcxx",
    "musl+libstdc++": "musl-libstdcxx",
    "musl+libc++": "musl-libcxx",
    "llvm-libc+libstdc++": "llvm-libc-libstdcxx",
    "llvm-libc+libc++": "llvm-libc-libcxx",
}

_RESERVED_CMAKE_DEFINES = {
    "CMAKE_ASM_COMPILER",
    "CMAKE_CXX_COMPILER",
    "CMAKE_CXX_COMPILER_TARGET",
    "CMAKE_C_COMPILER",
    "CMAKE_C_COMPILER_TARGET",
    "CMAKE_INSTALL_PREFIX",
    "CMAKE_SYSROOT",
    "LLDB_ENABLE_PYTHON",
    "LLVM_DEFAULT_TARGET_TRIPLE",
    "LLVM_ENABLE_ASSERTIONS",
    "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR",
    "LLVM_ENABLE_PROJECTS",
    "LLVM_ENABLE_RUNTIMES",
    "LLVM_LIBC_FULL_BUILD",
    "LLVM_PATH",
    "LLVM_TARGETS_TO_BUILD",
}


class PlannerError(ValueError):
    """Raised when the requested build matrix cannot be planned."""


@dataclass(frozen=True)
class RuntimeProfile:
    key: str
    c_library: str
    cpp_library: str
    runtimes: tuple[str, ...]
    default_target_triple: str
    requires_sysroot: bool = False
    uses_libcxx: bool = False
    full_build: bool = False
    notes: tuple[str, ...] = ()


RUNTIME_PROFILES: Mapping[str, RuntimeProfile] = {
    "glibc-libstdcxx": RuntimeProfile(
        key="glibc-libstdcxx",
        c_library="glibc",
        cpp_library="libstdc++",
        runtimes=(),
        default_target_triple="x86_64-unknown-linux-gnu",
        requires_sysroot=True,
        notes=(
            "Builds against a managed or user-provided glibc sysroot and a matching GCC/libstdc++ toolchain.",
        ),
    ),
    "glibc-libcxx": RuntimeProfile(
        key="glibc-libcxx",
        c_library="glibc",
        cpp_library="libc++",
        runtimes=("compiler-rt", "libunwind", "libcxxabi", "libcxx"),
        default_target_triple="x86_64-unknown-linux-gnu",
        requires_sysroot=True,
        uses_libcxx=True,
    ),
    "musl-libstdcxx": RuntimeProfile(
        key="musl-libstdcxx",
        c_library="musl",
        cpp_library="libstdc++",
        runtimes=(),
        default_target_triple="x86_64-unknown-linux-musl",
        requires_sysroot=True,
        notes=(
            "Builds against a managed or user-provided musl sysroot and a matching GCC/libstdc++ toolchain.",
        ),
    ),
    "musl-libcxx": RuntimeProfile(
        key="musl-libcxx",
        c_library="musl",
        cpp_library="libc++",
        runtimes=("compiler-rt", "libunwind", "libcxxabi", "libcxx"),
        default_target_triple="x86_64-unknown-linux-musl",
        requires_sysroot=True,
        uses_libcxx=True,
    ),
    "llvm-libc-libcxx": RuntimeProfile(
        key="llvm-libc-libcxx",
        c_library="llvm-libc",
        cpp_library="libc++",
        runtimes=("libc", "compiler-rt", "libunwind", "libcxxabi", "libcxx"),
        default_target_triple="x86_64-unknown-linux-llvm",
        requires_sysroot=True,
        uses_libcxx=True,
        full_build=True,
        notes=(
            "Uses a managed bootstrap sysroot for header and runtime configuration and installs the final runtime into the requested llvm-libc sysroot.",
        ),
    ),
}


@dataclass(frozen=True)
class BuildConfig:
    source_dir: Path
    build_root: Path
    install_root: Path
    projects: tuple[str, ...] = SUPPORTED_PROJECTS
    runtime_profiles: tuple[str, ...] = ("glibc-libstdcxx",)
    build_type: str = "Release"
    generator: str = "Ninja"
    targets_to_build: str = "host"
    host_build_targets: tuple[str, ...] = ("install",)
    jobs: int | None = None
    assertions: bool = False
    host_c_compiler: str | None = None
    host_cxx_compiler: str | None = None
    sysroots: Mapping[str, Path] = field(default_factory=dict)
    gcc_toolchains: Mapping[str, Path] = field(default_factory=dict)
    target_triples: Mapping[str, str] = field(default_factory=dict)
    cmake_defines: Mapping[str, str] = field(default_factory=dict)
    profile_cmake_defines: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    cache_root: Path = Path(".cache")
    clean: bool = False
    linux_version: str | None = None
    glibc_version: str | None = None
    libstdcxx_version: str | None = None
    musl_version: str | None = None
    managed_sysroots: Mapping[str, bool] = field(default_factory=dict)
    managed_gcc_toolchains: Mapping[str, bool] = field(default_factory=dict)
    bootstrap_sysroots: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanStep:
    name: str
    description: str
    kind: str
    profile: str | None = None
    command: tuple[str, ...] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildPlan:
    config: BuildConfig
    host_build_dir: Path
    host_install_dir: Path
    steps: tuple[PlanStep, ...]


def normalize_runtime_key(raw_key: str) -> str:
    lowered = raw_key.strip().lower().replace(" ", "")
    if lowered in _RUNTIME_ALIAS_OVERRIDES:
        return _RUNTIME_ALIAS_OVERRIDES[lowered]
    lowered = lowered.replace("libstdc++", "libstdcxx").replace("libc++", "libcxx")
    return lowered


def normalize_project_name(raw_project: str) -> str:
    return raw_project.strip().lower()


def _dedupe_preserving_order(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _normalize_profile_path_map(values: Mapping[str, Path]) -> dict[str, Path]:
    normalized: dict[str, Path] = {}
    for key, value in values.items():
        normalized[normalize_runtime_key(key)] = Path(value)
    return normalized


def _normalize_profile_string_map(values: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[normalize_runtime_key(key)] = value
    return normalized


def _normalize_profile_define_map(
    values: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for profile_key, define_map in values.items():
        normalized[normalize_runtime_key(profile_key)] = dict(define_map)
    return normalized


def _ensure_supported_projects(raw_projects: Sequence[str]) -> tuple[str, ...]:
    projects = raw_projects or SUPPORTED_PROJECTS
    normalized = _dedupe_preserving_order(normalize_project_name(project) for project in projects)
    unsupported = [project for project in normalized if project not in SUPPORTED_PROJECTS]
    if unsupported:
        raise PlannerError(
            f"Unsupported project selection: {', '.join(unsupported)}. "
            f"Supported projects are: {', '.join(SUPPORTED_PROJECTS)}."
        )
    return normalized


def _ensure_supported_profiles(raw_profiles: Sequence[str]) -> tuple[str, ...]:
    profiles = raw_profiles or ("glibc-libstdcxx",)
    normalized = _dedupe_preserving_order(normalize_runtime_key(profile) for profile in profiles)
    if "llvm-libc-libstdcxx" in normalized:
        raise PlannerError("The llvm-libc + libstdc++ runtime pair is rejected in v1.")
    unsupported = [profile for profile in normalized if profile not in RUNTIME_PROFILES]
    if unsupported:
        raise PlannerError(
            f"Unsupported runtime selection: {', '.join(unsupported)}. "
            f"Supported matrix entries are: {', '.join(RUNTIME_PROFILES)}."
        )
    return normalized


def _validate_define_names(defines: Mapping[str, str], scope: str) -> None:
    reserved = sorted(name for name in defines if name in _RESERVED_CMAKE_DEFINES)
    if reserved:
        raise PlannerError(
            f"{scope} overrides reserved CMake definitions: {', '.join(reserved)}. "
            "Use the dedicated CLI options instead."
        )


def _cmake_define_args(defines: Mapping[str, str]) -> list[str]:
    return [f"-D{name}={value}" for name, value in defines.items()]


def _auto_sysroot_path(config: BuildConfig, profile_key: str) -> Path:
    return config.cache_root / "sysroots" / profile_key


def _auto_gcc_toolchain_path(config: BuildConfig, profile_key: str) -> Path:
    return config.cache_root / "gcc-toolchains" / profile_key


def _bootstrap_sysroot_path(config: BuildConfig, profile_key: str) -> Path:
    return config.cache_root / "bootstrap-sysroots" / profile_key


def _sysroot_build_workspace(config: BuildConfig, profile_key: str) -> Path:
    return config.cache_root / "sysroot-builds" / profile_key


def _validate_component_versions(
    config: BuildConfig, runtime_profiles: tuple[str, ...]
) -> None:
    profiles = set(runtime_profiles)
    versions: dict[str, str | None] = {}
    c_libraries = {RUNTIME_PROFILES[p].c_library for p in profiles}
    cpp_libraries = {RUNTIME_PROFILES[p].cpp_library for p in profiles}
    requires_sysroot = any(RUNTIME_PROFILES[p].requires_sysroot for p in profiles)

    if requires_sysroot:
        versions["--linux-version"] = config.linux_version
    if "glibc" in c_libraries:
        versions["--glibc-version"] = config.glibc_version
    if "musl" in c_libraries:
        versions["--musl-version"] = config.musl_version
    if "libstdc++" in cpp_libraries:
        versions["--libstdcxx-version"] = config.libstdcxx_version

    invalid = [name for name, value in versions.items() if value is None or not value.strip()]
    if invalid:
        raise PlannerError(f"Version arguments must not be empty: {', '.join(invalid)}.")


def _resolve_config(config: BuildConfig) -> BuildConfig:
    projects = _ensure_supported_projects(config.projects)
    runtime_profiles = _ensure_supported_profiles(config.runtime_profiles)
    if any(RUNTIME_PROFILES[profile_key].full_build for profile_key in runtime_profiles):
        if "clang" not in projects:
            raise PlannerError(
                "The llvm-libc + libc++ profile requires clang in --project selection."
            )
    _validate_component_versions(config, runtime_profiles)

    sysroots = _normalize_profile_path_map(config.sysroots)
    gcc_toolchains = _normalize_profile_path_map(config.gcc_toolchains)
    target_triples = _normalize_profile_string_map(config.target_triples)
    profile_cmake_defines = _normalize_profile_define_map(config.profile_cmake_defines)
    _validate_define_names(config.cmake_defines, "Global")
    for profile_key, define_map in profile_cmake_defines.items():
        _validate_define_names(define_map, f"Profile {profile_key}")

    managed_sysroots: dict[str, bool] = {}
    managed_gcc_toolchains: dict[str, bool] = {}
    bootstrap_sysroots: dict[str, Path] = {}

    for profile_key in runtime_profiles:
        profile = RUNTIME_PROFILES[profile_key]
        if profile.requires_sysroot and profile_key not in sysroots:
            sysroots[profile_key] = _auto_sysroot_path(config, profile_key)
            managed_sysroots[profile_key] = True
        else:
            managed_sysroots[profile_key] = False

        bootstrap_sysroots[profile_key] = (
            _bootstrap_sysroot_path(config, profile_key) if profile.full_build else sysroots[profile_key]
        )

        if profile.cpp_library == "libstdc++" and profile_key not in gcc_toolchains:
            gcc_toolchains[profile_key] = _auto_gcc_toolchain_path(config, profile_key)
            managed_gcc_toolchains[profile_key] = True
        else:
            managed_gcc_toolchains[profile_key] = False

    return replace(
        config,
        projects=projects,
        runtime_profiles=runtime_profiles,
        sysroots=sysroots,
        gcc_toolchains=gcc_toolchains,
        target_triples=target_triples,
        profile_cmake_defines=profile_cmake_defines,
        managed_sysroots=managed_sysroots,
        managed_gcc_toolchains=managed_gcc_toolchains,
        bootstrap_sysroots=bootstrap_sysroots,
    )


def _profile_target_triple(config: BuildConfig, profile_key: str) -> str:
    return config.target_triples.get(profile_key, RUNTIME_PROFILES[profile_key].default_target_triple)


def _runtime_install_dir(config: BuildConfig, profile_key: str) -> Path:
    return config.install_root / "profiles" / profile_key


def _runtime_install_prefix(config: BuildConfig, profile_key: str) -> Path:
    profile = RUNTIME_PROFILES[profile_key]
    if profile.full_build:
        return config.sysroots[profile_key] / "usr"
    return _runtime_install_dir(config, profile_key)


def _runtime_sysroot_path(config: BuildConfig, profile_key: str) -> Path:
    return config.bootstrap_sysroots.get(profile_key, config.sysroots[profile_key])


def _runtime_header_stage_build_dir(config: BuildConfig, profile_key: str) -> Path:
    return config.build_root / f"libc-headers-{profile_key}"


def _runtime_source_dir(config: BuildConfig, profile_key: str) -> Path:
    profile = RUNTIME_PROFILES[profile_key]
    if profile.full_build:
        return config.source_dir / "llvm"
    return config.source_dir / "runtimes"


def _host_configure_command(config: BuildConfig, host_build_dir: Path, host_install_dir: Path) -> tuple[str, ...]:
    command = [
        "cmake",
        "-G",
        config.generator,
        "-S",
        str(config.source_dir / "llvm"),
        "-B",
        str(host_build_dir),
        f"-DCMAKE_BUILD_TYPE={config.build_type}",
        f"-DCMAKE_INSTALL_PREFIX={host_install_dir}",
        f"-DLLVM_ENABLE_PROJECTS={';'.join(config.projects)}",
        f"-DLLVM_TARGETS_TO_BUILD={config.targets_to_build}",
        f"-DLLVM_ENABLE_ASSERTIONS={'ON' if config.assertions else 'OFF'}",
    ]
    if "lldb" in config.projects:
        command.append("-DLLDB_ENABLE_PYTHON=ON")
    if config.host_c_compiler:
        command.append(f"-DCMAKE_C_COMPILER={config.host_c_compiler}")
    if config.host_cxx_compiler:
        command.append(f"-DCMAKE_CXX_COMPILER={config.host_cxx_compiler}")
    command.extend(_cmake_define_args(config.cmake_defines))
    return tuple(command)


def _libcxx_runtime_defines() -> dict[str, str]:
    return {
        "LIBCXX_USE_COMPILER_RT": "ON",
        "LIBCXXABI_USE_COMPILER_RT": "ON",
        "LIBCXXABI_USE_LLVM_UNWINDER": "ON",
        "LIBCXX_CXX_ABI": "libcxxabi",
        "COMPILER_RT_CXX_LIBRARY": "libcxx",
        "COMPILER_RT_USE_BUILTINS_LIBRARY": "ON",
        "COMPILER_RT_USE_LLVM_UNWINDER": "ON",
        "LIBUNWIND_USE_COMPILER_RT": "ON",
        "COMPILER_RT_DEFAULT_TARGET_ONLY": "ON",
    }


def _prefixed_defines(prefix: str, defines: Mapping[str, str]) -> dict[str, str]:
    return {f"{prefix}{name}": value for name, value in defines.items()}


def _llvm_libc_full_build_defines(
    config: BuildConfig,
    profile_key: str,
    target_triple: str,
) -> dict[str, str]:
    runtime_prefix = f"RUNTIMES_{target_triple}_"
    builtins_prefix = f"BUILTINS_{target_triple}_"
    target_flags = f"--target={target_triple}"
    sysroot = str(_runtime_sysroot_path(config, profile_key))
    runtime_defines = _prefixed_defines(runtime_prefix, _libcxx_runtime_defines())
    runtime_defines.update(
        {
            f"{runtime_prefix}LLVM_ENABLE_RUNTIMES": ";".join(
                RUNTIME_PROFILES[profile_key].runtimes
            ),
            f"{runtime_prefix}RUNTIMES_USE_LIBC": "llvm-libc",
            f"{runtime_prefix}LLVM_LIBC_FULL_BUILD": "ON",
            f"{runtime_prefix}LIBC_INCLUDE_DOCS": "OFF",
            f"{runtime_prefix}LLVM_LIBC_INCLUDE_SCUDO": "ON",
            f"{runtime_prefix}COMPILER_RT_BUILD_MEMPROF": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_PROFILE": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_SANITIZERS": "ON",
            f"{runtime_prefix}COMPILER_RT_BUILD_CRT": "TRUE",
            f"{runtime_prefix}COMPILER_RT_BUILD_LIBFUZZER": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_ORC": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_GWP_ASAN": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_XRAY": "OFF",
            f"{runtime_prefix}COMPILER_RT_BUILD_SCUDO_STANDALONE_WITH_LLVM_LIBC": "ON",
            f"{runtime_prefix}COMPILER_RT_SCUDO_STANDALONE_BUILD_SHARED": "OFF",
            f"{runtime_prefix}COMPILER_RT_USE_LLVM_UNWINDER": "OFF",
            f"{runtime_prefix}LIBUNWIND_ENABLE_SHARED": "OFF",
            f"{runtime_prefix}LIBCXXABI_ENABLE_SHARED": "OFF",
            f"{runtime_prefix}LIBCXXABI_ENABLE_EXCEPTIONS": "OFF",
            f"{runtime_prefix}LIBCXXABI_ENABLE_THREADS": "OFF",
            f"{runtime_prefix}LIBCXXABI_USE_LLVM_UNWINDER": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_SHARED": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_EXCEPTIONS": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_FILESYSTEM": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_LOCALIZATION": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_MONOTONIC_CLOCK": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_RTTI": "OFF",
            f"{runtime_prefix}LIBCXX_ENABLE_THREADS": "OFF",
            f"{runtime_prefix}CMAKE_SYSTEM_NAME": "Linux",
            f"{runtime_prefix}CMAKE_SYSROOT": sysroot,
            f"{runtime_prefix}CMAKE_C_FLAGS": target_flags,
            f"{runtime_prefix}CMAKE_CXX_FLAGS": target_flags,
            f"{runtime_prefix}CMAKE_ASM_FLAGS": target_flags,
        }
    )
    return {
        "CLANG_DEFAULT_CXX_STDLIB": "libc++",
        "CLANG_DEFAULT_RTLIB": "compiler-rt",
        "CLANG_DEFAULT_UNWINDLIB": "libunwind",
        "LLVM_ENABLE_PROJECTS": "clang",
        "LLVM_TARGETS_TO_BUILD": config.targets_to_build,
        "LLVM_RUNTIME_TARGETS": target_triple,
        "LLVM_BUILTIN_TARGETS": target_triple,
        f"{builtins_prefix}CMAKE_SYSTEM_NAME": "Linux",
        f"{builtins_prefix}CMAKE_SYSROOT": sysroot,
        f"{builtins_prefix}CMAKE_C_FLAGS": target_flags,
        f"{builtins_prefix}CMAKE_CXX_FLAGS": target_flags,
        f"{builtins_prefix}CMAKE_ASM_FLAGS": target_flags,
        **runtime_defines,
    }


def _runtime_default_defines(config: BuildConfig, profile_key: str, host_install_dir: Path) -> dict[str, str]:
    profile = RUNTIME_PROFILES[profile_key]
    target_triple = _profile_target_triple(config, profile_key)
    defines = {
        "CMAKE_BUILD_TYPE": config.build_type,
        "CMAKE_INSTALL_PREFIX": str(_runtime_install_prefix(config, profile_key)),
        "LLVM_DEFAULT_TARGET_TRIPLE": target_triple,
        "CMAKE_C_COMPILER": str(host_install_dir / "bin" / "clang"),
        "CMAKE_CXX_COMPILER": str(host_install_dir / "bin" / "clang++"),
        "CMAKE_ASM_COMPILER": str(host_install_dir / "bin" / "clang"),
    }
    if profile.runtimes and not profile.full_build:
        defines["LLVM_ENABLE_PER_TARGET_RUNTIME_DIR"] = "OFF"
        defines["LLVM_ENABLE_RUNTIMES"] = ";".join(profile.runtimes)
    if not profile.full_build:
        defines["CMAKE_C_COMPILER_TARGET"] = target_triple
        defines["CMAKE_CXX_COMPILER_TARGET"] = target_triple
    if not profile.full_build:
        defines["LLVM_PATH"] = str(config.source_dir / "llvm")
    if profile.requires_sysroot and not profile.full_build:
        defines["CMAKE_SYSROOT"] = str(_runtime_sysroot_path(config, profile_key))
        defines["CMAKE_TRY_COMPILE_TARGET_TYPE"] = "STATIC_LIBRARY"
    if profile.uses_libcxx:
        defines.update(_libcxx_runtime_defines())
    if profile.full_build:
        for name in list(_libcxx_runtime_defines()):
            defines.pop(name, None)
        defines.update(_llvm_libc_full_build_defines(config, profile_key, target_triple))
    return defines


def _llvm_libc_header_stage_defines(
    config: BuildConfig,
    profile_key: str,
    host_install_dir: Path,
) -> dict[str, str]:
    target_triple = _profile_target_triple(config, profile_key)
    sysroot = str(_runtime_sysroot_path(config, profile_key))
    return {
        "CMAKE_BUILD_TYPE": config.build_type,
        "CMAKE_INSTALL_PREFIX": str(_runtime_install_prefix(config, profile_key)),
        "CMAKE_C_COMPILER": str(host_install_dir / "bin" / "clang"),
        "CMAKE_CXX_COMPILER": str(host_install_dir / "bin" / "clang++"),
        "CMAKE_ASM_COMPILER": str(host_install_dir / "bin" / "clang"),
        "CMAKE_C_COMPILER_TARGET": target_triple,
        "CMAKE_CXX_COMPILER_TARGET": target_triple,
        "CMAKE_ASM_COMPILER_TARGET": target_triple,
        "CMAKE_SYSROOT": sysroot,
        "CMAKE_TRY_COMPILE_TARGET_TYPE": "STATIC_LIBRARY",
        "LLVM_ENABLE_RUNTIMES": "libc",
        "LLVM_LIBC_FULL_BUILD": "ON",
        "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR": "OFF",
        "LLVM_DEFAULT_TARGET_TRIPLE": target_triple,
        "LIBC_TARGET_TRIPLE": target_triple,
        "LLVM_INCLUDE_TESTS": "OFF",
        "LLVM_INCLUDE_BENCHMARKS": "OFF",
        "LLVM_INCLUDE_EXAMPLES": "OFF",
        "LIBC_INCLUDE_DOCS": "OFF",
        "LIBC_INCLUDE_TESTS": "OFF",
        "LIBC_INCLUDE_BENCHMARKS": "OFF",
        "LIBC_INCLUDE_EXAMPLES": "OFF",
    }


def _llvm_libc_header_stage_configure_command(
    config: BuildConfig,
    profile_key: str,
    host_install_dir: Path,
) -> tuple[str, ...]:
    build_dir = _runtime_header_stage_build_dir(config, profile_key)
    defines = _llvm_libc_header_stage_defines(config, profile_key, host_install_dir)
    command = [
        "cmake",
        "-G",
        config.generator,
        "-S",
        str(config.source_dir / "runtimes"),
        "-B",
        str(build_dir),
    ]
    command.extend(f"-D{name}={value}" for name, value in defines.items())
    command.extend(_cmake_define_args(config.cmake_defines))
    command.extend(
        _cmake_define_args(config.profile_cmake_defines.get(profile_key, {}))
    )
    return tuple(command)


def _runtime_configure_command(
    config: BuildConfig,
    profile_key: str,
    host_install_dir: Path,
) -> tuple[str, ...]:
    runtime_build_dir = config.build_root / f"runtimes-{profile_key}"
    defines = _runtime_default_defines(config, profile_key, host_install_dir)
    command = [
        "cmake",
        "-G",
        config.generator,
        "-S",
        str(_runtime_source_dir(config, profile_key)),
        "-B",
        str(runtime_build_dir),
    ]
    command.extend(f"-D{name}={value}" for name, value in defines.items())
    command.extend(_cmake_define_args(config.cmake_defines))
    command.extend(
        _cmake_define_args(config.profile_cmake_defines.get(profile_key, {}))
    )
    return tuple(command)


def _build_command(
    build_dir: Path,
    jobs: int | None,
    targets: Sequence[str],
) -> tuple[str, ...]:
    command = ["cmake", "--build", str(build_dir)]
    for target in targets:
        command.extend(["--target", target])
    command.append("--parallel")
    if jobs is not None:
        command.append(str(jobs))
    return tuple(command)


def _runtime_build_targets(profile_key: str) -> tuple[str, ...]:
    profile = RUNTIME_PROFILES[profile_key]
    if profile.full_build:
        return (
            "install-libc",
            "install-compiler-rt",
            "install-builtins",
            "install-cxx",
            "install-cxxabi",
            "install-unwind",
        )
    return ("install",)


def _cleanup_paths(config: BuildConfig) -> tuple[Path, ...]:
    paths: list[Path] = [config.build_root, config.install_root / "host-tools"]
    for profile_key in config.runtime_profiles:
        paths.append(_runtime_install_dir(config, profile_key))
        paths.append(_sysroot_build_workspace(config, profile_key))
        if config.managed_sysroots.get(profile_key, False):
            paths.append(config.sysroots[profile_key])
        if RUNTIME_PROFILES[profile_key].full_build:
            paths.append(config.bootstrap_sysroots[profile_key])
        if config.managed_gcc_toolchains.get(profile_key, False):
            paths.append(config.gcc_toolchains[profile_key])
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return tuple(deduped)


def _needs_sysroot_prepare(config: BuildConfig, profile_key: str) -> bool:
    profile = RUNTIME_PROFILES[profile_key]
    return (
        profile.full_build
        or config.managed_sysroots.get(profile_key, False)
        or config.managed_gcc_toolchains.get(profile_key, False)
    )


def _sysroot_step_metadata(config: BuildConfig, profile_key: str) -> dict[str, object]:
    profile = RUNTIME_PROFILES[profile_key]
    toolchain_dir = config.gcc_toolchains.get(profile_key)
    return {
        "profile_key": profile_key,
        "root_path": str(_runtime_sysroot_path(config, profile_key)),
        "managed_root_path": profile.full_build or config.managed_sysroots.get(profile_key, False),
        "final_sysroot": str(config.sysroots[profile_key]),
        "toolchain_dir": str(toolchain_dir) if toolchain_dir else "",
        "managed_gcc_toolchain": config.managed_gcc_toolchains.get(profile_key, False),
        "downloads_dir": str(config.cache_root / "downloads"),
        "sources_dir": str(config.cache_root / "sources"),
        "build_dir": str(_sysroot_build_workspace(config, profile_key)),
        "target_triple": _profile_target_triple(config, profile_key),
        "base_c_library": profile.c_library,
        "cpp_library": profile.cpp_library,
        "linux_version": config.linux_version,
        "glibc_version": config.glibc_version,
        "libstdcxx_version": config.libstdcxx_version,
        "musl_version": config.musl_version,
        "jobs": config.jobs or 0,
    }


def _wrapper_metadata(config: BuildConfig, profile_key: str, host_install_dir: Path) -> dict[str, object]:
    profile = RUNTIME_PROFILES[profile_key]
    profile_dir = _runtime_install_dir(config, profile_key)
    runtime_prefix = _runtime_install_prefix(config, profile_key)
    uses_compiler_rt = "compiler-rt" in profile.runtimes
    runtime_include_dir = str(runtime_prefix / "include" / "c++" / "v1") if profile.uses_libcxx else ""
    runtime_lib_dir = str(runtime_prefix / "lib") if profile.uses_libcxx else ""
    metadata: dict[str, object] = {
        "profile_key": profile.key,
        "profile_dir": str(profile_dir),
        "runtime_prefix": str(runtime_prefix),
        "host_install_dir": str(host_install_dir),
        "projects": list(config.projects),
        "target_triple": _profile_target_triple(config, profile_key),
        "sysroot": str(config.sysroots[profile_key]) if profile_key in config.sysroots else "",
        "gcc_toolchain": (
            str(config.gcc_toolchains[profile_key])
            if profile_key in config.gcc_toolchains
            else ""
        ),
        "uses_libcxx": profile.uses_libcxx,
        "uses_compiler_rt": uses_compiler_rt,
        "uses_llvm_libc": profile.full_build,
        "runtime_include_dir": runtime_include_dir,
        "runtime_lib_dir": runtime_lib_dir,
        "c_library": profile.c_library,
        "cpp_library": profile.cpp_library,
        "linux_version": config.linux_version,
        "glibc_version": config.glibc_version,
        "libstdcxx_version": config.libstdcxx_version,
        "musl_version": config.musl_version,
        "notes": list(profile.notes),
    }
    return metadata


def build_plan(config: BuildConfig) -> BuildPlan:
    resolved = _resolve_config(config)
    host_build_dir = resolved.build_root / "host-tools"
    host_install_dir = resolved.install_root / "host-tools"
    steps: list[PlanStep] = []

    if resolved.clean:
        steps.append(
            PlanStep(
                name="cleanup-build-cache",
                description="Remove cached build trees, managed sysroots, and generated profile outputs.",
                kind="cleanup",
                metadata={"paths": [str(path) for path in _cleanup_paths(resolved)]},
            )
        )

    for profile_key in resolved.runtime_profiles:
        if _needs_sysroot_prepare(resolved, profile_key):
            steps.append(
                PlanStep(
                    name=f"prepare-sysroot-{profile_key}",
                    description=f"Download and prepare the managed sysroot inputs for {profile_key}.",
                    kind="sysroot",
                    profile=profile_key,
                    metadata=_sysroot_step_metadata(resolved, profile_key),
                )
            )

    steps.extend(
        [
            PlanStep(
                name="configure-host-tools",
                description="Configure the host LLVM tools build.",
                kind="command",
                command=_host_configure_command(resolved, host_build_dir, host_install_dir),
            ),
            PlanStep(
                name="build-host-tools",
                description="Build and install the host clang/lld/lldb toolchain.",
                kind="command",
                command=_build_command(
                    host_build_dir,
                    resolved.jobs,
                    resolved.host_build_targets or ("install",),
                ),
            ),
        ]
    )

    for profile_key in resolved.runtime_profiles:
        profile = RUNTIME_PROFILES[profile_key]
        if profile.runtimes:
            if profile.full_build:
                header_stage_build_dir = _runtime_header_stage_build_dir(resolved, profile_key)
                steps.append(
                    PlanStep(
                        name=f"configure-libc-headers-{profile_key}",
                        description=f"Configure standalone llvm-libc header staging for {profile_key}.",
                        kind="command",
                        profile=profile_key,
                        command=_llvm_libc_header_stage_configure_command(
                            resolved,
                            profile_key,
                            host_install_dir,
                        ),
                    )
                )
                steps.append(
                    PlanStep(
                        name=f"build-libc-headers-{profile_key}",
                        description=f"Generate and install llvm-libc public headers for {profile_key}.",
                        kind="command",
                        profile=profile_key,
                        command=_build_command(
                            header_stage_build_dir,
                            resolved.jobs,
                            ("install-libc-headers",),
                        ),
                    )
                )
            runtime_build_dir = resolved.build_root / f"runtimes-{profile_key}"
            steps.append(
                PlanStep(
                    name=f"configure-runtimes-{profile_key}",
                    description=f"Configure LLVM runtimes for {profile_key}.",
                    kind="command",
                    profile=profile_key,
                    command=_runtime_configure_command(resolved, profile_key, host_install_dir),
                )
            )
            steps.append(
                PlanStep(
                    name=f"build-runtimes-{profile_key}",
                    description=f"Build and install LLVM runtimes for {profile_key}.",
                    kind="command",
                    profile=profile_key,
                    command=_build_command(
                        runtime_build_dir,
                        resolved.jobs,
                        _runtime_build_targets(profile_key),
                    ),
                )
            )
        steps.append(
            PlanStep(
                name=f"materialize-profile-{profile_key}",
                description=f"Create wrapper binaries and metadata for {profile_key}.",
                kind="wrappers",
                profile=profile_key,
                metadata=_wrapper_metadata(resolved, profile_key, host_install_dir),
            )
        )
    return BuildPlan(
        config=resolved,
        host_build_dir=host_build_dir,
        host_install_dir=host_install_dir,
        steps=tuple(steps),
    )