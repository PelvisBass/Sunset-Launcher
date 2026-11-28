"""Files a layer needs at run time, taken out of their vendors' own archives.

Only the files are fetched, with HTTP range requests, not the whole archive: NVIDIA's Streamline SDK is 276 MB
for 70 MB of DLLs. Every file has a pinned SHA-256 and lands under a `.part` name until it matches.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import ssl
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "sunset-launcher"


@dataclass(frozen=True)
class Member:
    path: str
    name: str
    # None for license texts, which are kept beside the files and not run.
    sha256: str | None


@dataclass(frozen=True)
class Archive:
    label: str
    url: str
    folder: str
    size_text: str
    members: tuple[Member, ...]

    def required(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members if member.sha256)


# Microsoft's redistributable D3DCompiler from the Windows SDK package on NuGet. Wine's own compiler cannot build
# some of the title's shaders, which shows in Series S mode.
D3DCOMPILER = Archive(
    label="Microsoft's D3DCompiler 47",
    url="https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.cpp/10.0.28000.2957/"
    "microsoft.windows.sdk.cpp.10.0.28000.2957.nupkg",
    folder="d3dcompiler-10.0.28000.2957",
    size_text="about 5 MB",
    members=(
        Member("c/Redist/D3D/x64/d3dcompiler_47.dll", "d3dcompiler_47.dll",
               "d28d341306db9067c779d2cfe5865c3840e3a863efa47a2e0b0d4a1b906f6158"),
    ),
)

# The files VIctory-NVIDIA's get-nvidia-sdks.ps1 takes from NVIDIA's release: Streamline, DLSS, DLSS Frame
# Generation and Reflex.
STREAMLINE = Archive(
    label="NVIDIA Streamline 2.14.16 with DLSS",
    url="https://github.com/NVIDIA-RTX/Streamline/releases/download/v2.14.16/streamline-sdk-v2.14.16.zip",
    folder="streamline-2.14.16",
    size_text="about 70 MB",
    members=(
        Member("bin/x64/sl.interposer.dll", "sl.interposer.dll",
               "b1d7395aebd7b9722841d64dcf7433680020b6a8e421f851c16d5ba0c2182edf"),
        Member("bin/x64/sl.common.dll", "sl.common.dll",
               "38eea3b4209afc455636ea372b5933d65fcc13becec6c84d101dcfc4d8648f75"),
        Member("bin/x64/sl.dlss.dll", "sl.dlss.dll",
               "18871eea87c90345da97d8109faa81ea7d5840e8d99b51b55b0e46cfa177f84e"),
        Member("bin/x64/sl.dlss_g.dll", "sl.dlss_g.dll",
               "3ed9538df724993bea8a050195a6a78b9c8c8a566d5b05ade477aab3c390e90f"),
        Member("bin/x64/sl.reflex.dll", "sl.reflex.dll",
               "443577e869352436ff7ec8f4d9c90418d3e95368436ce219261e0f4f07a38122"),
        Member("bin/x64/sl.pcl.dll", "sl.pcl.dll",
               "5698c38e722318681f215c9fe68e45b9bedd51378acc699d8eee26e961e57770"),
        Member("bin/x64/nvngx_dlss.dll", "nvngx_dlss.dll",
               "d00b5b0ab29c3ca101b70c3a5ecb3a01b7ef33766a3383a28a18623083e6123e"),
        Member("bin/x64/nvngx_dlssg.dll", "nvngx_dlssg.dll",
               "8d59d49b36410dd6b62fef2503a5871d347415bd85d943fa0848563a95dd50c7"),
        Member("license.txt", "license.txt", None),
        Member("bin/x64/nvngx_dlss.license.txt", "nvngx_dlss.license.txt", None),
        Member("bin/x64/reflex.license.txt", "reflex.license.txt", None),
    ),
)

# Where Linux distributions keep their certificate bundle. A bundled OpenSSL looks where its build system kept it.
_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/ssl/cert.pem",
)


def ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if sys.platform.startswith("linux") and not os.environ.get("SSL_CERT_FILE"):
        paths = ssl.get_default_verify_paths()
        if not (paths.cafile and os.path.isfile(paths.cafile)) and not (paths.capath and os.path.isdir(paths.capath)):
            if bundle := next((path for path in _CA_BUNDLES if os.path.isfile(path)), None):
                context.load_verify_locations(cafile=bundle)
    return context


class RemoteFile(io.RawIOBase):
    """A read-only, seekable view of a URL, read with HTTP range requests."""

    def __init__(self, url: str, *, timeout: float, check: Callable[[], None] = lambda: None):
        self.timeout = timeout
        self.check = check
        self.context = ssl_context()
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:
            self.url = response.url
            self.size = int(response.headers["Content-Length"])
        self.position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def readinto(self, buffer) -> int:
        self.check()
        if self.position >= self.size:
            return 0
        last = min(self.size, self.position + len(buffer)) - 1
        request = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.position}-{last}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
            if response.status != 206:
                raise OSError(f"The server sent the whole file instead of part of it (HTTP {response.status}).")
            data = response.read()
        buffer[:len(data)] = data
        self.position += len(data)
        return len(data)


def folder(archive: Archive, root: Path) -> Path:
    return root / archive.folder


def ready(archive: Archive, root: Path) -> Path | None:
    """The archive's folder under `root` when every required file is there."""
    target = folder(archive, root)
    return target if all((target / member.name).is_file() for member in archive.required()) else None


def fetch(archive: Archive, root: Path, output: Callable[[str], None], *, timeout: float = 60,
          check: Callable[[], None] = lambda: None) -> Path:
    """Fetch the archive's missing files into its folder under `root` and return the folder."""
    target = folder(archive, root)
    if ready(archive, root) is not None:
        return target
    missing = [member for member in archive.members if not (target / member.name).is_file()]
    output(f"Downloading {archive.label} ({archive.size_text}) from {archive.url.split('/')[2]}…\n")
    target.mkdir(parents=True, exist_ok=True)
    try:
        zip_file = zipfile.ZipFile(io.BufferedReader(RemoteFile(archive.url, timeout=timeout, check=check), 8 << 20))
    except zipfile.BadZipFile as error:
        # zipfile reports a failed read as a bad archive. The read's own error says more.
        raise OSError(str(error.__context__ or error)) from error
    for member in missing:
        try:
            info = zip_file.getinfo(member.path)
        except KeyError:
            if member.sha256 is None:
                continue
            raise OSError(f"{member.path} is not in {archive.url}.") from None
        part = target / f"{member.name}.part"
        digest = hashlib.sha256()
        with zip_file.open(info) as source, part.open("wb") as destination:
            while block := source.read(1 << 20):
                check()
                digest.update(block)
                destination.write(block)
        if member.sha256 and digest.hexdigest() != member.sha256:
            part.unlink(missing_ok=True)
            raise OSError(f"{member.name} did not match its expected SHA-256. Nothing was kept.")
        part.replace(target / member.name)
        output(f"  {member.name}  {info.file_size / 2**20:.1f} MB\n")
    return target


def place(archive: Archive, root: Path, name: str, beside: Path) -> bool:
    """Copy a fetched file into `beside` unless a file of that name is already there. Returns whether one is."""
    destination = beside / name
    if destination.exists():
        return True
    source = folder(archive, root) / name
    if not source.is_file():
        return False
    shutil.copy2(source, destination)
    return True
