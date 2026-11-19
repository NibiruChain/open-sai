"""A throwaway sai-cli release for tests: dependency-free wheels built in
memory, and the loopback routes that serve them the way GitHub does."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import Sequence

from sai_cli.core import update
from test_update import API_PATH, Routes, listing


def build_wheel(
    version: str, server_body: str = "", requires: Sequence[str] = ()
) -> bytes:
    """Build a sai-cli wheel whose `sai --version` prints VERSION.
    SERVER_BODY is the source of sai_cli.mcp_server.server and REQUIRES its
    dependencies; without them the wheel installs offline."""
    info = f"sai_cli-{version}.dist-info"
    files = {
        "sai_cli/__init__.py": "",
        "sai_cli/sai_cli.py": f'def main():\n    print("sai, version {version}")\n',
        "sai_cli/mcp_server/__init__.py": "",
        "sai_cli/mcp_server/server.py": server_body,
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: sai-cli\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {name}\n" for name in requires)
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{info}/entry_points.txt": "[console_scripts]\nsai = sai_cli.sai_cli:main\n",
    }
    record = []
    for path, text in files.items():
        digest = hashlib.sha256(text.encode()).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        record.append(f"{path},sha256={encoded},{len(text.encode())}")
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = "\n".join(record) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return buffer.getvalue()


def downloads(version: str) -> str:
    """The path GitHub serves the files of release VERSION under."""
    return f"/{update.REPO}/releases/download/sai-cli/v{version}"


def release_routes(
    wheels: dict[str, bytes],
    latest: Sequence[str] | None = None,
    bad_hash: Sequence[str] = (),
) -> Routes:
    """Routes serving WHEELS (version -> wheel bytes) as GitHub releases,
    with the release list naming LATEST (default: every version). Versions
    in BAD_HASH get a SHA256SUMS entry for other bytes."""
    tags = [update.tag_for(version) for version in (latest or wheels)]
    routes: Routes = {API_PATH: listing("sai-rs/v9.9.9", *tags)}
    for version, data in wheels.items():
        name = update.wheel_name(version)
        digest = hashlib.sha256(data).hexdigest()
        if version in bad_hash:
            digest = hashlib.sha256(b"something else").hexdigest()
        sums = f"{'0' * 64}  sai_cli-{version}.tar.gz\n{digest}  {name}\n"
        routes[f"{downloads(version)}/{name}"] = (200, {}, data)
        routes[f"{downloads(version)}/SHA256SUMS"] = (200, {}, sums.encode())
    return routes
