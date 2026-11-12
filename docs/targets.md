# Targets

A target is a Python version and a platform, such as Python 3.13 on `musl-aarch64`. wheelgap checks every Python you
pass with `-p` on every platform you pass with `-t`.

```bash
wheelgap -p 3.13,3.14t -t linux-x86_64,linux-aarch64,musl-aarch64
```

Run `wheelgap --list-targets` to see the names, aliases and compatibility limits, or add `--json` for a
machine-readable list.

## Platforms

| name | aliases | wheel tags it accepts |
| --- | --- | --- |
| `linux-x86_64` | `linux`, `linux-amd64`, `linux/amd64` | `manylinux_2_*_x86_64` up to `--glibc` (default 2.36), legacy `manylinux2014`/`2010`/`1` |
| `linux-aarch64` | `linux-arm64`, `linux/arm64`, `linux/arm64/v8` | `manylinux_2_*_aarch64` from 2.17 up to `--glibc`, legacy `manylinux2014` |
| `musl-x86_64` | `alpine`, `musl` | `musllinux_1_*_x86_64` up to `--musl` (default 1.2) |
| `musl-aarch64` | `alpine-arm64` | `musllinux_1_*_aarch64` |
| `macos-arm64` | `macos`, `mac` | `macosx_*_arm64` and `universal2` up to `--macos` (default 14.0) |
| `macos-x86_64` | `macos-intel` | `macosx_*_x86_64` and `universal2` |
| `win-amd64` | `windows`, `win` | `win_amd64` |
| `win-arm64` | `windows-arm64` | `win_arm64` |
| `linux-armv7l` | `linux-arm`, `linux-armv7`, `linux/arm/v7` | `manylinux_2_*_armv7l` from 2.17 (32-bit ARM, e.g. Raspberry Pi OS), legacy `manylinux2014`, and plain `linux_armv7l` (which PyPI allows for the Pi) |
| `linux-ppc64le`, `linux-s390x`, `linux-riscv64` | `linux/ppc64le`, `linux/s390x`, `linux/riscv64` | `manylinux_2_*` from 2.17 for that arch (`riscv64` has no legacy `manylinux2014` tag) |
| `linux-i686` | `linux-x86`, `linux-386`, `linux/386` | `manylinux_2_*_i686` up to `--glibc`, legacy `manylinux2014`/`2010`/`1` (32-bit x86) |
| `win32` | `windows-x86`, `win-x86` | `win32` (32-bit Python on Windows) |
| `musl-armv7l`, `musl-ppc64le`, `musl-s390x`, `musl-riscv64`, `musl-i686` | `alpine-armv7`, `alpine-x86` | `musllinux_1_*` for that arch (pip would also take a plain `linux_armv7l` wheel on Alpine, but it's a glibc build, so wheelgap doesn't count it) |
| `pyodide` | `wasm`, `wasm32`, `emscripten` | Pyodide's ABI for that Python: `pyodide_2024_0` (3.12), `pyodide_2025_0` / `pyemscripten_2025_0` (3.13), `pyemscripten_2026_0` (3.14) |
| `ios-arm64` | `ios`, `iphoneos` | `ios_*_arm64_iphoneos` up to `--ios` (default 13.0) |
| `ios-simulator-arm64`, `ios-simulator-x86_64` | `ios-simulator` | `ios_*_arm64_iphonesimulator` / `ios_*_x86_64_iphonesimulator` |
| `android-arm64`, `android-x86_64` | `android` | `android_*_arm64_v8a` / `android_*_x86_64` up to `--android-api` (default 24) |

`--list-targets` shows a few more aliases than this table, such as `manylinux-x86_64` and `musllinux-aarch64`.

Docker platform names also work with `-t`, for example `-t linux/amd64,linux/arm64` (also `linux/arm64/v8`,
`linux/arm/v7`, `linux/ppc64le`, `linux/s390x`, `linux/riscv64` and `linux/386`, also spelled `linux/i386`).

### Groups

- `-t all` checks the eight common native platforms: `linux-x86_64`, `linux-aarch64`, `musl-x86_64`, `musl-aarch64`,
  `macos-arm64`, `macos-x86_64`, `win-amd64` and `win-arm64`.
- `-t everything` adds the armv7l, ppc64le, s390x, riscv64 and i686 Linux targets (glibc and musl) and `win32`.
- `-t mobile` is the iOS and Android targets.

Pyodide, iOS and Android are only checked when you name them.

## Floors: glibc, musl, macOS, iOS and Android

A wheel built for a newer system than your target's can't be used there, so each family of platforms has a version:

| flag | config key | default | applies to |
| --- | --- | --- | --- |
| `--glibc X.Y` | `glibc` | 2.36 | `linux-*` targets |
| `--musl X.Y` | `musl` | 1.2 | `musl-*` targets |
| `--macos X.Y` | `macos` | 14.0 | `macos-*` targets |
| `--ios X.Y` | `ios` | 13.0 | `ios-*` targets |
| `--android-api N` | `android-api` | 24 | `android-*` targets |

Only x86 Linux takes `manylinux` wheels older than glibc 2.17: every other architecture's first manylinux was
`manylinux2014` (glibc 2.17), so pip accepts nothing older there, and neither does wheelgap.

### Distros

If you know the deployment distro rather than its libc version, use `--distro`:

```bash
wheelgap uv.lock -p 3.13 -t linux-aarch64 --distro ubuntu-22.04
wheelgap uv.lock -p 3.13 -t musl-aarch64 --distro alpine
```

It supplies a glibc or musl version only: it does **not** select Python versions or platforms. Keep using `-t` for
those platforms, including `musl-*` for Alpine. You can also set `distro = "ubuntu-22.04"` in `[tool.wheelgap]`.

| distro names | libc version | package sources |
| --- | --- | --- |
| `ubuntu-20.04`, `ubuntu-22.04`, `ubuntu-24.04` | glibc 2.31, 2.35, 2.39 | [Focal](https://launchpad.net/ubuntu/focal/+source/glibc), [Jammy](https://packages.ubuntu.com/jammy/libc6), [Noble](https://packages.ubuntu.com/noble/libc6) |
| `debian-11` / `bullseye`, `debian-12` / `bookworm`, `debian-13` / `trixie` | glibc 2.31, 2.36, 2.41 | [Bullseye](https://packages.debian.org/bullseye/libc6), [Bookworm](https://packages.debian.org/bookworm/libc6), [Trixie](https://packages.debian.org/trixie/libc6) |
| `rhel-8` / `rocky-8` / `alma-8`, `rhel-9` / `rocky-9` / `alma-9` | glibc 2.28, 2.34 | [RHEL 8](https://access.redhat.com/errata/RHSA-2021:1585), [RHEL 9](https://access.redhat.com/errata/RHBA-2022:3917) |
| `amazonlinux-2`, `amazonlinux-2023` | glibc 2.26, 2.34 | [Amazon Linux toolchain packages](https://docs.aws.amazon.com/linux/al2023/ug/glibc-gcc-and-binutils.html) |
| `alpine` | musl 1.2 (Alpine 3.13 and later) | [Alpine musl](https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/musl) |

For each libc, an explicit `--glibc` / `--musl` wins, then `--distro`, then a version from `--dockerfile`, then a
`glibc` / `musl` config value, then the config `distro`, then the built-in default. A distro only supplies its own
libc; the other libc keeps its existing default. Names are case-insensitive; unknown names are errors. For older
Alpine, pass `--musl 1.1` explicitly. The text `--list-targets` output also lists distro names; its JSON output
remains a list of platforms.

## Python versions

`-p` takes versions such as `3.13`, repeated or comma-separated. A trailing `t` means a free-threaded build
(`3.14t`), which correctly rejects `abi3` wheels.

Without `-p`, the Python comes from `--dockerfile`, then `python` in `[tool.wheelgap]`, then a `.python-version` file
next to the lockfile (or `pyproject.toml`), and otherwise the Python wheelgap is running on.

### Free-threading and new Python readiness

!!! note "After 0.9.0"
    `-p next`, `-p next-t` and the "blocking" summary line are not in 0.9.0.

A new CPython comes out each October, and for months many packages have no wheels for it yet. To see what would
build from source if you upgraded, check the Python you use now next to the new one. `-p next` is the newest CPython
this wheelgap release knows (3.15), and `-p next-t` is its free-threaded build, so you don't have to look the version
up; a note on stderr says which version it is (`-q` drops it). Upgrade wheelgap to move `next` on. `next` works
anywhere a Python version does: `-p`, `python = ["3.14", "next"]` in `[tool.wheelgap]`, `wheelgap diff -p`,
`make_targets(python="next")` and the MCP tools' `python` argument, but not in `.python-version`, which uv and pyenv
read too.

```bash
wheelgap uv.lock -p 3.14 -p next -t linux-x86_64,linux-aarch64
wheelgap uv.lock -p 3.14 -p next-t -t all   # the upgrade and free-threading together
```

When exactly two Python versions are checked and one is newer, the summary names the packages that block the
upgrade: those with a gap (`● source` or `✗ missing`) on the newer Python on a platform where the older one has a
wheel (or doesn't install the package). Gaps both Pythons share, and targets ignored with `--ignore NAME:TARGET`,
don't count. Packages that couldn't be checked aren't listed, only counted
(`blocking 3.15: none known (1 package couldn't be checked)`), so an empty list isn't a pass on its own: the summary
and the exit code still decide.

```text
3 of 41 packages have gaps: 3 will build from source.
blocking 3.15: numpy, pydantic-core
```

`--markdown` and `-q` print the same line, and `--json` has the list as `blocking_newer_python`. Two builds of the
same version, such as `-p 3.14 -p 3.14t`, have no newer one, so they get no such line; the
[free-threading recipe](recipes.md#free-threading-readiness) lists a free-threaded build's gaps for scripts.

## How targets are chosen

Target platforms come from the first of these that gives any:

1. `-t` on the command line;
2. the final stage of the Dockerfile given with `--dockerfile`;
3. `targets` in `[tool.wheelgap]`;
4. uv's `[tool.uv] required-environments` in the project's `pyproject.toml`;
5. the built-in defaults: `linux-x86_64`, `linux-aarch64`, `macos-arm64` and `win-amd64`.

### uv's required-environments

For uv projects, wheelgap reads `[tool.uv] required-environments` as default targets:

```toml
[tool.uv]
required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]
```

Each marker is evaluated against the eight common native platforms using the selected Python versions. A platform is
included if any marker matches for any selected Python, and the resulting platforms are checked for all selected
Pythons. Linux markers select both glibc and musl targets, since markers cannot distinguish the libc; the example
above selects `linux-aarch64` and `musl-aarch64`. Use `-t` to choose one explicitly. An inferred selection is
explained on stderr, including with `--json` or `--markdown`.

The inferred targets replace the built-in defaults, so a project that only requires Intel macOS wheels is then
checked for Intel macOS only: add `[tool.wheelgap] targets` or `-t` to check more. An absent or empty setting keeps
the built-in defaults, and so does one matching none of the targets above (with a note). An invalid marker is an
error. Only `required-environments` is used here: `environments`, extra Linux architectures, mobile and Pyodide
targets are not inferred.

### From a Dockerfile

`--dockerfile PATH` takes the Python, the platform and the glibc or musl version from the image of the Dockerfile's
final stage, such as `python:3.13-alpine` or `ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim`, following named
stages like `FROM base AS runtime` back to the image they start from. `-p`, `-t`, `--glibc` and `--musl` on the
command line still win, and the image wins over `[tool.wheelgap]`.

A python X.Y tag without a Debian release (`python:3.13-slim`) is read as the release Docker Hub currently builds it
on (trixie, glibc 2.41, for 3.9 and later); patch tags like `3.12.7-slim` keep the `--glibc` default. Pin `-bookworm`
or `-trixie` for an answer that doesn't move. A Dockerfile wheelgap can't read the way Docker does is an error, not a
guess.

## Advisory targets

!!! note "After 0.9.0"
    `--advisory` and `advisory = [...]` are not in 0.9.0.

To watch a platform before you support it, such as Windows on ARM, make it advisory:
`-t linux-x86_64,win-arm64 --advisory win-arm64` (or `advisory = ["win-arm64"]` in `[tool.wheelgap]`). Its gaps are
reported as usual but never fail the run. [`--advisory`](cli.md#advisory) has the details.

## Pyodide

Pyodide (Python in the browser) only installs wheels: there's no compiler, so a package with only an sdist shows as
`✗ missing`, not `● source`. Pyodide wheels are tied to one ABI per Python version and don't work across them.

Pyodide also ships its own builds of many popular packages (numpy, pydantic-core and others) that aren't on PyPI.
Point `--pyodide-lock` at the release's `pyodide-lock.json` (a path or `https://` URL, also `pyodide-lock = "..."` in
`[tool.wheelgap]`) and those count as `✓ bundled`, with a note when Pyodide's version differs from the one you pinned
(shown with `-q` too, and `"bundled_version_differs": true` in `--json`). That still passes: `pyodide.loadPackage`
gives you Pyodide's version, though `micropip.install` of your exact pin would fail.

```bash
wheelgap -p 3.12 -t pyodide --pyodide-lock https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide-lock.json
```

## iOS and Android

iOS and Android (PEP 730 and PEP 738, Python 3.13+) are checked with `-t mobile` or by name, for apps built with tools
like BeeWare's Briefcase. Mobile dependencies are installed by a cross-platform pip run that can only use wheels, so a
package with only an sdist shows as `✗ missing`. `--ios` and `--android-api` (or `ios = "15.0"` and
`android-api = 28` in `[tool.wheelgap]`) set your app's minimum iOS version and Android API level.

wheelgap checks PyPI only: Briefcase and Chaquopy also install from their own package repositories (BeeWare's on
anaconda.org, and chaquo.com), which have mobile builds of packages like numpy, so a `✗ missing` there may still
install.

## Environment markers on unusual platforms

On `win32`, `linux-i686`, `musl-i686`, `linux-armv7l` and `musl-armv7l` a `platform_machine` comparison counts as
unknown, so the package is still checked. `platform_machine` reports the machine, not the Python: 32-bit Python on
64-bit Windows says `AMD64`, a `linux/386` container on an x86_64 host says `x86_64`, and 32-bit Raspberry Pi OS on a
Pi 4 or 5 (which boots a 64-bit kernel) or a `linux/arm/v7` container on an arm64 host can say `aarch64`.
