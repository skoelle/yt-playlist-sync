# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Stand-in for yt-dlp used by the tests. Behaviour is driven by a JSON file (FAKE_YTDLP_DATA)."""
import json
import os
import re
import sys
import time
from pathlib import Path


def log_err(text):
    print(text, file=sys.stderr, flush=True)


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("2099.01.01")
        return 0
    data = json.loads(Path(os.environ["FAKE_YTDLP_DATA"]).read_text())
    url = args[-1]

    if "--flat-playlist" in args:
        if "/playlists" in url:
            entries = [{"id": p["id"], "title": p["title"], "_type": "url"} for p in data["channel"]]
            print(json.dumps({"_type": "playlist", "entries": entries}))
        else:
            pid = re.search(r"list=([^&]+)", url).group(1)
            vids = data["playlists"][pid]
            print(json.dumps({"_type": "playlist", "id": pid,
                              "entries": [{"id": v["id"], "title": v["title"]} for v in vids]}))
        return 0

    archive = Path(args[args.index("--download-archive") + 1])
    out_dir = Path(args[args.index("-o") + 1]).parent
    simulate = "--simulate" in args
    pid = re.search(r"list=([^&]+)", url).group(1)
    vids = data["playlists"][pid]
    archived = set()
    if archive.exists():
        archived = {line.split()[-1] for line in archive.read_text().splitlines() if line.strip()}
    errors = 0
    for i, v in enumerate(vids, 1):
        vid = v["id"]
        if vid in archived:
            print(f"[download] {v['title']}: has already been recorded in the archive", flush=True)
            continue
        print(f"[download] Downloading item {i} of {len(vids)}", flush=True)
        print(f"[youtube] {vid}: Downloading webpage", flush=True)
        mode = data.get("fail", {}).get(vid)
        if mode == "unavailable":
            log_err(f"ERROR: [youtube] {vid}: Video unavailable")
            errors += 1
            continue
        if mode == "temporary":
            log_err(
                f"ERROR: [youtube] {vid}: Unable to download webpage: "
                "HTTP Error 503: Service Unavailable"
            )
            errors += 1
            continue
        if mode == "ratelimit":
            log_err(f"ERROR: [youtube] {vid}: HTTP Error 429: Too Many Requests")
            time.sleep(60)
            return 1
        name = f"{i:02d} - {v['title']} [{vid}].mp4"
        print(f"[download] Destination: {out_dir / name}", flush=True)
        for pct in ("10.0%", "55.5%", "100.0%"):
            print(f"YTPS|{pct}|1.50MiB/s|00:01", flush=True)
            time.sleep(float(data.get("slow", 0)))
        if not simulate:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_bytes(b"x" * 1000)
            with archive.open("a") as fh:
                fh.write(f"youtube {vid}\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
