import os
import shutil
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from scripts.merge_frontend_assets import (
    file_manifest,
    merge_legacy_assets,
    referenced_asset_manifest,
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return


def test_old_candidate_mtimes_never_break_active_or_hot_manifest_closure(tmp_path):
    image_dist = tmp_path / "image-dist"
    assets = image_dist / "assets"
    assets.mkdir(parents=True)
    names = (
        "index-current.js",
        "vendor-icons-current.js",
        "vendor-radix-current.js",
        "index-current.css",
    )
    (image_dist / "index.html").write_text(
        """
        <script type="module" src="/assets/index-current.js"></script>
        <link rel="modulepreload" href="/assets/vendor-icons-current.js">
        <link rel="modulepreload" href="/assets/vendor-radix-current.js">
        <link rel="stylesheet" href="/assets/index-current.css">
        """,
        encoding="utf-8",
    )
    for number, name in enumerate(names):
        (assets / name).write_bytes(f"current-{number}".encode())

    # Reproduce the incident condition: immutable image files are older than
    # the former 60-minute cleanup threshold before deployment even begins.
    old_timestamp = time.time() - 2 * 60 * 60
    for path in image_dist.rglob("*"):
        if path.is_file():
            os.utime(path, (old_timestamp, old_timestamp))

    hot_dist = tmp_path / "hot-dist"
    shutil.copytree(image_dist, hot_dist)
    active_dist = tmp_path / "active-dist"
    shutil.copytree(image_dist, active_dist)
    candidate_before = file_manifest(active_dist)

    legacy = tmp_path / "legacy-assets"
    legacy.mkdir()
    (legacy / "index-current.js").write_bytes(b"must-not-overwrite-current")
    (legacy / "lazy-previous.js").write_bytes(b"previous-generation")

    result = merge_legacy_assets(active_dist, legacy)
    active_after = file_manifest(active_dist)
    assert result["merged_legacy_files"] == 1
    assert all(active_after[name] == digest for name, digest in candidate_before.items())
    assert (active_dist / "assets" / "lazy-previous.js").is_file()

    # The active slot may retain an extra previous-generation lazy chunk, but
    # the immutable current index closure must be byte-identical to the
    # force-recreated hot slot.
    assert referenced_asset_manifest(active_dist) == referenced_asset_manifest(hot_dist)

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_QuietHandler, directory=str(active_dist)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for relative_path in referenced_asset_manifest(active_dist):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/{relative_path}",
                timeout=5,
            ) as response:
                assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
