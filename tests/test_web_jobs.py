"""Full-library query contracts and isolated SQL correctness, never production jobs."""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_web_console import HEADERS, ORIGIN, PASSWORD, SNAPSHOT, Backend, login

from rag_favorite import web_console as web
from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.queue_control import set_control
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_store import library_id
from rag_favorite.web_jobs import WebJobQueries


class Queries:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.item = {
            **SNAPSHOT["jobs"][0],
            "display_state": "queued",
            "reason_code": "UNKNOWN",
        }

    def summary(self):
        return {
            "counts": {"queued": 2401},
            "reasons": [
                {
                    "code": "UNKNOWN",
                    "label": "未确认原因",
                    "count": 2,
                    "secret": "PRIVATE",
                }
            ],
            "collections": ["unified"],
            "total": 2401,
            "captured_at": "now",
            "private": "PRIVATE",
        }

    def list_jobs(self, **kwargs):
        if self.fail:
            raise RuntimeError("PRIVATE_DATABASE_PASSWORD")
        self.calls.append(kwargs)
        return {
            "items": [self.item],
            "total": 2401,
            "page": kwargs["page"],
            "page_size": kwargs["page_size"],
            "captured_at": "now",
            "private": "PRIVATE",
        }

    def updates(self, ids):
        self.calls.append(ids)
        return {
            "items": [self.item] if self.item["id"] in ids else [],
            "missing_ids": [i for i in ids if i != self.item["id"]],
            "captured_at": "now",
        }

    def get_job(self, job_id):
        return self.item if job_id == self.item["id"] else None


@pytest.fixture
def query_client():
    queries = Queries()
    settings = {
        "origin": ORIGIN,
        "owner_login": "owner@example.com",
        "username": "admin",
        "allowed_ips": {"100.64.0.1", "100.64.0.2"},
        "self_ips": {"100.64.0.1"},
        "salt": "aa" * 16,
        "password_hash": web.password_hash(PASSWORD, "aa" * 16),
    }
    with TestClient(
        web.create_app(Backend(), settings, job_queries=queries),
        base_url=ORIGIN,
        client=("127.0.0.1", 1234),
        headers=HEADERS,
    ) as client:
        yield client, queries


def test_task_routes_require_session_and_same_network_boundary(query_client):
    client, queries = query_client
    paths = [
        "/api/jobs",
        "/api/jobs/updates?ids=" + queries.item["id"],
        "/api/jobs/" + queries.item["id"],
    ]
    for path in paths:
        assert client.get(path).status_code == 401
    login(client)
    for path in paths:
        assert (
            client.get(path, headers={"x-forwarded-for": "100.64.0.99"}).status_code
            == 403
        )


def test_full_query_arguments_and_public_projection(query_client):
    client, queries = query_client
    login(client)
    args = {
        "q": "标题%_",
        "state": "attention",
        "reason": "SOURCE_MISSING",
        "collection": "unified",
        "page": 53,
        "page_size": 40,
    }
    response = client.get("/api/jobs", params=args)
    assert response.status_code == 200
    assert queries.calls[-1] == args
    assert response.json()["total"] == 2401
    assert "PRIVATE" not in response.text
    assert response.json()["items"][0]["display_state"] == "queued"
    assert response.headers["cache-control"] == "no-store"
    result = client.get("/api/jobs/" + queries.item["id"])
    assert result.status_code == 200 and "PRIVATE" not in result.text
    assert client.get("/api/jobs/" + str(uuid4())).status_code == 404


@pytest.mark.parametrize(
    "args",
    [
        {"page": 0},
        {"page_size": 101},
        {"state": "bogus"},
        {"q": "x" * 201},
        {"reason": "x" * 201},
    ],
)
def test_query_validation(query_client, args):
    client, queries = query_client
    login(client)
    assert client.get("/api/jobs", params=args).status_code == 422
    assert not queries.calls


def test_batch_ids_and_safe_errors(query_client):
    client, queries = query_client
    login(client)
    known, absent = queries.item["id"], str(uuid4())
    response = client.get(
        "/api/jobs/updates", params={"ids": f"{known},{known},{absent}"}
    )
    assert response.status_code == 200 and queries.calls[-1] == [known, absent]
    assert response.json()["missing_ids"] == [absent]
    assert "PRIVATE" not in response.text
    assert client.get("/api/jobs/updates?ids=invalid").status_code == 422
    assert (
        client.get(
            "/api/jobs/updates",
            params={"ids": ",".join(str(uuid4()) for _ in range(101))},
        ).status_code
        == 422
    )
    queries.fail = True
    response = client.get("/api/jobs")
    assert response.status_code == 503 and "PRIVATE" not in response.text


def test_compact_overview_keeps_legacy_snapshot_contract(query_client):
    client, _ = query_client
    login(client)
    legacy = client.get("/api/snapshot").json()["snapshot"]
    compact = client.get("/api/snapshot?include_jobs=false").json()["snapshot"]
    assert "jobs" in legacy and "jobs" in legacy["pipeline"]
    assert "task_summary" not in legacy
    assert (
        "jobs" not in compact
        and "jobs" not in compact["pipeline"]
        and "running" not in compact["pipeline"]
    )
    assert compact["task_summary"]["total"] == 2401
    assert "PRIVATE" not in json.dumps(compact)


@pytest.fixture
def sql_queries(tmp_path):
    path = os.environ.get("RAG_GUI_TEST_CONFIG")
    if not path:
        pytest.skip("Set RAG_GUI_TEST_CONFIG to use isolated PostgreSQL fixture")
    config = load_config(path)
    video = VideoConfig(
        root=tmp_path / "ui-library", credentials_file=tmp_path / "unused"
    )
    video.root.mkdir()
    own = library_id(video)
    ids = [str(uuid4()) for _ in range(2405)]
    foreign = str(uuid4())
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    with connect_database(config, register_pgvector=False) as db:
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload,created_at,updated_at,priority) VALUES(%s,%s,'unified','queued','queued',%s::jsonb,%s,%s,%s)",
                [
                    (
                        value,
                        own,
                        json.dumps(
                            {"title": "历史任务 " + str(i), "secret": "NEVER_EXPOSE"}
                        ),
                        stamp,
                        stamp,
                        5000 - i,
                    )
                    for i, value in enumerate(ids)
                ],
            )
        db.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload) VALUES(%s,%s,'foreign','failed','failed',%s::jsonb)",
            (foreign, "ui-foreign-" + foreign, json.dumps({"title": "FOREIGN_SECRET"})),
        )
    try:
        yield config, video, ids, foreign, WebJobQueries(config, video)
    finally:
        with connect_database(config, register_pgvector=False) as db:
            db.execute(
                "DELETE FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[])",
                (ids + [foreign],),
            )


def test_sql_full_library_pagination_literal_search_and_rank(sql_queries):
    config, _video, ids, foreign, queries = sql_queries
    result = queries.list_jobs(q="历史任务 2404")
    assert result["total"] == 1 and result["items"][0]["id"] == ids[-1]
    assert result["items"][0]["queue_rank"] == 2405
    last = queries.list_jobs(page=9999)
    assert last["total"] == 2405 and last["page"] == 61 and len(last["items"]) == 5
    first = queries.list_jobs()
    assert [row["id"] for row in first["items"]] == ids[:40]
    assert queries.list_jobs()["items"] == first["items"]
    with connect_database(config, register_pgvector=False) as db:
        db.execute(
            "UPDATE public.rag_video_jobs SET payload=%s::jsonb WHERE id=%s",
            (
                json.dumps({"title": r"字面 100%_\检索", "token": "NEVER_EXPOSE"}),
                ids[1],
            ),
        )
    for q in ["%", "_", "\\"]:
        assert queries.list_jobs(q=q)["total"] == 1
    assert queries.get_job(foreign) is None
    assert queries.updates([ids[0], foreign])["missing_ids"] == [foreign]
    assert "NEVER_EXPOSE" not in json.dumps(queries.list_jobs())
    assert "FOREIGN_SECRET" not in json.dumps(queries.summary())


def test_sql_display_states_reason_groups_and_phase_isolation(sql_queries):
    config, video, ids, _foreign, queries = sql_queries
    own = library_id(video)
    with connect_database(config, register_pgvector=False) as db:
        db.execute(
            "UPDATE public.rag_video_jobs SET state='running' WHERE id=ANY(%s::uuid[])",
            ([ids[0], ids[1]],),
        )
        for value in ids[:2]:
            db.execute(
                "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status,retry_at) VALUES(%s,%s,'llm','queued',now()+interval '1 hour')",
                (value, own),
            )
        db.execute(
            "UPDATE public.rag_video_jobs SET state='blocked',error_code='XHS_SOURCE_URL_REQUIRED' WHERE id=ANY(%s::uuid[])",
            ([ids[2], ids[3]],),
        )
        db.execute(
            "UPDATE public.rag_video_jobs SET state='failed',error_code=NULL WHERE id=%s",
            (ids[4],),
        )
        db.execute(
            "UPDATE public.rag_video_jobs SET state='complete' WHERE id=%s", (ids[5],)
        )
        db.execute(
            "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status,error_code) VALUES(%s,%s,'llm','blocked','STALE_PHASE')",
            (ids[5], own),
        )
        db.execute(
            "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status,error_code) VALUES(%s,%s,'llm','blocked','FOREIGN_SECRET')",
            (ids[6], "wrong-library"),
        )
        db.execute(
            "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status) VALUES(%s,%s,'download','queued'),(%s,%s,'prepare','running')",
            (ids[7], own, ids[7], own),
        )
    set_control(video, paused=True)
    set_control(video, job_id=ids[0], job_paused=True)
    assert queries.get_job(ids[0])["display_state"] == "paused"
    assert queries.get_job(ids[1])["display_state"] == "queued"
    assert queries.get_job(ids[1])["pipeline_status"] == "retry_wait"
    assert queries.get_job(ids[5])["display_state"] == "complete"
    assert queries.get_job(ids[5])["pipeline_stage"] is None
    assert queries.get_job(ids[6])["pipeline_stage"] is None
    assert queries.get_job(ids[7])["pipeline_stage"] == "prepare"
    assert queries.list_jobs(q="历史任务 7")["items"][0]["id"] == ids[7]
    summary = queries.summary()
    assert summary["total"] == 2405
    assert summary["counts"]["paused"] == 1
    assert summary["counts"]["queued"] == 2400
    assert summary["reasons"] == [
        {
            "code": "XHS_SOURCE_URL_REQUIRED",
            "label": "需要有效的小红书来源链接",
            "count": 2,
        },
        {"code": "UNKNOWN", "label": "未确认原因", "count": 1},
    ]
    assert (
        queries.list_jobs(state="attention", reason="XHS_SOURCE_URL_REQUIRED")["total"]
        == 2
    )
    assert queries.list_jobs(state="paused")["total"] == 1
    assert queries.list_jobs(q="does-not-exist")["page"] == 1
    with connect_database(config, register_pgvector=False) as db:
        db.execute(
            "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status,error_code) VALUES(%s,%s,'llm','blocked','PHASE_ONLY_ERROR')",
            (ids[4], own),
        )
    assert queries.list_jobs(q="PHASE_ONLY_ERROR")["items"][0]["id"] == ids[4]
