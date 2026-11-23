import io

import pytest

from archive.app import create_app
from archive.core import reel_state
from archive.db import init_db

HEADER = "镜头号,原卷号,起帧,帧数\n"


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "web.sqlite3"
    init_db(db)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(db),
            "SECRET_KEY": "test-secret",
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def submit(client, order_no, film_no, csv_text, filename="edit.csv"):
    token = csrf(client)
    return client.post(
        "/submit",
        data={
            "csrf_token": token,
            "order_no": order_no,
            "film_no": film_no,
            "csv_file": (io.BytesIO(csv_text.encode("utf-8")), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "交档" in resp.get_data(as_text=True)


def test_submit_then_detail_and_api(client, app):
    resp = submit(
        client,
        "ORD-1",
        "FILM-A",
        HEADER + "S01,R001,0,10\nS02,R001,100,5\n",
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/orders/ORD-1")

    page = client.get("/orders/ORD-1")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "S01" in body and "[0, 10)" in body

    data = client.get("/api/orders/ORD-1").get_json()
    assert data["film_no"] == "FILM-A"
    assert [(c["orig_start"], c["arch_start"]) for c in data["copies"]] == [(0, 0), (100, 10)]
    assert reel_state(app.config["DATABASE"], "R001")["next_frame"] == 15


def test_second_order_reuses_and_appends(client):
    submit(client, "ORD-1", "FILM-A", HEADER + "S01,R001,0,10\n")
    submit(client, "ORD-2", "FILM-B", HEADER + "S01,R001,5,10\n")
    data = client.get("/api/orders/ORD-2").get_json()
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["is_new"]) for c in data["copies"]] == [
        (5, 10, 5, False),
        (10, 15, 10, True),
    ]


def test_invalid_csv_leaves_reel_untouched(client, app):
    submit(client, "ORD-1", "FILM-A", HEADER + "S01,R001,0,10\n")
    before = reel_state(app.config["DATABASE"], "R001")
    resp = submit(client, "ORD-2", "FILM-B", HEADER + "S01,R001,10,0\n")
    assert resp.status_code == 400
    assert "帧数必须大于 0" in resp.get_data(as_text=True)
    assert reel_state(app.config["DATABASE"], "R001") == before
    assert client.get("/api/orders/ORD-2").status_code == 404


def test_duplicate_order_no_returns_409(client):
    submit(client, "ORD-1", "FILM-A", HEADER + "S01,R001,0,10\n")
    resp = submit(client, "ORD-1", "FILM-A", HEADER + "S01,R001,50,10\n")
    assert resp.status_code == 409
    assert "已存在" in resp.get_data(as_text=True)


def test_missing_order_no_rejected(client):
    token = csrf(client)
    resp = client.post(
        "/submit",
        data={
            "csrf_token": token,
            "order_no": "",
            "film_no": "FILM",
            "csv_file": (io.BytesIO(b"x"), "e.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "单号不能为空" in resp.get_data(as_text=True)


def test_missing_file_rejected(client):
    token = csrf(client)
    resp = client.post(
        "/submit",
        data={"csrf_token": token, "order_no": "X", "film_no": "Y"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "请选择" in resp.get_data(as_text=True)


def test_csrf_required(client):
    resp = client.post(
        "/submit",
        data={"csrf_token": "wrong", "order_no": "X", "film_no": "Y"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_unknown_order_and_reel_404(client):
    assert client.get("/orders/NOPE").status_code == 404
    assert client.get("/reels/NOPE").status_code == 404
    assert client.get("/api/orders/NOPE").status_code == 404
    assert client.get("/api/reels/NOPE").status_code == 404


def test_reel_page_shows_tail(client):
    submit(client, "ORD-1", "FILM-A", HEADER + "S01,R001,0,10\n")
    resp = client.get("/reels/R001")
    assert resp.status_code == 200
    assert "10" in resp.get_data(as_text=True)


def test_order_no_with_slash_via_api(client):
    submit(client, "ORD/1", "FILM-A", HEADER + "S01,R001,0,10\n")
    data = client.get("/api/orders/ORD%2F1").get_json()
    assert data["order_no"] == "ORD/1"
