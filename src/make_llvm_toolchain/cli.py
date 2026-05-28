from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .planner import (
    BuildConfig,
    BuildPlan,
    PlannerError,
    RUNTIME_PROFILES,
    build_plan,
    normalize_runtime_key,
)
from .releases import (
    ReleaseVersionError,
    ResolvedComponentVersions,
    llvm_git_ref_for_version,
    resolve_component_versions,
    resolve_latest_llvm_release_version,
)
from .sysroot import SysrootBuildSpec, SysrootError, ensure_sysroot


DEFAULT_LLVM_GIT_URL = "https://github.com/llvm/llvm-project.git"


@dataclass(frozen=True)
class SourceBootstrapConfig:
    git_url: str = DEFAULT_LLVM_GIT_URL
    git_ref: str | None = None
    git_depth: int | None = 1
    auto_download: bool = True


def _absolute_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _parse_key_value(
    items: Sequence[str],
    option_name: str,
    *,
    normalize_keys: bool = True,
    key_name: str = "PROFILE",
) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise PlannerError(f"{option_name} expects {key_name}=VALUE, got: {item}")
        key, value = item.split("=", 1)
        values[normalize_runtime_key(key) if normalize_keys else key] = value
    return values


def _parse_path_map(items: Sequence[str], option_name: str) -> dict[str, Path]:
    return {key: Path(value) for key, value in _parse_key_value(items, option_name).items()}


def _parse_profile_cmake_defines(items: Sequence[str]) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for item in items:
        if ":" not in item or "=" not in item:
            raise PlannerError(
                "--profile-cmake-define expects PROFILE:KEY=VALUE, got: "
                f"{item}"
            )
        profile_and_key, value = item.split("=", 1)
        profile, key = profile_and_key.split(":", 1)
        normalized_profile = normalize_runtime_key(profile)
        values.setdefault(normalized_profile, {})[key] = value
    return values


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("llvm-project"),
        help="Path to an llvm-project checkout.",
    )
    parser.add_argument(
        "--build-root",
        type=Path,
        default=Path("build"),
        help="Directory used for intermediate build trees.",
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        default=Path("toolchains"),
        help="Directory used for the host toolchain and runtime profile outputs.",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path(".cache"),
        help="Directory used for downloaded archives, managed sysroots, and transient sysroot build workspaces.",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Repeat to select any subset of clang, lld, lldb. Defaults to all three.",
    )
    parser.add_argument(
        "--runtime",
        action="append",
        default=[],
        help=(
            "Repeat to select runtime profiles. Accepts forms like glibc+libc++, "
            "musl+libstdc++, or llvm-libc+libc++."
        ),
    )
    parser.add_argument(
        "--sysroot",
        action="append",
        default=[],
        help="Repeat PROFILE=PATH for musl or llvm-libc profiles.",
    )
    parser.add_argument(
        "--gcc-toolchain",
        action="append",
        default=[],
        help="Repeat PROFILE=PATH when libstdc++ lives outside the chosen sysroot.",
    )
    parser.add_argument(
        "--target-triple",
        action="append",
        default=[],
        help="Repeat PROFILE=TRIPLE to override a profile's default target triple.",
    )
    parser.add_argument(
        "--cmake-define",
        action="append",
        default=[],
        help="Repeat KEY=VALUE to append global CMake cache entries.",
    )
    parser.add_argument(
        "--profile-cmake-define",
        action="append",
        default=[],
        help="Repeat PROFILE:KEY=VALUE to append CMake cache entries to one runtime profile.",
    )
    parser.add_argument(
        "--build-type",
        default="Release",
        help="CMAKE_BUILD_TYPE for host and runtime builds.",
    )
    parser.add_argument(
        "--generator",
        default="Ninja",
        help="CMake generator to use.",
    )
    parser.add_argument(
        "--targets-to-build",
        default="host",
        help="LLVM_TARGETS_TO_BUILD for the host tools build.",
    )
    parser.add_argument(
        "--host-build-target",
        action="append",
        default=[],
        help=(
            "Repeat to override the host build targets passed to cmake --build. "
            "Defaults to install."
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="Parallel job count passed to cmake --build --parallel.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete managed build caches and generated outputs before executing the plan.",
    )
    parser.add_argument(
        "--assertions",
        action="store_true",
        help="Enable LLVM assertions in the host tools build.",
    )
    parser.add_argument(
        "--host-cc",
        help="Override CMAKE_C_COMPILER for the host tools build.",
    )
    parser.add_argument(
        "--host-cxx",
        help="Override CMAKE_CXX_COMPILER for the host tools build.",
    )
    parser.add_argument(
        "--llvm-version",
        help=(
            "LLVM release version used when bootstrapping llvm-project. "
            "If omitted, the latest published release is used."
        ),
    )
    parser.add_argument(
        "--llvm-git-url",
        default=DEFAULT_LLVM_GIT_URL,
        help="Git URL used to bootstrap llvm-project when --source-dir is missing.",
    )
    parser.add_argument(
        "--llvm-git-ref",
        help="Optional branch or tag to clone when bootstrapping llvm-project. Overrides --llvm-version.",
    )
    parser.add_argument(
        "--llvm-git-depth",
        type=int,
        default=1,
        help="Shallow clone depth used when bootstrapping llvm-project. Use 0 for a full clone.",
    )
    parser.add_argument(
        "--skip-source-download",
        action="store_true",
        help="Disable automatic llvm-project download when the source tree is missing.",
    )
    parser.add_argument(
        "--linux-version",
        help="Linux kernel headers version used when building managed sysroots. Defaults to the latest published stable release.",
    )
    parser.add_argument(
        "--glibc-version",
        help="glibc version used when building managed glibc bootstrap/sysroot images. Defaults to the latest published release.",
    )
    parser.add_argument(
        "--libstdcxx-version",
        help="GCC/libstdc++ version used when building managed libstdc++ toolchains. Defaults to the latest published release.",
    )
    parser.add_argument(
        "--musl-version",
        help="musl version used when building managed musl sysroots. Defaults to the latest published release.",
    )


def _resolve_component_versions_from_args(args: argparse.Namespace) -> ResolvedComponentVersions:
    try:
        return resolve_component_versions(
            linux_version=args.linux_version,
            glibc_version=args.glibc_version,
            libstdcxx_version=args.libstdcxx_version,
            musl_version=args.musl_version,
        )
    except ReleaseVersionError as error:
        raise PlannerError(str(error)) from error


def _build_config_from_args(
    args: argparse.Namespace,
    resolved_versions: ResolvedComponentVersions | None = None,
) -> BuildConfig:
    source_dir = _absolute_path(args.source_dir)
    build_root = _absolute_path(args.build_root)
    install_root = _absolute_path(args.install_root)
    cache_root = _absolute_path(args.cache_root)
    versions = resolved_versions or _resolve_component_versions_from_args(args)
    cmake_defines = _parse_key_value(
        args.cmake_define,
        "--cmake-define",
        normalize_keys=False,
        key_name="KEY",
    )
    profile_cmake_defines = _parse_profile_cmake_defines(args.profile_cmake_define)
    return BuildConfig(
        source_dir=source_dir,
        build_root=build_root,
        install_root=install_root,
        cache_root=cache_root,
        projects=tuple(args.project),
        runtime_profiles=tuple(args.runtime),
        build_type=args.build_type,
        generator=args.generator,
        targets_to_build=args.targets_to_build,
        host_build_targets=tuple(args.host_build_target),
        jobs=args.jobs,
        clean=args.clean,
        assertions=args.assertions,
        host_c_compiler=args.host_cc,
        host_cxx_compiler=args.host_cxx,
        sysroots=_parse_path_map(args.sysroot, "--sysroot"),
        gcc_toolchains=_parse_path_map(args.gcc_toolchain, "--gcc-toolchain"),
        target_triples=_parse_key_value(args.target_triple, "--target-triple"),
        cmake_defines=cmake_defines,
        profile_cmake_defines=profile_cmake_defines,
        linux_version=versions.linux_version,
        glibc_version=versions.glibc_version,
        libstdcxx_version=versions.libstdcxx_version,
        musl_version=versions.musl_version,
    )


def _build_source_bootstrap_config(args: argparse.Namespace) -> SourceBootstrapConfig:
    git_depth = args.llvm_git_depth
    if git_depth is not None and git_depth < 0:
        raise PlannerError("--llvm-git-depth must be greater than or equal to 0.")
    llvm_version = args.llvm_version.strip() if args.llvm_version else None
    if llvm_version and llvm_version.lower() == "latest":
        llvm_version = None
    if llvm_version and args.llvm_git_ref:
        raise PlannerError("--llvm-version cannot be combined with --llvm-git-ref.")
    git_ref = args.llvm_git_ref
    if llvm_version:
        try:
            git_ref = llvm_git_ref_for_version(llvm_version)
        except ReleaseVersionError as error:
            raise PlannerError(str(error)) from error
    return SourceBootstrapConfig(
        git_url=args.llvm_git_url,
        git_ref=git_ref,
        git_depth=None if git_depth == 0 else git_depth,
        auto_download=not args.skip_source_download,
    )


def _resolve_source_bootstrap_for_clone(bootstrap: SourceBootstrapConfig) -> SourceBootstrapConfig:
    if bootstrap.git_ref:
        return bootstrap
    try:
        latest_version = resolve_latest_llvm_release_version()
        latest_ref = llvm_git_ref_for_version(latest_version)
    except ReleaseVersionError as error:
        raise PlannerError(str(error)) from error
    return replace(bootstrap, git_ref=latest_ref)


def _expected_source_entries(plan: BuildPlan) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = [
        ("llvm", plan.config.source_dir / "llvm" / "CMakeLists.txt"),
    ]
    for project in plan.config.projects:
        entries.append((project, plan.config.source_dir / project / "CMakeLists.txt"))
    needs_runtimes = any(
        RUNTIME_PROFILES[profile_key].runtimes for profile_key in plan.config.runtime_profiles
    )
    if needs_runtimes:
        entries.append(("runtimes", plan.config.source_dir / "runtimes" / "CMakeLists.txt"))
    return entries


def _missing_source_entries(plan: BuildPlan) -> list[tuple[str, Path]]:
    return [entry for entry in _expected_source_entries(plan) if not entry[1].exists()]


def _clone_llvm_project(
    source_dir: Path,
    bootstrap: SourceBootstrapConfig,
    verbose: bool = False,
) -> None:
    clone_command = ["git", "clone"]
    if bootstrap.git_depth is not None:
        clone_command.extend(["--depth", str(bootstrap.git_depth)])
    if bootstrap.git_ref:
        clone_command.extend(["--branch", bootstrap.git_ref, "--single-branch"])
    clone_command.extend([bootstrap.git_url, str(source_dir)])
    if verbose:
        print(_render_command(clone_command), file=sys.stderr)
    try:
        subprocess.run(clone_command, check=True)
    except FileNotFoundError as error:
        raise PlannerError(
            "git is required to download llvm-project automatically. "
            "Install git or pass an existing --source-dir checkout."
        ) from error
    except subprocess.CalledProcessError as error:
        raise PlannerError(
            f"Failed to clone llvm-project from {bootstrap.git_url}. git exited with {error.returncode}."
        ) from error


def _ensure_source_checkout(
    plan: BuildPlan,
    bootstrap: SourceBootstrapConfig,
    verbose: bool = False,
) -> None:
    missing = _missing_source_entries(plan)
    if not missing:
        return

    missing_paths = ", ".join(str(path) for _, path in missing)
    source_dir = plan.config.source_dir
    if not bootstrap.auto_download:
        raise PlannerError(
            "The source tree must point at an llvm-project checkout containing the requested sources. "
            f"Missing: {missing_paths}"
        )

    if source_dir.exists():
        if not source_dir.is_dir():
            raise PlannerError(f"--source-dir exists but is not a directory: {source_dir}")
        if any(source_dir.iterdir()):
            raise PlannerError(
                "The source directory exists but is not a usable llvm-project checkout, and automatic "
                f"download will not overwrite a non-empty directory. Missing: {missing_paths}"
            )
    else:
        source_dir.parent.mkdir(parents=True, exist_ok=True)

    bootstrap = _resolve_source_bootstrap_for_clone(bootstrap)
    clone_target = bootstrap.git_ref or bootstrap.git_url

    print(
        f"llvm-project sources are missing under {source_dir}; cloning from {bootstrap.git_url} ({clone_target})",
        file=sys.stderr,
    )
    _clone_llvm_project(source_dir, bootstrap, verbose=verbose)

    missing_after_bootstrap = _missing_source_entries(plan)
    if missing_after_bootstrap:
        missing_after_paths = ", ".join(str(path) for _, path in missing_after_bootstrap)
        raise PlannerError(
            "The downloaded source tree is incomplete for the requested build. "
            f"Missing: {missing_after_paths}"
        )


def _render_command(command: Sequence[str]) -> str:
    return shlex.join(command)


def _print_plan(plan: BuildPlan) -> None:
    print(f"Host tools install: {plan.host_install_dir}")
    print("Steps:")
    for index, step in enumerate(plan.steps, start=1):
        if step.kind == "command" and step.command is not None:
            print(f"{index}. {step.name}: {_render_command(step.command)}")
        elif step.kind == "cleanup":
            paths = ", ".join(step.metadata.get("paths", []))
            print(f"{index}. {step.name}: remove {paths}")
        elif step.kind == "sysroot":
            toolchain = step.metadata.get("toolchain_dir")
            target = str(step.metadata["root_path"])
            if toolchain:
                print(f"{index}. {step.name}: prepare sysroot {target} and GCC toolchain {toolchain}")
            else:
                print(f"{index}. {step.name}: prepare sysroot {target}")
        else:
            profile_dir = Path(str(step.metadata["profile_dir"]))
            print(f"{index}. {step.name}: generate wrappers in {profile_dir}")


def _host_bin_from_metadata(metadata: dict[str, object]) -> Path:
    return Path(str(metadata["host_install_dir"])) / "bin"


def _profile_flags(metadata: dict[str, object], cxx: bool) -> list[str]:
    flags = [f"--target={metadata['target_triple']}"]
    sysroot = str(metadata["sysroot"])
    if sysroot:
        flags.append(f"--sysroot={sysroot}")
    gcc_toolchain = str(metadata["gcc_toolchain"])
    if gcc_toolchain:
        flags.append(f"--gcc-toolchain={gcc_toolchain}")
    if bool(metadata["uses_llvm_libc"]):
        flags.extend(["--rtlib=compiler-rt", "--unwindlib=none", "-static"])
    elif bool(metadata["uses_compiler_rt"]):
        flags.append("--rtlib=compiler-rt")
        if cxx:
            flags.append("--unwindlib=libunwind")
    if cxx:
        if bool(metadata["uses_libcxx"]):
            runtime_include_dir = str(metadata["runtime_include_dir"])
            runtime_lib_dir = str(metadata["runtime_lib_dir"])
            flags.extend(
                [
                    "-stdlib=libc++",
                    "-isystem",
                    runtime_include_dir,
                    "-L",
                    runtime_lib_dir,
                    f"-Wl,-rpath,{runtime_lib_dir}",
                ]
            )
        else:
            flags.append("-stdlib=libstdc++")
    return flags


def _shell_array_lines(values: Sequence[str]) -> list[str]:
    return [f"  {shlex.quote(value)}" for value in values]


def _write_text(path: Path, content: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o755)


def _wrapper_script(binary_name: str, host_bin: Path, default_flags: Sequence[str]) -> str:
    flag_lines = _shell_array_lines(default_flags)
    joined_flags = "\n".join(flag_lines)
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"host_bin={shlex.quote(str(host_bin / binary_name))}\n"
        "default_flags=(\n"
        f"{joined_flags}\n"
        ")\n\n"
        "exec \"$host_bin\" \"${default_flags[@]}\" \"$@\"\n"
    )


def _forwarder_script(binary_name: str, host_bin: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"exec {shlex.quote(str(host_bin / binary_name))} \"$@\"\n"
    )


def _materialize_profile(step_metadata: dict[str, object]) -> None:
    profile_dir = Path(str(step_metadata["profile_dir"]))
    profile_bin = profile_dir / "bin"
    host_bin = _host_bin_from_metadata(step_metadata)
    profile_bin.mkdir(parents=True, exist_ok=True)

    _write_text(
        profile_bin / "clang",
        _wrapper_script("clang", host_bin, _profile_flags(step_metadata, cxx=False)),
        executable=True,
    )
    _write_text(
        profile_bin / "clang++",
        _wrapper_script("clang++", host_bin, _profile_flags(step_metadata, cxx=True)),
        executable=True,
    )

    project_to_binary = {
        "lld": ["lld", "ld.lld"],
        "lldb": ["lldb"],
    }
    selected_projects = set(step_metadata["projects"])
    for project, binaries in project_to_binary.items():
        if project not in selected_projects:
            continue
        for binary_name in binaries:
            _write_text(
                profile_bin / binary_name,
                _forwarder_script(binary_name, host_bin),
                executable=True,
            )

    manifest_dir = profile_dir / "share" / "make-llvm-toolchain"
    manifest = {
        "profile": step_metadata["profile_key"],
        "target_triple": step_metadata["target_triple"],
        "c_library": step_metadata["c_library"],
        "cpp_library": step_metadata["cpp_library"],
        "linux_version": step_metadata["linux_version"],
        "glibc_version": step_metadata["glibc_version"],
        "libstdcxx_version": step_metadata["libstdcxx_version"],
        "musl_version": step_metadata["musl_version"],
        "sysroot": step_metadata["sysroot"] or None,
        "gcc_toolchain": step_metadata["gcc_toolchain"] or None,
        "host_toolchain": str(Path(str(step_metadata["host_install_dir"]))),
        "runtime_include_dir": step_metadata["runtime_include_dir"],
        "runtime_lib_dir": step_metadata["runtime_lib_dir"],
        "projects": step_metadata["projects"],
        "notes": step_metadata["notes"],
    }
    _write_text(
        manifest_dir / "profile.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _remove_paths(paths: Sequence[str], verbose: bool) -> None:
    for raw_path in paths:
        path = Path(raw_path)
        if verbose:
            print(f"rm -rf {shlex.quote(str(path))}")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)


def _run_plan(plan: BuildPlan, verbose: bool) -> None:
    for step in plan.steps:
        if step.kind == "command":
            assert step.command is not None
            if verbose:
                print(_render_command(step.command))
            subprocess.run(step.command, check=True)
            continue
        if step.kind == "cleanup":
            _remove_paths(tuple(str(path) for path in step.metadata.get("paths", [])), verbose)
            continue
        if step.kind == "sysroot":
            ensure_sysroot(SysrootBuildSpec.from_metadata(step.metadata), verbose=verbose)
            continue
        _materialize_profile(dict(step.metadata))
        if verbose:
            print(f"materialized {step.profile} -> {step.metadata['profile_dir']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make-llvm-toolchain",
        description="Plan or build LLVM host tools plus one or more runtime profiles.",
    )
    subparsers = parser.add_subparsers(dest="command")

    plan_parser = subparsers.add_parser("plan", help="Print the CMake/build plan.")
    _common_arguments(plan_parser)

    build_parser = subparsers.add_parser("build", help="Execute the generated plan.")
    _common_arguments(build_parser)
    build_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each command before it runs.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "plan"
    if args.command is None:
        args = parser.parse_args([command, *(argv or sys.argv[1:])])
    try:
        resolved_versions = _resolve_component_versions_from_args(args)
        config = _build_config_from_args(args, resolved_versions)
        source_bootstrap = _build_source_bootstrap_config(args)
        plan = build_plan(config)
        _ensure_source_checkout(plan, source_bootstrap, verbose=getattr(args, "verbose", False))
        if command == "plan":
            _print_plan(plan)
            return 0
        _run_plan(plan, verbose=args.verbose)
        return 0
    except PlannerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except SysrootError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"error: command failed with exit code {error.returncode}", file=sys.stderr)
        return error.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())