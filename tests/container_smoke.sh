#!/bin/sh
# Run from the compose project directory after `docker compose up -d --build`.
set -eu
SERVICE="${SERVICE:-transcode-studio}"
echo "== health =="
docker compose exec -T "$SERVICE" python3 - <<'PY'
import json, os, urllib.request
with urllib.request.urlopen("http://127.0.0.1:5124/api/health", timeout=5) as response:
    data = json.load(response)
assert data["ok"] and data["status"] == "healthy", data
assert data["source_readonly"] is True, f"/host is not read-only: {data}"
print("health ok; source readonly =", data["source_readonly"])
print("tools =", data["tools"])
PY

echo "== source read-only (write must fail) =="
docker compose exec -T "$SERVICE" python3 - <<'PY'
import os, stat
root = "/host"
assert os.path.isdir(root), "/host is not mounted"
probe = os.path.join(root, ".nts-write-probe")
try:
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as exc:
    print("source write correctly denied:", exc.strerror)
else:
    os.close(fd)
    os.unlink(probe)
    raise SystemExit("FATAL: /host was writable")
# Read a known host file, proving traversal/read works.
with open("/host/etc/os-release", "rb") as fh:
    assert fh.read(32)
print("host root readable and immutable")
PY

echo "== output and data writable =="
docker compose exec -T "$SERVICE" python3 - <<'PY'
import os, sqlite3
for path in ("/output", "/data"):
    probe = os.path.join(path, ".nts-smoke")
    with open(probe, "w") as fh:
        fh.write("ok")
    os.unlink(probe)
assert os.path.isfile("/data/app.db")
db = sqlite3.connect("/data/app.db")
assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
db.close()
print("output/data writable; sqlite integrity ok")
PY

echo "ALL CONTAINER SMOKE CHECKS PASSED"
