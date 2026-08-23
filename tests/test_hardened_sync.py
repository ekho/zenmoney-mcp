from __future__ import annotations

import pytest

from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.hardened_sync import HardenedSyncEngine, SyncError


def base_db():
    db=HardenedDatabase(':memory:'); db.init_schema()
    db.upsert_instruments([{'id':1,'title':'RUB','shortTitle':'RUB','symbol':'₽','rate':1,'changed':1}])
    db.upsert_users([{'id':1,'login':'u','currency':1,'parent':None,'changed':1}])
    db.upsert_accounts([{'id':'stale','title':'Stale','type':'checking','instrument':1,'balance':10,'user':1,'changed':1}])
    db.set_server_timestamp(10)
    return db


def full_snapshot():
    return {
        'serverTimestamp':20,
        'instrument':[{'id':1,'title':'RUB','shortTitle':'RUB','symbol':'₽','rate':1,'changed':2}],
        'user':[{'id':1,'login':'u','currency':1,'parent':None,'changed':2}],
        'account':[{'id':'fresh','title':'Fresh','type':'checking','instrument':1,'balance':20,'user':1,'changed':2}],
    }


def test_force_full_replaces_cache_instead_of_leaving_stale_rows():
    db=base_db(); engine=HardenedSyncEngine(db,'token')
    engine.apply_diff_data(full_snapshot(),force_full=True)
    ids=[r['id'] for r in db.connect().execute('SELECT id FROM accounts').fetchall()]
    assert ids == ['fresh']
    assert db.get_server_timestamp() == 20


def test_incremental_keeps_existing_rows_and_adds_changes():
    db=base_db(); engine=HardenedSyncEngine(db,'token')
    diff={'serverTimestamp':20,'account':[{'id':'fresh','title':'Fresh','type':'checking','instrument':1,'balance':20,'user':1,'changed':2}]}
    engine.apply_diff_data(diff,force_full=False)
    ids={r['id'] for r in db.connect().execute('SELECT id FROM accounts').fetchall()}
    assert ids == {'stale','fresh'}


def test_missing_timestamp_does_not_mutate_original_cache():
    db=base_db(); engine=HardenedSyncEngine(db,'token')
    with pytest.raises(SyncError,match='serverTimestamp'):
        engine.apply_diff_data({'account':[{'id':'new'}]},force_full=True)
    ids=[r['id'] for r in db.connect().execute('SELECT id FROM accounts').fetchall()]
    assert ids == ['stale']
    assert db.get_server_timestamp() == 10


def test_failed_staging_apply_is_atomic():
    db=base_db(); engine=HardenedSyncEngine(db,'token')
    with pytest.raises(KeyError):
        engine.apply_diff_data({'serverTimestamp':20,'account':[{'title':'missing id'}]},force_full=False)
    ids=[r['id'] for r in db.connect().execute('SELECT id FROM accounts').fetchall()]
    assert ids == ['stale']
    assert db.get_server_timestamp() == 10


def test_nullable_budget_upsert_is_idempotent_and_extended_fields_are_persisted():
    db=HardenedDatabase(':memory:'); db.init_schema()
    budget={'user':1,'tag':None,'date':'2026-08-01','income':0,'incomeLock':False,'outcome':100,'outcomeLock':True,'changed':1}
    db.upsert_budgets([budget]); budget['outcome']=200; db.upsert_budgets([budget])
    rows=db.connect().execute('SELECT * FROM budgets').fetchall()
    assert len(rows)==1 and rows[0]['outcome']==200 and rows[0]['tag_key']==''
    db.upsert_accounts([{'id':'a','title':'A','type':'checking','instrument':1,'balance':1,'startBalance':7,'changed':1}])
    assert db.connect().execute('SELECT start_balance FROM accounts WHERE id="a"').fetchone()['start_balance']==7
    db.upsert_reminders([{'id':'r','interval':'month','step':2,'points':[1,15],'outcome':50,'outcomeInstrument':1,'changed':1}])
    row=db.connect().execute('SELECT points,outcome_instrument FROM reminders WHERE id="r"').fetchone()
    assert row['points']=='[1, 15]' and row['outcome_instrument']==1


@pytest.mark.asyncio
async def test_force_full_http_sync_retries_once_and_uses_snapshot_replacement(monkeypatch):
    db = base_db()
    engine = HardenedSyncEngine(db, "token")
    calls: list[dict] = []

    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return full_snapshot()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            if len(calls) == 1:
                import httpx

                raise httpx.RemoteProtocolError("connection closed")
            return Response()

    monkeypatch.setattr("zenmoney_mcp.hardened_sync.httpx.AsyncClient", Client)

    result = await engine.sync(force_full=True)

    assert len(calls) == 2
    assert calls[-1]["json"]["serverTimestamp"] == 0
    assert calls[-1]["timeout"] == 300.0
    assert result["full_replacement"] is True
    ids = [row["id"] for row in db.connect().execute("SELECT id FROM accounts")]
    assert ids == ["fresh"]


@pytest.mark.asyncio
async def test_incremental_http_sync_does_not_retry_protocol_errors(monkeypatch):
    db = base_db()
    engine = HardenedSyncEngine(db, "token")
    calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            import httpx

            raise httpx.RemoteProtocolError("connection closed")

    monkeypatch.setattr("zenmoney_mcp.hardened_sync.httpx.AsyncClient", Client)

    with pytest.raises(SyncError, match="after 1 attempts"):
        await engine.sync(force_full=False)

    assert calls == 1
    assert db.get_server_timestamp() == 10
    ids = [row["id"] for row in db.connect().execute("SELECT id FROM accounts")]
    assert ids == ["stale"]


def test_malformed_deletion_is_rejected_without_mutating_cache():
    db = base_db()
    engine = HardenedSyncEngine(db, "token")

    with pytest.raises(SyncError, match="deletion"):
        engine.apply_diff_data(
            {"serverTimestamp": 20, "deletion": [{"object": "account"}]},
            force_full=False,
        )

    assert db.get_server_timestamp() == 10
    ids = [row["id"] for row in db.connect().execute("SELECT id FROM accounts")]
    assert ids == ["stale"]


def test_incremental_budget_deletion_is_rejected_without_mutating_cache():
    db = base_db()
    engine = HardenedSyncEngine(db, "token")
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": None,
                "date": "2026-08-01",
                "outcome": 100,
                "changed": 1,
            }
        ]
    )

    with pytest.raises(SyncError, match="full sync"):
        engine.apply_diff_data(
            {
                "serverTimestamp": 20,
                "deletion": [{"object": "budget", "id": "opaque-budget-id"}],
            },
            force_full=False,
        )

    assert db.get_server_timestamp() == 10
    assert db.connect().execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_http_error_does_not_expose_response_body(monkeypatch):
    db = base_db()
    engine = HardenedSyncEngine(db, "token")

    class Response:
        status_code = 500
        text = "sensitive upstream response"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("zenmoney_mcp.hardened_sync.httpx.AsyncClient", Client)

    with pytest.raises(SyncError) as error:
        await engine.sync()

    assert "status 500" in str(error.value)
    assert Response.text not in str(error.value)


def test_non_positive_server_timestamp_is_rejected_without_mutating_cache():
    for timestamp in (0, -1):
        db = base_db()
        engine = HardenedSyncEngine(db, "token")

        with pytest.raises(SyncError, match="positive serverTimestamp"):
            engine.apply_diff_data(
                {"serverTimestamp": timestamp, "account": []},
                force_full=True,
            )

        assert db.get_server_timestamp() == 10
        ids = [row["id"] for row in db.connect().execute("SELECT id FROM accounts")]
        assert ids == ["stale"]


def test_publication_failure_restores_original_cache(monkeypatch):
    db = base_db()
    engine = HardenedSyncEngine(db, "token")

    def destructive_publish(staging):
        del staging
        conn = db.connect()
        conn.execute("DELETE FROM accounts")
        conn.execute(
            "INSERT OR REPLACE INTO sync_meta(key,value) VALUES ('server_timestamp','999')"
        )
        conn.commit()
        raise RuntimeError("publication failed")

    monkeypatch.setattr(engine, "_publish_staging", destructive_publish)

    with pytest.raises(RuntimeError, match="publication failed"):
        engine.apply_diff_data(full_snapshot(), force_full=True)

    assert db.get_server_timestamp() == 10
    ids = [row["id"] for row in db.connect().execute("SELECT id FROM accounts")]
    assert ids == ["stale"]
