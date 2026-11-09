"""Install targets: a Python version on a platform, expanded to the wheel tags it accepts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from packaging import tags
from packaging.tags import Tag

# Short platform name -> (family, arch)
PLATFORMS: dict[str, tuple[str, str]] = {
    "linux-x86_64": ("manylinux", "x86_64"),
    "linux-aarch64": ("manylinux", "aarch64"),
    "musl-x86_64": ("musllinux", "x86_64"),
    "musl-aarch64": ("musllinux", "aarch64"),
    "macos-arm64": ("macos", "arm64"),
    "macos-x86_64": ("macos", "x86_64"),
    "win-amd64": ("windows", "win_amd64"),
    "win-arm64": ("windows", "win_arm64"),
    "pyodide": ("pyodide", "wasm32"),
}

# `-t all` means the native platforms; Pyodide only when asked for by name.
NATIVE_PLATFORMS = [name for name, (family, _) in PLATFORMS.items() if family != "pyodide"]

ALIASES: dict[str, str] = {
    "linux": "linux-x86_64",
    "linux-amd64": "linux-x86_64",
    "linux-arm64": "linux-aarch64",
    "linux-arm64-v8": "linux-aarch64",
    "manylinux-x86_64": "linux-x86_64",
    "manylinux-aarch64": "linux-aarch64",
    "alpine": "musl-x86_64",
    "musl": "musl-x86_64",
    "musl-arm64": "musl-aarch64",
    "musllinux-x86_64": "musl-x86_64",
    "musllinux-aarch64": "musl-aarch64",
    "alpine-arm64": "musl-aarch64",
    "alpine-aarch64": "musl-aarch64",
    "mac": "macos-arm64",
    "macos": "macos-arm64",
    "macos-aarch64": "macos-arm64",
    "macos-intel": "macos-x86_64",
    "windows": "win-amd64",
    "win": "win-amd64",
    "windows-amd64": "win-amd64",
    "windows-x86_64": "win-amd64",
    "win-x86_64": "win-amd64",
    "windows-arm64": "win-arm64",
    "pyodide-wasm32": "pyodide",
    "emscripten": "pyodide",
    "wasm": "pyodide",
    "wasm32": "pyodide",
}

# family -> (sys_platform, platform_system, os_name); arch -> platform_machine
_MARKER_OS = {
    "manylinux": ("linux", "Linux", "posix"),
    "musllinux": ("linux", "Linux", "posix"),
    "macos": ("darwin", "Darwin", "posix"),
    "windows": ("win32", "Windows", "nt"),
    "pyodide": ("emscripten", "Emscripten", "posix"),
}
_MARKER_MACHINE = {"win_amd64": "AMD64", "win_arm64": "ARM64"}

DEFAULT_PLATFORMS = ["linux-x86_64", "linux-aarch64", "macos-arm64", "win-amd64"]
DEFAULT_GLIBC = "2.36"
DEFAULT_MUSL = "1.2"
DEFAULT_MACOS = "14.0"

# Legacy manylinux aliases (PEP 513/571/599) and the glibc minor they map to.
_LEGACY_MANYLINUX = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}
_LEGACY_ARCHES = {
    "manylinux1": {"x86_64", "i686"},
    "manylinux2010": {"x86_64", "i686"},
    "manylinux2014": {"x86_64", "i686", "aarch64", "armv7l", "ppc64", "ppc64le", "s390x"},
}

# Pyodide wheels are tied to one ABI per Python version and don't work across them
# (https://pyodide-build.readthedocs.io/en/latest/reference/platform.html). PEP 783 renamed
# the tag to pyemscripten_*; both spellings of the same ABI are accepted.
PYODIDE_PLATFORMS: dict[tuple[int, int], list[str]] = {
    (3, 11): ["emscripten_3_1_32_wasm32"],
    (3, 12): ["pyodide_2024_0_wasm32", "pyemscripten_2024_0_wasm32"],
    (3, 13): ["pyodide_2025_0_wasm32", "pyemscripten_2025_0_wasm32"],
    (3, 14): ["pyemscripten_2026_0_wasm32"],
}

_PY_RE = re.compile(r"^(?:cp|py)?(\d)\.?(\d{1,2})(t?)$")


class TargetError(ValueError):
    pass


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace("/", "-")


_LOOKUP = {_norm(k): k for k in PLATFORMS} | {_norm(k): v for k, v in ALIASES.items()}


def resolve_platform(name: str) -> str:
    try:
        return _LOOKUP[_norm(name)]
    except KeyError:
        known = ", ".join(PLATFORMS)
        raise TargetError(f"unknown target {name!r} (known: {known})") from None


def parse_python(spec: str) -> tuple[int, int, bool]:
    """'3.14' -> (3, 14, False); '3.14t' / 'cp314t' -> (3, 14, True)."""
    m = _PY_RE.match(spec.strip().lower())
    if not m:
        raise TargetError(f"can't parse Python version {spec!r} (try 3.13 or 3.14t)")
    return int(m.group(1)), int(m.group(2)), bool(m.group(3))


def parse_version_pair(spec: str, what: str) -> tuple[int, int]:
    try:
        parts = spec.strip().split(".")
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0  # "15" means 15.0
    except ValueError:
        raise TargetError(f"can't parse {what} version {spec!r} (expected e.g. 2.36)") from None
    return major, minor


def manylinux_platforms(arch: str, glibc: tuple[int, int]) -> list[str]:
    major, minor = glibc
    out = []
    for m in range(minor, 4, -1):
        out.append(f"manylinux_{major}_{m}_{arch}")
        legacy = _LEGACY_MANYLINUX.get(m)
        if major == 2 and legacy and arch in _LEGACY_ARCHES[legacy]:
            out.append(f"{legacy}_{arch}")
    return out


def musllinux_platforms(arch: str, musl: tuple[int, int]) -> list[str]:
    major, minor = musl
    return [f"musllinux_{major}_{m}_{arch}" for m in range(minor, -1, -1)]


@dataclass(frozen=True)
class Target:
    major: int
    minor: int
    freethreaded: bool
    platform: str
    glibc: tuple[int, int] = (2, 36)
    musl: tuple[int, int] = (1, 2)
    macos: tuple[int, int] = (14, 0)

    @property
    def python(self) -> str:
        return f"{self.major}.{self.minor}{'t' if self.freethreaded else ''}"

    def label(self, with_python: bool) -> str:
        return f"{self.python} {self.platform}" if with_python else self.platform

    @property
    def builds_sdists(self) -> bool:
        """Whether an installer can fall back to building an sdist. Pyodide has no compiler."""
        return PLATFORMS[self.platform][0] != "pyodide"

    def platform_tags(self) -> list[str]:
        family, arch = PLATFORMS[self.platform]
        if family == "pyodide":
            return list(PYODIDE_PLATFORMS[(self.major, self.minor)])
        if family == "manylinux":
            return manylinux_platforms(arch, self.glibc)
        if family == "musllinux":
            return musllinux_platforms(arch, self.musl)
        if family == "macos":
            return list(tags.mac_platforms(self.macos, arch))
        return [arch]

    @cached_property
    def marker_env(self) -> dict[str, str]:
        """PEP 508 environment for evaluating markers like `sys_platform == 'win32'`."""
        family, arch = PLATFORMS[self.platform]
        sys_platform, system, os_name = _MARKER_OS[family]
        version = f"{self.major}.{self.minor}"
        return {
            "implementation_name": "cpython",
            "implementation_version": f"{version}.0",
            "os_name": os_name,
            "platform_machine": _MARKER_MACHINE.get(arch, arch),
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": system,
            "platform_version": "",
            "python_full_version": f"{version}.0",
            "python_version": version,
            "sys_platform": sys_platform,
            "extra": "",
        }

    @cached_property
    def tags(self) -> frozenset[Tag]:
        version = (self.major, self.minor)
        interp = f"cp{self.major}{self.minor}"
        # CPython 3.7 and older built "m" (pymalloc) ABIs: cp37-cp37m.
        abi = interp + ("t" if self.freethreaded else "m" if (self.major, self.minor) < (3, 8) else "")
        plats = self.platform_tags()
        supported = set(tags.cpython_tags(version, [abi], plats))
        supported.update(tags.compatible_tags(version, interp, plats))
        return frozenset(supported)

    def accepts(self, wheel_tags: frozenset[Tag]) -> bool:
        return not self.tags.isdisjoint(wheel_tags)


def build_targets(
    pythons: list[str],
    platforms: list[str],
    glibc: str = DEFAULT_GLIBC,
    musl: str = DEFAULT_MUSL,
    macos: str = DEFAULT_MACOS,
) -> list[Target]:
    plats: list[str] = []
    for item in platforms:
        for name in item.split(","):
            if not name.strip():
                continue
            if name.strip().lower() == "all":
                plats.extend(NATIVE_PLATFORMS)
            else:
                plats.append(resolve_platform(name))
    plats = list(dict.fromkeys(plats)) or list(DEFAULT_PLATFORMS)

    pys = list(dict.fromkeys(parse_python(p) for item in pythons for p in item.split(",") if p.strip()))
    opts = dict(
        glibc=parse_version_pair(glibc, "glibc"),
        musl=parse_version_pair(musl, "musl"),
        macos=parse_version_pair(macos, "macOS"),
    )
    if "pyodide" in plats:
        for ma, mi, ft in pys:
            if ft or (ma, mi) not in PYODIDE_PLATFORMS:
                known = ", ".join(f"{a}.{b}" for a, b in PYODIDE_PLATFORMS)
                raise TargetError(f"no Pyodide build for Python {ma}.{mi}{'t' * ft} (known: {known})")
    return [Target(ma, mi, ft, plat, **opts) for (ma, mi, ft) in pys for plat in plats]
