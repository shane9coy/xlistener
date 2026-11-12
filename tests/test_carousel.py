#!/usr/bin/env python3
"""Tests for carousel (multi-image) publishing. No network, no Threads posts, no X API.

The reposter script gets imported directly; every network-touching function is patched.
Run: venv/bin/python test_carousel.py
"""
import importlib.util
import os
import sys
from pathlib import Path

import tempfile

# Tests live in tests/ in the repo but at the service root on a deployed install.
_HERE = Path(__file__).resolve().parent
REPO = _HERE if (_HERE / "mirror.py").exists() else _HERE.parent
sys.path.insert(0, str(REPO))

# The poster loads .env lazily into a private dict (never os.environ). Point it at a
# throwaway .env so a real credential file is never read during tests.
_ENVDIR = Path(tempfile.mkdtemp(prefix="xlistener-env-"))
(_ENVDIR / ".env").write_text(
    "THREADS_ACCESS_TOKEN=test-token\nTHREADS_USER_ID=22222222222222222\n")
os.environ["THREADS_POST_ENV"] = str(_ENVDIR / ".env")

import mirror as m  # noqa: E402

_cfg_path = REPO / "config.example.json"
if not _cfg_path.exists():
    _cfg_path = REPO / "config.json"
cfg = m.load_config(_cfg_path)
script_path = REPO / cfg["threads_poster_script"]
spec = importlib.util.spec_from_file_location("p2t", script_path)
p2t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p2t)

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        failures.append(name)


def expect_exit(name, fn):
    try:
        fn()
    except SystemExit:
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception {type(e).__name__}: {e}")
    else:
        check(name, False, "did not exit")


# ---------------- parent container payload ----------------
captured = []
uploads: list[str] = []


def counting_upload(path):
    uploads.append(str(path))
    return "https://cdn.example/" + Path(path).name


def fake_form_post(url, data, timeout=30.0):
    captured.append({"url": url, "data": dict(data)})
    n = len([c for c in captured if c["data"].get("is_carousel_item") == "true"])
    return {"id": f"child{n}" if data.get("is_carousel_item") == "true" else "parent1"}


p2t._form_post = fake_form_post

parent_id = p2t._create_carousel_parent("tok", "uid", "caption here", ["c1", "c2", "c3"])
check("parent container id returned", parent_id == "parent1")
last = captured[-1]["data"]
check("parent media_type=CAROUSEL", last.get("media_type") == "CAROUSEL")
check("parent children is a comma-separated id string", last.get("children") == "c1,c2,c3", last.get("children"))
check("parent carries the caption", last.get("text") == "caption here")
check("parent has no is_carousel_item", "is_carousel_item" not in last)

# validation
captured.clear()
expect_exit("1-item carousel rejected", lambda: p2t._create_carousel_parent("t", "u", "x", ["only"]))
expect_exit("21-item carousel rejected",
            lambda: p2t._create_carousel_parent("t", "u", "x", [f"c{i}" for i in range(21)]))
expect_exit("over-long carousel caption rejected",
            lambda: p2t._create_carousel_parent("t", "u", "x" * 501, ["a", "b"]))

# ---------------- child container payload ----------------
captured.clear()
p2t._create_image_container("tok", "uid", "", "https://cdn.example/a.jpg", None, is_carousel_item=True)
child = captured[-1]["data"]
check("child media_type=IMAGE", child.get("media_type") == "IMAGE")
check("child is_carousel_item='true'", child.get("is_carousel_item") == "true")
check("child has no text", "text" not in child)
check("child has image_url", child.get("image_url") == "https://cdn.example/a.jpg")

# ---------------- full carousel flow ----------------
captured.clear()
order = []
p2t._form_post = lambda url, data, timeout=30.0: (
    order.append(data.get("media_type")),
    captured.append({"url": url, "data": dict(data)}),
    {"id": ("parent9" if data.get("media_type") == "CAROUSEL" else f"child{len(captured)}")},
)[2]
p2t._wait_for_image_ready = lambda *a, **k: None
p2t.upload_to_public_host = counting_upload
p2t._publish_with_verify = lambda token, uid, cid, recreate_fn=None, max_retries=2: {
    "id": "post123", "permalink": "https://www.threads.net/@example_threads_user/post/ABC"}

import tempfile
_tmp = Path(tempfile.mkdtemp(prefix="carousel-"))
_shots = []
for _name in ("a.jpg", "b.jpg", "c.jpg"):
    _p = _tmp / _name
    _p.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg-but-exists")
    _shots.append(str(_p))

result = p2t.post_carousel("caption", image_paths=_shots)
child_posts = [c for c in captured if c["data"].get("media_type") == "IMAGE"]
parent_posts = [c for c in captured if c["data"].get("media_type") == "CAROUSEL"]
check("3 child containers created", len(child_posts) == 3, len(child_posts))
check("1 parent container created", len(parent_posts) == 1, len(parent_posts))
check("children created BEFORE parent", order == ["IMAGE", "IMAGE", "IMAGE", "CAROUSEL"], order)
check("all children flagged is_carousel_item",
      all(c["data"].get("is_carousel_item") == "true" for c in child_posts))
check("children carry no text", all("text" not in c["data"] for c in child_posts))
check("parent lists all three children",
      parent_posts[0]["data"].get("children") == "child1,child2,child3",
      parent_posts[0]["data"].get("children"))
check("result reports carousel item count", result.get("carousel_items") == 3, result.get("carousel_items"))
check("result exposes published post url", "threads.net" in result.get("post_url", ""), result.get("post_url"))

# ---------------- argument validation ----------------
expect_exit("post_carousel rejects 1 image", lambda: p2t.post_carousel("x", image_urls=["https://a/b.jpg"]))
expect_exit("post_carousel rejects 21 images",
            lambda: p2t.post_carousel("x", image_urls=[f"https://a/{i}.jpg" for i in range(21)]))
expect_exit("post_carousel rejects mixing paths and urls",
            lambda: p2t.post_carousel("x", image_paths=["/tmp/a.jpg"], image_urls=["https://a/b.jpg"]))

# ---------------- dry run: plan only, no posts, no uploads ----------------
captured.clear()
uploads.clear()
dry = p2t.post_carousel("caption", image_urls=["https://a/1.jpg", "https://a/2.jpg"], dry_run=True)
check("dry run returns a plan", dry.get("dry_run") is True and len(dry.get("steps", [])) == 3, dry)
check("dry run posts nothing", captured == [], captured)
check("dry run reports item count", dry.get("carousel_items") == 2)

# single-image dry run must not upload either (this was the observe-mode media leak)
_img = _tmp / "single.jpg"
_img.write_bytes(b"\xff\xd8\xff\xe0fake")
uploads.clear()
plan = p2t.post_image("caption", image_path=str(_img), image_url=None, dry_run=True)
check("single-image dry run performs no upload", uploads == [], uploads)
check("single-image dry run does not print the real token",
      plan["step1_payload"]["access_token"] == "<token>", plan["step1_payload"].get("access_token"))

# ---------------- mirror.run_threads_post picks the right CLI flag ----------------
captured_cmd = {}


class FakeProc:
    returncode = 0
    stdout = '{"published": {"id": "1"}, "post_url": "https://www.threads.net/@x/post/A"}'
    stderr = ""


def fake_run(cmd, **kwargs):
    captured_cmd["cmd"] = cmd
    return FakeProc()


real_run = m.subprocess.run
m.subprocess.run = fake_run
try:
    import logging
    log = logging.getLogger("carousel-test")
    m.run_threads_post(cfg, "text", None, False, log,
                       image_paths=[Path("/tmp/a.jpg"), Path("/tmp/b.jpg")])
    cmd = captured_cmd["cmd"]
    check("2 photos -> --images flag used", "--images" in cmd, cmd)
    check("2 photos -> both paths passed", "/tmp/a.jpg" in cmd and "/tmp/b.jpg" in cmd, cmd)
    check("--images not used as --image", "--image" not in cmd, cmd)

    m.run_threads_post(cfg, "text", Path("/tmp/a.jpg"), False, log)
    cmd = captured_cmd["cmd"]
    check("1 photo -> --image flag used", "--image" in cmd and "--images" not in cmd, cmd)

    m.run_threads_post(cfg, "text", None, False, log, image_paths=[Path("/tmp/a.jpg")])
    cmd = captured_cmd["cmd"]
    check("image_paths of length 1 -> --image", "--image" in cmd and "--images" not in cmd, cmd)
finally:
    m.subprocess.run = real_run

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all carousel tests passed")
