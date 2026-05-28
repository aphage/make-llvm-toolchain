from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SysrootError(RuntimeError):
    """Raised when preparing a managed sysroot or GCC toolchain fails."""


@dataclass(frozen=True)
class SysrootBuildSpec:
    profile_key: str
    root_path: Path
    managed_root_path: bool
    final_sysroot: Path
    toolchain_dir: Path | None
    managed_gcc_toolchain: bool
    downloads_dir: Path
    sources_dir: Path
    build_dir: Path
    target_triple: str
    base_c_library: str
    cpp_library: str
    linux_version: str
    glibc_version: str
    libstdcxx_version: str
    musl_version: str
    jobs: int | None = None

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, object]) -> "SysrootBuildSpec":
        jobs = int(metadata["jobs"]) if metadata.get("jobs") else None
        toolchain_dir = metadata.get("toolchain_dir")
        return cls(
            profile_key=str(metadata["profile_key"]),
            root_path=Path(str(metadata["root_path"])),
            managed_root_path=bool(metadata["managed_root_path"]),
            final_sysroot=Path(str(metadata["final_sysroot"])),
            toolchain_dir=Path(str(toolchain_dir)) if toolchain_dir else None,
            managed_gcc_toolchain=bool(metadata["managed_gcc_toolchain"]),
            downloads_dir=Path(str(metadata["downloads_dir"])),
            sources_dir=Path(str(metadata["sources_dir"])),
            build_dir=Path(str(metadata["build_dir"])),
            target_triple=str(metadata["target_triple"]),
            base_c_library=str(metadata["base_c_library"]),
            cpp_library=str(metadata["cpp_library"]),
            linux_version=str(metadata["linux_version"]),
            glibc_version=str(metadata["glibc_version"]),
            libstdcxx_version=str(metadata["libstdcxx_version"]),
            musl_version=str(metadata["musl_version"]),
            jobs=jobs,
        )


def ensure_sysroot(spec: SysrootBuildSpec, verbose: bool = False) -> None:
    spec.downloads_dir.mkdir(parents=True, exist_ok=True)
    spec.sources_dir.mkdir(parents=True, exist_ok=True)
    spec.build_dir.mkdir(parents=True, exist_ok=True)

    if spec.managed_root_path:
        _ensure_root_sysroot(spec, verbose)
    elif not spec.root_path.exists():
        raise SysrootError(
            f"Managed sysroot creation is disabled for {spec.profile_key}, but {spec.root_path} does not exist."
        )

    if spec.managed_gcc_toolchain:
        if spec.toolchain_dir is None:
            raise SysrootError(f"Missing GCC toolchain directory for {spec.profile_key}.")
        _ensure_gcc_toolchain(spec, verbose)


def _ensure_root_sysroot(spec: SysrootBuildSpec, verbose: bool) -> None:
    manifest_path = spec.root_path / ".make-llvm-toolchain-sysroot.json"
    expected_manifest = {
        "profile": spec.profile_key,
        "root_path": str(spec.root_path),
        "final_sysroot": str(spec.final_sysroot),
        "target_triple": spec.target_triple,
        "base_c_library": spec.base_c_library,
        "linux_version": spec.linux_version,
        "glibc_version": spec.glibc_version,
        "musl_version": spec.musl_version,
    }
    if _manifest_matches(manifest_path, expected_manifest):
        return

    _reset_directory(spec.root_path)
    _reset_directory(spec.build_dir / "root")

    _build_linux_headers(spec, verbose)
    if spec.base_c_library == "musl":
        _build_musl(spec, verbose)
    elif spec.base_c_library == "glibc":
        _build_glibc(spec, verbose)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_gcc_toolchain(spec: SysrootBuildSpec, verbose: bool) -> None:
    assert spec.toolchain_dir is not None
    manifest_path = spec.toolchain_dir / ".make-llvm-toolchain-gcc.json"
    expected_manifest = {
        "profile": spec.profile_key,
        "toolchain_dir": str(spec.toolchain_dir),
        "target_triple": _gcc_target_triple(spec),
        "libstdcxx_version": spec.libstdcxx_version,
        "sysroot": str(spec.root_path),
    }
    if _manifest_matches(manifest_path, expected_manifest):
        return

    _reset_directory(spec.toolchain_dir)
    _reset_directory(spec.build_dir / "gcc")
    _build_gcc_toolchain(spec, verbose)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_matches(path: Path, expected: Mapping[str, object]) -> bool:
    if not path.exists():
        return False
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return current == dict(expected)


def _reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _build_linux_headers(spec: SysrootBuildSpec, verbose: bool) -> None:
    source_dir = _prepare_source_tree(
        spec.sources_dir,
        spec.downloads_dir,
        f"linux-{spec.linux_version}",
        _linux_kernel_url(spec.linux_version),
    )
    headers_dir = spec.build_dir / "root" / "linux-headers"
    headers_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "make",
            "-C",
            str(source_dir),
            f"ARCH={_linux_arch(spec.target_triple)}",
            "headers_install",
            f"INSTALL_HDR_PATH={headers_dir}",
        ],
        verbose=verbose,
    )
    include_root = spec.root_path / "usr" / "include"
    include_root.mkdir(parents=True, exist_ok=True)
    _copy_tree(headers_dir / "include", include_root)


def _build_musl(spec: SysrootBuildSpec, verbose: bool) -> None:
    source_dir = _prepare_source_tree(
        spec.sources_dir,
        spec.downloads_dir,
        f"musl-{spec.musl_version}",
        f"https://musl.libc.org/releases/musl-{spec.musl_version}.tar.gz",
    )
    build_dir = spec.build_dir / "root" / "musl"
    build_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env()
    _run(
        [
            str(source_dir / "configure"),
            "--prefix=/usr",
            f"--target={_gnu_target_triple(spec.target_triple, 'musl')}",
            "--syslibdir=/lib",
        ],
        cwd=build_dir,
        env=env,
        verbose=verbose,
    )
    _run(_make_command(spec.jobs), cwd=build_dir, env=env, verbose=verbose)
    _run(
        ["make", f"DESTDIR={spec.root_path}", "install"],
        cwd=build_dir,
        env=env,
        verbose=verbose,
    )


def _build_glibc(spec: SysrootBuildSpec, verbose: bool) -> None:
    source_dir = _prepare_source_tree(
        spec.sources_dir,
        spec.downloads_dir,
        f"glibc-{spec.glibc_version}",
        f"https://ftp.gnu.org/gnu/glibc/glibc-{spec.glibc_version}.tar.xz",
    )
    build_dir = spec.build_dir / "root" / "glibc"
    build_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env()
    _append_env_flag(env, "CPPFLAGS", "-U_FORTIFY_SOURCE")
    headers_root = spec.root_path / "usr" / "include"
    configure_command = [
        str(source_dir / "configure"),
        "--prefix=/usr",
        "--libdir=/usr/lib",
        f"--host={_build_machine_triple()}",
        f"--with-headers={headers_root}",
        "--with-selinux=no",
        f"--enable-kernel={spec.linux_version}",
        "--disable-werror",
    ]
    _run(configure_command, cwd=build_dir, env=env, verbose=verbose)
    _run(_make_command(spec.jobs), cwd=build_dir, env=env, verbose=verbose)
    _run(
        ["make", "install", f"DESTDIR={spec.root_path}"],
        cwd=build_dir,
        env=env,
        verbose=verbose,
    )


def _build_gcc_toolchain(spec: SysrootBuildSpec, verbose: bool) -> None:
    assert spec.toolchain_dir is not None
    source_dir = _prepare_source_tree(
        spec.sources_dir,
        spec.downloads_dir,
        f"gcc-{spec.libstdcxx_version}",
        f"https://ftp.gnu.org/gnu/gcc/gcc-{spec.libstdcxx_version}/gcc-{spec.libstdcxx_version}.tar.xz",
    )
    _run(["bash", str(source_dir / "contrib" / "download_prerequisites")], cwd=source_dir, verbose=verbose)

    build_dir = spec.build_dir / "gcc"
    build_dir.mkdir(parents=True, exist_ok=True)
    gcc_target = _gcc_target_triple(spec)
    build_machine = _build_machine_triple()
    env = _build_env()
    configure_command = [
        str(source_dir / "configure"),
        f"--prefix={spec.toolchain_dir}",
        f"--target={gcc_target}",
        f"--build={build_machine}",
        f"--host={build_machine}",
        f"--with-sysroot={spec.root_path}",
        "--disable-bootstrap",
        "--disable-multilib",
        "--disable-nls",
        "--enable-languages=c,c++",
    ]
    _run(configure_command, cwd=build_dir, env=env, verbose=verbose)
    _run(
        _make_command(spec.jobs, "all-gcc", "all-target-libgcc", "all-target-libstdc++-v3"),
        cwd=build_dir,
        env=env,
        verbose=verbose,
    )
    _run(
        [
            "make",
            "install-gcc",
            "install-target-libgcc",
            "install-target-libstdc++-v3",
        ],
        cwd=build_dir,
        env=env,
        verbose=verbose,
    )


def _prepare_source_tree(sources_dir: Path, downloads_dir: Path, source_name: str, url: str) -> Path:
    extracted_dir = sources_dir / source_name
    if extracted_dir.exists():
        return extracted_dir

    archive_name = url.rsplit("/", 1)[-1]
    archive_path = downloads_dir / archive_name
    if not archive_path.exists():
        _download(url, archive_path)

    _extract_archive(archive_path, sources_dir)
    if extracted_dir.exists():
        return extracted_dir
    candidates = sorted(path for path in sources_dir.iterdir() if path.is_dir() and path.name.startswith(source_name))
    if len(candidates) == 1:
        return candidates[0]
    raise SysrootError(f"Could not locate extracted source directory for {source_name}.")


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            member_path = (destination / member.name).resolve()
            if not member_path.is_relative_to(root):
                raise SysrootError(f"Archive member escapes extraction root: {member.name}")
        archive.extractall(destination)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        raise SysrootError(f"Missing expected source tree: {source}")
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=True)


def _linux_kernel_url(version: str) -> str:
    major = version.split(".", 1)[0]
    return f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{version}.tar.xz"


def _linux_arch(target_triple: str) -> str:
    return target_triple.split("-", 1)[0]


def _gnu_target_triple(target_triple: str, c_library: str) -> str:
    arch = _linux_arch(target_triple)
    if c_library == "musl":
        return f"{arch}-linux-musl"
    return f"{arch}-linux-gnu"


def _gcc_target_triple(spec: SysrootBuildSpec) -> str:
    if spec.base_c_library == "musl":
        return _gnu_target_triple(spec.target_triple, "musl")
    return _build_machine_triple()


def _build_machine_triple() -> str:
    try:
        result = subprocess.run(
            ["gcc", "-dumpmachine"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise SysrootError("gcc is required to build managed glibc/libstdc++ sysroots.") from error
    except subprocess.CalledProcessError as error:
        raise SysrootError(f"gcc -dumpmachine failed with exit code {error.returncode}.") from error
    return result.stdout.strip()


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("CC", "gcc")
    env.setdefault("CXX", "g++")
    env.setdefault("AR", "ar")
    env.setdefault("RANLIB", "ranlib")
    return env


def _append_env_flag(env: dict[str, str], key: str, flag: str) -> None:
    current = env.get(key, "").strip()
    if not current:
        env[key] = flag
        return
    parts = shlex.split(current)
    if flag not in parts:
        env[key] = f"{current} {flag}"


def _make_command(jobs: int | None, *targets: str) -> list[str]:
    command = ["make"]
    if jobs:
        command.append(f"-j{jobs}")
    command.extend(targets)
    return command


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    verbose: bool = False,
) -> None:
    if verbose:
        rendered = shlex.join(command)
        if cwd is not None:
            rendered = f"(cd {shlex.quote(str(cwd))} && {rendered})"
        print(rendered)
    try:
        subprocess.run(command, cwd=cwd, env=dict(env) if env is not None else None, check=True)
    except FileNotFoundError as error:
        raise SysrootError(f"Missing build tool while running: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise SysrootError(
            f"Command failed while preparing sysroot/toolchain: {shlex.join(command)} "
            f"(exit code {error.returncode})."
        ) from error