# Deployment

From a fresh clone to a running system. Everything here was checked against the code;
where the README disagrees, the README is stale and this document wins.

---

## ⚠️ Fix this before binding to anything but loopback

`GET /api/media/{jid}/{name}` validates `name` but **never validates `jid`**
(`web/app.py:609-616`). A percent-encoded traversal reaches the database:

```
GET /api/media/%2e%2e/jobs.db   →  200 OK, the entire SQLite file
```

Verified live against the running app, not theoretical. The un-encoded form is
normalised away by the router; `%2e%2e` is not. One level up is exactly enough to
reach `web/data/jobs.db`.

**The fix** — reject anything that isn't a bare job id:

```python
@app.get("/api/media/{jid}/{name}")
def media(jid: str, name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    if not re.fullmatch(r"[0-9a-f]{12}", jid):        # ids are uuid4().hex[:12]
        raise HTTPException(400, "bad job id")
    p = os.path.join(JOBS, jid, name)
```

Until that lands, run on `127.0.0.1` only.

There is also **no authentication, rate limiting, CSRF protection or upload size
limit** on any route — including `DELETE /api/jobs/{jid}` — and `/docs`, `/redoc`,
`/openapi.json` are public. `Image.MAX_IMAGE_PIXELS = None` (`web/app.py:53`) removes
PIL's decompression-bomb guard on an unauthenticated upload path. Put a reverse proxy
with auth in front before any public bind.

---

## 1. Topologies

Pick one. They differ only in where the GPU work happens.

**A — Everything local (laptop, no GPU).** Web app + CV + VLM on one box.
Works, but layout inference on CPU is very slow. `health.py` reports missing CUDA as
WARN, not FAIL.

**B — Split (current setup).** Web app on the laptop; DocLayout-YOLO and Ollama on the
T4 box, reached over SSH tunnels. This is what the code's remote mode is for.

**C — Everything on the GPU box.** Simplest to supervise; needs a proxy in front.

---

## 2. Prerequisites

### Python

System `python3` here is **3.9.6**, and all eleven `epaper` modules plus `web.app`
import cleanly under it. 3.9 is fine for the *code*; the virtualenv exists for
`torch` and `doclayout_yolo`, not for syntax.

### Packages

`requirements.txt` declares only `Pillow>=9.0` and `pymongo>=4.0`. **That is not
enough to start anything.** The full set:

```bash
# web app — undeclared, required
pip3 install fastapi uvicorn python-multipart

# declared
pip3 install -r requirements.txt

# layout stage, on the GPU box, inside ~/epaper-venv — undeclared, required
pip3 install torch --index-url https://download.pytorch.org/whl/cu121
pip3 install doclayout-yolo
```

No lockfile, no pins, no checksums. `epaper/vlm.py` uses stdlib `urllib` only — there
is no `requests`/`httpx`/`ollama` dependency.

### System

| Dependency | Needed by | Note |
|---|---|---|
| **Ollama** + `qwen2.5vl:7b` | everything that reads text | `ollama pull qwen2.5vl:7b` (~6 GB) |
| **DocLayout-YOLO checkpoint** | layout stage | `~/models/doclayout_yolo_docstructbench_imgsz1024.pt` — fetched out of band, nothing in the repo downloads it |
| **CUDA GPU** | layout stage | Default device `cuda:0`; CPU works but is very slow |
| **MongoDB** at `localhost:27017` | `fetch_delhi.py`, `build_all_pdfs.py`, `health.py` | **Not** needed by the web app, which is SQLite-only. URI is hardcoded in `epaper/store.py` — a remote Mongo needs a code change |
| **poppler** (`pdftoppm`) | PDF uploads only | `brew install poppler` / `apt-get install poppler-utils` |
| **A TrueType font** | PDF label text | Searched in 4 dirs; falls back to PIL's bitmap default. Labels are Latin-only by design (no raqm shaping) |

> The GPU box has a **broken NVML** — `nvidia-smi` fails with a driver/library
> mismatch, which is why vLLM is blocked. `server/layout_service.py` is stdlib-only on
> purpose so nothing queries NVML at import. Torch's CUDA runtime is unaffected.
> **Ollama on that box is already running and must not be stopped or modified.**

---

## 3. Environment variables

Twelve exist; the README documents four. Full list:

| Variable | Default | Effect |
|---|---|---|
| `EPAPER_LAYOUT_URL` | *unset* | Unset = in-process YOLO. Set = POST to a remote layout service. **The switch between topology A and B** |
| `EPAPER_YOLO` | `~/models/doclayout_yolo_docstructbench_imgsz1024.pt` | Checkpoint path |
| `EPAPER_DEVICE` | `cuda:0` | Torch device for YOLO |
| `EPAPER_DEBUG_DIR` | `out/debug` **(relative)** | Debug artefacts. Pin to an absolute path under a supervisor or they scatter to the process CWD |
| `EPAPER_VLM_BACKEND` | `ollama` | `ollama` \| `vllm`; anything else raises |
| `EPAPER_VLM` | `qwen2.5vl:7b` | Vision model tag |
| `EPAPER_TEXT_MODEL` | falls back to `EPAPER_VLM` | Topic screening model. Leave unset |
| `EPAPER_NUM_CTX` | `16384` | **Do not lower.** Ollama's own default is 4096 and it truncates a full-page image *silently* — the model answers about a page it half saw |
| `EPAPER_OLLAMA` | `http://localhost:11434` | Ollama endpoint |
| `EPAPER_VLLM` | `http://localhost:8000/v1` | Future |
| `EPAPER_MODEL` | `claude-opus-5` | Legacy Claude path only |
| `ANTHROPIC_API_KEY` | *none* | Legacy Claude path only; read lazily, so its absence doesn't break imports |

There is no `.env` or `.env.example` in the repo. A supervisor-launched process
inherits **none** of these and silently falls back to `cuda:0` + localhost Ollama.

---

## 4. Local run (topology A or C)

```bash
cd ~/Desktop/AI_ML/E-Paper-Collection
source ~/epaper-venv/bin/activate            # load-bearing: torch + doclayout_yolo live here

export EPAPER_YOLO=~/models/doclayout_yolo_docstructbench_imgsz1024.pt
export EPAPER_VLM_BACKEND=ollama
export EPAPER_VLM=qwen2.5vl:7b
export EPAPER_NUM_CTX=16384

python3 scripts/health.py --probe             # check every prerequisite FIRST
python3 web/app.py --port 8080                # → http://127.0.0.1:8080
```

`health.py` exits 0 only when the local backend is ready. It checks weights, the
`doclayout_yolo` import, CUDA, torch, VLM reachability, context window, Mongo counts,
and how many Mongo page paths still exist on disk.

> **Launch it as `python3 web/app.py`, not `uvicorn web.app:app`.** `init_db()` runs
> only inside `main()` (`web/app.py:629`); any external ASGI runner leaves the tables
> uncreated and every endpoint 500s. Fixing that properly means adding a startup hook.

---

## 5. Split run (topology B — laptop + GPU box)

### On the GPU box

```bash
~/epaper-venv/bin/python server/layout_service.py --port 8001 --preload
```

Serves `POST /layout` (`{image_b64, conf}` → `{elements, size, ms}`) and `GET /health`.

`--host` defaults to `127.0.0.1`, which contradicts the split. Either pass
`--host 0.0.0.0` plus a firewall rule, or tunnel — tunnelling is safer.

### On the laptop

```bash
ssh -N -L 8001:localhost:8001 -L 11500:localhost:11434 gpubox

export EPAPER_LAYOUT_URL=http://localhost:8001
export EPAPER_OLLAMA=http://localhost:11500
```

**Both tunnels are required.** This is the step that gets missed: even in remote-layout
mode, every VLM call still goes to `EPAPER_OLLAMA`, which defaults to *your own*
localhost. Port 11500 rather than 11434 because the Mac runs its own Ollama on 11434
and the ports collide.

### Health reporting is not trustworthy here

`server/layout_service.py:117` sets `ok = weights_present` **alone** — it returns
`ok: true` on a box with no CUDA, no torch and no model loaded. Worse, remote mode
copies that into the web app's own `ready` (`local_backend.py:596-607`), so
`GET /api/health` can report `ready: true` against a dead GPU box.

**Gate on `cuda` and `model_loaded`, not on `ok` or `ready`:**

```bash
curl -s localhost:8001/health | python3 -m json.tool
```

---

## 6. Process supervision

The repo has **no** Makefile, shell script, systemd unit, launchd plist, Dockerfile or
compose file. Both servers are foreground commands that die with the SSH session — the
de facto practice is a shell redirect into `web/server.log`. Write real units:

**`/etc/systemd/system/epaper-web.service`**

```ini
[Unit]
Description=E-Paper Clipping Desk
After=network.target mongod.service

[Service]
Type=simple
User=epaper
WorkingDirectory=/opt/e-paper
Environment=EPAPER_VLM_BACKEND=ollama
Environment=EPAPER_VLM=qwen2.5vl:7b
Environment=EPAPER_NUM_CTX=16384
Environment=EPAPER_DEBUG_DIR=/opt/e-paper/out/debug
Environment=EPAPER_YOLO=/opt/e-paper/models/doclayout_yolo_docstructbench_imgsz1024.pt
ExecStart=/opt/e-paper/venv/bin/python web/app.py --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/epaper/web.log
StandardError=append:/var/log/epaper/web.log

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/epaper-layout.service`**

```ini
[Unit]
Description=DocLayout-YOLO inference
After=network.target

[Service]
Type=simple
User=epaper
WorkingDirectory=/opt/e-paper
Environment=EPAPER_YOLO=/opt/e-paper/models/doclayout_yolo_docstructbench_imgsz1024.pt
Environment=EPAPER_DEVICE=cuda:0
ExecStart=/opt/e-paper/venv/bin/python server/layout_service.py --port 8001 --preload
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/epaper/layout.log
StandardError=append:/var/log/epaper/layout.log

[Install]
WantedBy=multi-user.target
```

Note `EPAPER_DEBUG_DIR` is absolute in both — its default is the relative string
`out/debug`.

---

## 7. Ports

| Port | Service | Default bind |
|---|---|---|
| 8080 | Web UI | `127.0.0.1` |
| 8001 | Layout service | `127.0.0.1` |
| 11434 | Ollama | loopback |
| 11500 | Ollama tunnel (laptop side) | loopback |
| 27017 | MongoDB | loopback |
| 8000 | vLLM (blocked on NVML) | — |

**Everything is loopback by default.** On the target server, 80 and 443 are held by
nginx/Laravel and 8080 is blocked by the AWS security group — so production needs an
nginx vhost or location block proxying to `127.0.0.1:8080`, not a new public port:

```nginx
location /epaper/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 40m;          # no server-side upload limit exists
    auth_basic "E-Paper";
    auth_basic_user_file /etc/nginx/.htpasswd;
}
```

Outbound access is needed to the publisher hosts (`epaper.tribuneindia.com`,
`epaper.jansatta.com`, `epaper.financialexpress.com`, `epaper.thestatesman.com`,
`epaper.dainiktribuneonline.com`, `epaper.ntnews.com`, `epaper.sakshi.com`) and the
tile CDN `cache.epapr.in`.

> **Security review note:** `epaper/probe.py:97-101` disables TLS verification globally
> (`check_hostname=False`, `CERT_NONE`), and **both platform clients route through that
> `fetch()`**. Every publisher request is unverified.

---

## 8. Storage

| Path | Contents | Growth |
|---|---|---|
| `out/` | Page images, crops, overlays, PDFs, probe JSON | ~560 MB for one Delhi run |
| `web/data/jobs.db` | Job + article rows | Small (28 KB today) |
| `web/data/jobs/<jid>/` | `page.jpg`, `overlay.jpg`, every crop | **Unbounded** |

Both are gitignored. Neither has a quota, rotation or backup. The only cleanup path is
the unauthenticated `DELETE /api/jobs/{jid}`, which also does **not** cancel a running
job — the worker keeps writing and recreates the directory. Write a retention job.

---

## 9. Fresh-clone gotchas

These fail immediately on a clone because `out/` is gitignored:

- `scripts/build_page.py` reads `out/report_assets.json` **and** `out/coverage.json` at
  import. **Nothing in the repo produces `coverage.json`** — it is hand-authored. Its
  schema is inferable only from the reader: a list of
  `{name, lang, host, platform, verdict, pages}`.
- `scripts/build_report.py` and `scripts/build_pdfs.py` hardcode
  `out/regions_tribune_p01.json` and `out/regions_jansatta_p03.json` — hand-checked
  ground-truth layouts, not generated artefacts. No script recreates them.
- `build_report.py`'s docstring says to run it as `... > out/report_assets.json`. That
  is wrong: the script writes the file itself and the redirect truncates it.

Run `fetch_delhi.py` first to populate `out/pages/`, then the report scripts.

---

## 10. Current git state

```
branch   main → origin/main, clean
commits  787a381  Newspaper e-paper article extraction pipeline
remote   https://github.com/Sreenu-Y-Android/e-paper.git
files    28 tracked
```

Commit author is `BCS-Sreenu-Y <sreenivasulu.yakasiri@bluecloudsoftech.com>`, which
differs from the remote account owner. `out/` and `web/data/` are excluded.

---

## 11. Pre-flight checklist

- [ ] **Patch `/api/media` `jid` validation** — blocker
- [ ] Auth in front of the app (nginx basic auth at minimum)
- [ ] `pip3 install fastapi uvicorn python-multipart` — otherwise nothing starts
- [ ] DocLayout-YOLO checkpoint present at `EPAPER_YOLO`
- [ ] `ollama pull qwen2.5vl:7b`, daemon reachable at `EPAPER_OLLAMA`
- [ ] `EPAPER_NUM_CTX=16384` exported in the *service* environment
- [ ] `EPAPER_DEBUG_DIR` set to an absolute path
- [ ] Launched as `python3 web/app.py` (so `init_db()` runs), not `uvicorn web.app:app`
- [ ] Both SSH tunnels up if split (`8001` **and** Ollama)
- [ ] `curl localhost:8001/health` shows `cuda: true` and `model_loaded: true` — not just `ok`
- [ ] `python3 scripts/health.py --probe` exits 0
- [ ] systemd units installed; logs going somewhere durable
- [ ] Retention policy for `web/data/jobs/`
- [ ] poppler installed if PDF upload is wanted
- [ ] MongoDB running if any `scripts/` tool will be used
