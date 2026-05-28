# make-llvm-toolchain

一个面向 `llvm-project` 的脚本项目，用来构建主机侧 `clang`、`lld`、`lldb`，并按 runtime profile 生成一个或多个独立的工具链包装目录。

从当前版本开始，工具还会在用户没有显式提供 `--sysroot` / `--gcc-toolchain` 时，自动下载并构建一套受管的 sysroot 组件。可配置的组件版本包括 Linux kernel headers、glibc、musl、以及 GCC/libstdc++；其中 glibc 和 musl 只会在所选 profile 真正需要时才构建。

## 支持矩阵

- `glibc + libstdc++`
- `glibc + libc++`
- `musl + libstdc++`
- `musl + libc++`
- `llvm-libc + libc++`

`llvm-libc + libstdc++` 在 v1 明确拒绝。

## 设计

脚本把构建拆成两层：

1. 先构建一份主机工具链，只包含 `clang`、`lld`、`lldb`。
2. 再按所选 runtime profile 先准备受管 sysroot / GCC toolchain，再决定是否单独构建 LLVM runtimes，并在每个 profile 目录里生成包装好的 `clang` / `clang++` 启动脚本。

这样可以避免为了 `musl` 或 `llvm-libc` profile 去交叉编译 `lldb` 这类主机工具，同时允许一次生成多个 runtime 变体。

其中 `llvm-libc + libc++` 不是走 `runtimes/` 的 standalone 配置，而是走 LLVM 上游推荐的 top-level `llvm/` bootstrap build。原因是这条路径需要用 `clang` 参与完整 sysroot/runtime 组装，直接把 `llvm-libc` 当作普通 standalone runtime 更容易在配置阶段被不完整 sysroot 卡住。

`llvm-libc + libc++` 现在会额外使用一套受管的 bootstrap sysroot 作为配置和 compiler-rt/builtins 的输入，而最终的 `llvm-libc` 安装仍然写入你指定的 final sysroot。这套 bootstrap sysroot 只准备内核头文件等必要输入，不会再额外编译 glibc 或 musl，从而保持 final sysroot 更纯净。

## 目录输出

- `toolchains/host-tools`: 主机工具链安装目录。
- `toolchains/profiles/<profile>`: 某个 runtime profile 的包装目录和运行时安装目录。
- `toolchains/profiles/<profile>/share/make-llvm-toolchain/profile.json`: 该 profile 的元数据。
- `.cache/downloads`: 自动下载的源码压缩包缓存。
- `.cache/sources`: 自动解压后的第三方源码目录。
- `.cache/sysroots/<profile>`: 自动构建的 final sysroot。
- `.cache/bootstrap-sysroots/<profile>`: `llvm-libc + libc++` 专用 bootstrap sysroot。
- `.cache/gcc-toolchains/<profile>`: `libstdc++` profile 自动构建的 GCC/libstdc++ toolchain。
- `.cache/sysroot-builds/<profile>`: sysroot/toolchain 临时构建目录。

`llvm-libc + libc++` 是一个例外：包装脚本仍然放在 `toolchains/profiles/llvm-libc-libcxx`，但 runtime 本体会安装到你传入的 `--sysroot` 下的 `usr/`，也就是 `<sysroot>/usr`。

## 前置条件

- Python 3.10+
- CMake 3.20+
- Ninja
- GNU make
- GCC / G++，用于构建受管 glibc、musl、GCC/libstdc++ sysroot 组件
- git 和可访问 `github.com` 的网络，前提是你希望工具在缺失源码时自动下载 `llvm-project`
- 能编译 LLVM 的主机编译器
- 构建 `lldb` 时需要系统提供它依赖的 Python、SWIG、libedit、ncurses 等开发包
- 能访问 `cdn.kernel.org`、`ftp.gnu.org`、`musl.libc.org` 等上游源站，前提是你希望工具自动构建受管 sysroot
- `llvm-libc + libc++` 额外要求 `clang` 在 `--project` 选择里保持启用，因为 bootstrap build 需要它

## 源码检测与自动下载

运行 `plan` 或 `build` 时，工具会先检测当前 `--source-dir` 是否包含本次构建真正需要的源码：

- 始终检查 `llvm/`
- 按 `--project` 检查 `clang/`、`lld/`、`lldb/`
- 只有在所选 runtime profile 需要构建 LLVM runtimes 时，才检查 `runtimes/`

如果 `--source-dir` 不存在，或者存在但为空目录，工具会默认执行：

```bash
git clone --depth 1 --branch llvmorg-22.1.6 --single-branch https://github.com/llvm/llvm-project.git <source-dir>
```

可用选项：

- `--llvm-git-url URL`: 覆盖默认仓库地址。
- `--llvm-version VERSION`: 指定 LLVM 发布版本号（如 `22.1.6`），默认取最新 GitHub Release。会被转成对应的 `llvmorg-*` tag 传给 clone。
- `--llvm-git-ref REF`: 指定要克隆的 branch/tag，优先级高于 `--llvm-version`；与 `--llvm-version` 互斥。
- `--llvm-git-depth N`: 指定浅克隆深度，传 `0` 表示完整克隆。
- `--skip-source-download`: 禁用自动下载，源码缺失时直接报错。

如果 `--source-dir` 已经存在且不是空目录，但缺少必须的 `llvm` / `clang` / `lld` / `lldb` / `runtimes` 目录，工具不会覆盖该目录，而是直接报错。

## 用法

先打印计划：

```bash
./bin/make-llvm-toolchain plan \
  --source-dir /path/to/llvm-project \
  --runtime glibc+libstdc++ \
  --runtime glibc+libc++
```

构建 `glibc + libc++`：

```bash
./bin/make-llvm-toolchain build \
  --source-dir /path/to/llvm-project \
  --runtime glibc+libc++ \
  --clean \
  --linux-version 6.12.32 \
  --glibc-version 2.39
```

所有 `libc++` profile，也就是 `glibc + libc++`、`musl + libc++`、`llvm-libc + libc++`，现在都会统一把 runtime 构建偏向 `compiler-rt + libunwind`。planner 会默认追加 `LIBCXX_USE_COMPILER_RT=ON`、`COMPILER_RT_USE_BUILTINS_LIBRARY=ON`、`COMPILER_RT_USE_LLVM_UNWINDER=ON`、`LIBUNWIND_USE_COMPILER_RT=ON`，避免某些 profile 仍然隐式回退到 `libgcc`。

同时生成 `musl + libstdc++` 与 `musl + libc++`：

```bash
./bin/make-llvm-toolchain build \
  --source-dir /path/to/llvm-project \
  --runtime musl+libstdc++ \
  --runtime musl+libc++ \
  --linux-version 6.12.32 \
  --musl-version 1.2.5 \
  --libstdcxx-version 14.2.0
```

如果你已经有现成的 sysroot 或 GCC toolchain，也仍然可以显式传入 `--sysroot PROFILE=PATH` 和 `--gcc-toolchain PROFILE=PATH` 覆盖受管路径。

生成 `llvm-libc + libc++`：

```bash
./bin/make-llvm-toolchain build \
  --source-dir /path/to/llvm-project \
  --project clang \
  --runtime llvm-libc+libc++ \
  --sysroot llvm-libc+libc++=/opt/sysroots/llvm-libc \
  --linux-version 6.12.32
```

这条 profile 会生成一条等价于下面思路的规划：

```bash
cmake -G Ninja -S /path/to/llvm-project/llvm -B build/runtimes-llvm-libc-libcxx \
  -DLLVM_ENABLE_PROJECTS=clang \
  -DLLVM_RUNTIME_TARGETS=x86_64-unknown-linux-llvm \
  -DLLVM_BUILTIN_TARGETS=x86_64-unknown-linux-llvm \
  -DRUNTIMES_x86_64-unknown-linux-llvm_LLVM_ENABLE_RUNTIMES='libc;compiler-rt;libunwind;libcxxabi;libcxx' \
  -DRUNTIMES_x86_64-unknown-linux-llvm_CMAKE_SYSROOT=/path/to/.cache/bootstrap-sysroots/llvm-libc-libcxx \
  -DBUILTINS_x86_64-unknown-linux-llvm_CMAKE_SYSROOT=/path/to/.cache/bootstrap-sysroots/llvm-libc-libcxx \
  -DCLANG_DEFAULT_RTLIB=compiler-rt \
  -DCLANG_DEFAULT_UNWINDLIB=libunwind \
  -DCMAKE_INSTALL_PREFIX=/opt/sysroots/llvm-libc/usr
```

这条 profile 在上面的 libc++ 公共策略之外，还会把 `target` / `sysroot` 收敛到 `RUNTIMES_<triple>_*` 和 `BUILTINS_<triple>_*` 变量上，而不是污染整个 top-level LLVM configure。这样 host 侧的 `clang`/`llvm` 配置检查仍然使用本机头文件，runtime 和 builtins 则切到 bootstrap sysroot，最终安装再写入 final llvm-libc sysroot。

## 常用选项

- `--project clang --project lld`: 只构建部分主机工具。
- `--host-build-target install-clang --host-build-target install-clang-resource-headers`: 收窄 host 构建目标，适合做 runtime focused build。
- `--targets-to-build X86;AArch64`: 控制主机工具链启用的 LLVM 后端。
- `--target-triple musl+libc++=aarch64-unknown-linux-musl`: 覆盖 profile 默认 triple。
- `--cache-root PATH`: 覆盖 `.cache` 根目录。
- `--clean`: 在执行前删除当前 build cache、受管 sysroot、受管 GCC toolchain、以及生成的 profile 输出。
- `--linux-version` / `--glibc-version` / `--libstdcxx-version` / `--musl-version`: 指定受管 sysroot 组件版本，不指定时自动取上游最新发布版。
- `--cmake-define KEY=VALUE`: 追加全局 CMake cache entry。
- `--profile-cmake-define musl+libc++:KEY=VALUE`: 只给某个 runtime profile 追加 CMake cache entry。

`llvm-libc + libc++` profile 额外会固定一组 bootstrap 所需参数和 install targets，例如 `LLVM_ENABLE_PROJECTS=clang`、`LLVM_RUNTIME_TARGETS=<triple>`、`LLVM_BUILTIN_TARGETS=<triple>`、`CLANG_DEFAULT_RTLIB=compiler-rt`、`CLANG_DEFAULT_UNWINDLIB=libunwind`、`RUNTIMES_<triple>_COMPILER_RT_USE_BUILTINS_LIBRARY=ON`、`RUNTIMES_<triple>_LIBUNWIND_USE_COMPILER_RT=ON`，以及 `install-libc`、`install-cxx`、`install-cxxabi`、`install-unwind`、`install-compiler-rt`、`install-builtins`。

如果 profile 选择的是 `libstdc++`，且用户没有显式提供 `--gcc-toolchain`，工具会在 `cache-root` 下自动下载 GCC 源码并构建一套匹配版本的 GCC/libstdc++ toolchain，再把包装脚本指向这套 toolchain。

项目会保留对 orchestration 至关重要的 CMake 变量，例如 `CMAKE_INSTALL_PREFIX`、`LLVM_ENABLE_PROJECTS`、`LLVM_ENABLE_RUNTIMES`，这些变量不能用自定义 define 覆盖。

## 包装脚本说明

每个 profile 至少会生成：

- `bin/clang`
- `bin/clang++`

如果主机工具链里启用了对应项目，还会生成：

- `bin/lld`
- `bin/ld.lld`
- `bin/lldb`

`libc++` profile 的 `clang++` 包装脚本会自动附带 `-isystem <profile>/include/c++/v1`、`-L <profile>/lib`、`-Wl,-rpath,<profile>/lib` 等参数。`libstdc++` profile 则通过 `--sysroot` 和可选的 `--gcc-toolchain` 指向外部 runtime。

对 `llvm-libc + libc++` 而言，`clang++` 包装脚本会把 `libc++` 的头文件和库路径指向 `<sysroot>/usr/include/c++/v1` 与 `<sysroot>/usr/lib`，而不是 profile 目录本身。

## 验证

```bash
python3 -m unittest tests.test_planner
python3 -m unittest tests.test_cli
```