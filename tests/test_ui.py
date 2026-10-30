"""fugart ui: the page's API calls the same gates as the CLI, and nothing else gets in."""

import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import cli
from test_boxing import imported
from test_imports import reviewable
from test_review import boxed

from fugart import review, ui

assert imported and reviewable and boxed


@pytest.fixture
def server(boxed):
    workspace, root = boxed
    made = ui.make_server(workspace, 0)
    thread = threading.Thread(target=made.serve_forever, daemon=True)
    thread.start()
    port = made.server_address[1]
    token = made.RequestHandlerClass.token
    yield workspace, root, f"http://127.0.0.1:{port}", token
    made.shutdown()
    made.server_close()


def call(url, body=None, token=None, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Fugau-Token"] = token
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_page_carries_its_token_and_overview_lists_the_batch_bird(server):
    _, _, base, token = server
    status, page = call(base + "/")
    assert status == 200 and token.encode() in page
    status, raw = call(base + "/api/overview")
    assert status == 200
    assert "status" in json.loads(raw)


def test_bird_shows_each_gate_and_the_saved_box(server):
    _, _, base, _ = server
    status, raw = call(base + "/api/bird/hirundo-neoxena")
    bird = json.loads(raw)
    assert status == 200 and bird["phase"] == "boxed"
    assert bird["gates"]["final"] == "current" and bird["gates"]["box"] == "done"
    assert bird["box"]["box"] == [0.1, 0.2, 0.8, 0.9]


def test_posts_need_the_page_token_and_the_right_host(server):
    _, _, base, token = server
    assert call(base + "/api/review/build", {"species": "hirundo-neoxena"})[0] == 403
    assert call(base + "/api/overview", host="evil.example:80")[0] == 403
    status, raw = call(base + "/api/review/build", {"species": "hirundo-neoxena"}, token)
    assert status == 200, raw


def test_files_are_confined_to_work_and_the_artwork(server):
    workspace, _, base, _ = server
    (workspace / "work/hello.txt").write_text("hi")
    assert call(base + "/files/work/hello.txt")[0] == 200
    for path in (
        "/files/.fugart/fugart.sqlite3",
        "/files/work/../pyproject.toml",
        "/files/upstream/pyproject.toml",
    ):
        assert call(base + path)[0] == 404, path


def test_approval_needs_the_tick_name_and_notes_like_the_cli(server):
    workspace, _, base, token = server
    built = review.build(workspace, "Welcome Swallow")
    body = {
        "species": "hirundo-neoxena",
        "review": built["id"],
        "reviewer": "Reviewer",
        "notes": "ok",
    }
    status, raw = call(base + "/api/review/approve", body, token)
    assert status == 400 and b"tick" in raw
    status, raw = call(base + "/api/review/approve", body | {"confirm": True, "notes": ""}, token)
    assert status == 400
    status, raw = call(base + "/api/review/approve", body | {"confirm": True}, token)
    assert status == 200, raw
    assert json.loads(call(base + "/api/bird/hirundo-neoxena")[1])["review"]["approved"]


def test_final_step_gets_the_aligned_review_layers(server):
    workspace, _, base, _ = server
    review.build(workspace, "Welcome Swallow")
    files = json.loads(call(base + "/api/bird/hirundo-neoxena")[1])["review"]["files"]
    for name in ui.REVIEW_LAYERS:
        assert files[name].endswith(f"img/hirundo-neoxena/{name}"), name
        assert call(base + "/files/" + files[name])[0] == 200, name


def test_feedback_queues_for_the_agent_and_resolves(server):
    workspace, _, base, token = server
    note = {
        "species": "Welcome Swallow",
        "gate": "cut",
        "notes": "Tail tip clipped",
        "reviewer": "Reviewer",
    }
    status, raw = call(base + "/api/feedback", note, token)
    assert status == 200, raw
    made = json.loads(raw)
    listed = cli(workspace, "feedback", "--json")
    assert [n["id"] for n in listed] == [made["id"]] and listed[0]["species"] == "hirundo-neoxena"
    cli(workspace, "feedback", "--resolve", made["id"], "--notes", "Recut v003", "--by", "agent")
    assert cli(workspace, "feedback", "--json") == []
    assert cli(workspace, "feedback", "--all", "--json")[0]["resolution"]["notes"] == "Recut v003"


def test_a_settled_box_cannot_be_redrawn_from_the_page(server):
    _, _, base, token = server
    body = {"species": "hirundo-neoxena", "box": [0.2, 0.25, 0.7, 0.8], "reviewer": "Reviewer"}
    # the fixture bird was imported, not replaced, so its box is settled
    status, raw = call(base + "/api/box", body, token)
    assert status == 400 and b"box" in raw
