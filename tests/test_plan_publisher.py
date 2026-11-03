"""No-network tests for the owner-only TrainingPeaks calendar planner.

All read and write calls are monkeypatched to fake in-memory TrainingPeaks data.
No real account, cookie or username is used by these tests.
"""

from urllib.parse import urlparse

import httpx
import pytest

from tp_mcp import plan_publisher as portal_mod
from tp_mcp.plan_publisher import COOKIE_NAME, EXPECTED_FTP, PlannerPortal, plan
from tp_mcp.remote import RemoteConfig, build_app
from tp_mcp.remote_auth import Sealer

BASE = "https://tp-test.example"
PASSWORD = "test-only-owner-password-long"
KEY = "unit-test-only-signing-key-longer-than-32-characters"
CONFIG = RemoteConfig(public_url=BASE, owner_password=PASSWORD, signing_key=KEY)
ORIGIN = {"Origin": BASE}


def test_plan_is_exactly_12_weeks_with_no_monday_and_long_course():
    drafts = plan()
    assert len(drafts) == 12
    assert all(len(week) == 6 for week in drafts)
    assert drafts[0][0].date.isoformat() == "2026-11-10"
    assert drafts[-1][-1].date.isoformat() == "2027-01-31"
    assert all(d.weekday == d.date.strftime("%a") for week in drafts for d in week)
    assert all(d.weekday != "Mon" for week in drafts for d in week)
    assert all(d.structure is not None for week in drafts for d in week
               if d.date.isoformat() != "2027-01-31")
    for week in drafts:
        for d in week:
            if d.structure is not None:
                # Each published native builder has a real %FTP intensity structure,
                # accurate target duration, and a deterministically estimated TSS.
                from tp_mcp.tools.structure import compute_if_tss, parse_structure_input
                struct = parse_structure_input(d.structure)
                i, tss, seconds = compute_if_tss(struct)
                assert seconds == d.duration_min * 60
                assert struct.primaryIntensityMetric == "percentOfFtp"
                assert 0 < i <= 1.05
                assert tss > 0
    race = drafts[-1][-1]
    assert "LOW GAP LONG" in race.title and race.distance_km == 94.95
    assert race.structure is None
    assert race.if_tss == (None, None)
    assert EXPECTED_FTP == 280


@pytest.fixture()
def mocked_trainingpeaks(monkeypatch):
    records = []
    calls = []

    async def settings():
        calls.append("settings")
        return {"settings": {"powerZones": [{"workoutTypeId": 0, "threshold": 280}]}}

    async def workouts(start_date, end_date, workout_filter="all"):
        calls.append(("get", start_date, end_date))
        return {
            "workouts": [w for w in records if start_date <= w["date"] <= end_date],
            "count": len(records),
        }

    async def create(**kwargs):
        calls.append(("create", kwargs["date_str"], kwargs["title"]))
        records.append({
            "date": kwargs["date_str"],
            "sport": kwargs["sport"],
            "title": kwargs["title"],
        })
        return {"success": True, "workout_id": str(len(records))}

    monkeypatch.setattr(portal_mod, "tp_get_athlete_settings", settings)
    monkeypatch.setattr(portal_mod, "tp_get_workouts", workouts)
    monkeypatch.setattr(portal_mod, "tp_create_workout", create)
    return records, calls


class RunningApp:
    async def __aenter__(self):
        self.app = build_app(CONFIG)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url=BASE)
        return self.client

    async def __aexit__(self, *_):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)


async def do_login(client):
    response = await client.post("/planner/login",
                                 data={"password": PASSWORD}, headers=ORIGIN)
    assert response.status_code == 303
    assert urlparse(response.headers["location"]).path == "/planner"
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    session = Sealer(KEY).open("planner-session", client.cookies[COOKIE_NAME])
    assert session and session["sub"] == "owner" and session["csrf"]
    return session["csrf"]


async def test_login_preview_csrf_and_weekly_idempotent_publishing(mocked_trainingpeaks):
    records, calls = mocked_trainingpeaks
    async with RunningApp() as client:
        public = await client.get("/planner")
        assert public.status_code == 200 and "Owner password" in public.text
        assert "W01 Tue" not in public.text
        wrong = await client.post("/planner/login",
                                  data={"password": "bad-password"}, headers=ORIGIN)
        assert wrong.status_code == 401
        csrf = await do_login(client)
        preview = await client.get("/planner")
        assert preview.status_code == 200
        assert "W01 Tue" in preview.text and "LOW GAP LONG" in preview.text
        assert "not medical clearance" in preview.text
        assert "280 W" in preview.text

        # Never publish without valid owner session, CSRF and approval.
        rejected = await client.post("/planner/publish",
                                     data={"csrf": "bad", "week": "1", "acknowledged": "yes"},
                                     headers=ORIGIN)
        assert rejected.status_code == 403
        # Browser-supplied Origin is no longer an authorization mechanism.
        # A forged cross-origin form still cannot know the private CSRF token.
        rejected = await client.post("/planner/publish",
                                     data={"csrf": "forged", "week": "1", "acknowledged": "yes"},
                                     headers={"Origin": "https://attacker.invalid"})
        assert rejected.status_code == 403
        rejected = await client.post("/planner/publish",
                                     data={"csrf": csrf, "week": "1"}, headers=ORIGIN)
        assert rejected.status_code == 400
        assert not records

        success = await client.post("/planner/publish",
                                    data={"csrf": csrf, "week": "1", "acknowledged": "yes"},
                                    headers=ORIGIN)
        assert success.status_code == 303
        assert "created+6" in success.headers["location"]
        assert len(records) == 6
        assert all(w["sport"] == "Bike" for w in records)
        again = await client.post("/planner/publish",
                                  data={"csrf": csrf, "week": "1", "acknowledged": "yes"},
                                  headers=ORIGIN)
        assert again.status_code == 303
        assert len(records) == 6, "same week must not create duplicates"
        assert any("created+0" in again.headers.get("location", "") for _ in [0])


async def test_ftp_mismatch_blocks_publishing(mocked_trainingpeaks, monkeypatch):
    records, _ = mocked_trainingpeaks

    async def changed_ftp():
        return {"settings": {"powerZones": [{"workoutTypeId": 0, "threshold": 300}]}}

    monkeypatch.setattr(portal_mod, "tp_get_athlete_settings", changed_ftp)
    async with RunningApp() as client:
        csrf = await do_login(client)
        attempt = await client.post("/planner/publish",
                                    data={"csrf": csrf, "week": "1", "acknowledged": "yes"},
                                    headers=ORIGIN)
        assert attempt.status_code == 409
        assert records == []


async def test_unlisted_week_cannot_publish(mocked_trainingpeaks):
    async with RunningApp() as client:
        csrf = await do_login(client)
        for value in ("0", "13", "not-a-week"):
            attempt = await client.post("/planner/publish",
                                        data={"csrf": csrf, "week": value, "acknowledged": "yes"},
                                        headers=ORIGIN)
            assert attempt.status_code == 400


async def test_owner_session_is_not_a_general_write_mcp_token(mocked_trainingpeaks):
    async with RunningApp() as client:
        await do_login(client)
        response = await client.post("/mcp",
                                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                     headers={"Accept": "application/json, text/event-stream",
                                              "Content-Type": "application/json"})
        assert response.status_code == 401


async def test_server_only_publishes_after_explicit_owner_action(mocked_trainingpeaks):
    records, _ = mocked_trainingpeaks
    async with RunningApp() as client:
        assert (await client.get("/planner")).status_code == 200
        await do_login(client)
        await client.get("/planner")
        assert records == [], "preview, GET, and login must never publish"


async def test_browser_post_without_origin_can_login_and_publish(mocked_trainingpeaks):
    """Some browsers omit Origin and Referer. A real login should still work.

    Signed owner session, SameSite cookie, and independent CSRF authorization
    are still enforced before creating any TrainingPeaks workout.
    """
    records, _ = mocked_trainingpeaks
    async with RunningApp() as client:
        landing = await client.get("/planner")
        assert landing.status_code == 200
        # Exercise the exact browser behavior that previously got
        # "Invalid request origin" (POST has neither Origin nor Referer).
        login = await client.post("/planner/login", data={"password": PASSWORD})
        assert login.status_code == 303
        claims = Sealer(KEY).open("planner-session", client.cookies[COOKIE_NAME])
        assert claims and claims["csrf"]
        csrf = claims["csrf"]

        bad_csrf = await client.post(
            "/planner/publish", data={"week": "1", "acknowledged": "yes"}
        )
        assert bad_csrf.status_code == 403
        foreign_origin = await client.post(
            "/planner/publish",
            data={"csrf": "fake-csrf", "week": "1", "acknowledged": "yes"},
            headers={"Origin": "https://attacker.invalid"},
        )
        assert foreign_origin.status_code == 403
        foreign_referer = await client.post(
            "/planner/publish",
            data={"csrf": "fake-csrf", "week": "1", "acknowledged": "yes"},
            headers={"Referer": "https://attacker.invalid/publish"},
        )
        assert foreign_referer.status_code == 403
        assert not records

        published = await client.post(
            "/planner/publish", data={"csrf": csrf, "week": "1", "acknowledged": "yes"}
        )
        assert published.status_code == 303
        assert len(records) == 6

        # Missing both headers is not a bypass: a cross-site attacker has
        # neither the owner session nor its unguessable CSRF value.
        client.cookies.clear()
        unsigned = await client.post(
            "/planner/publish", data={"csrf": csrf, "week": "2", "acknowledged": "yes"}
        )
        assert unsigned.status_code == 403
        assert len(records) == 6


async def test_login_and_publish_with_opaque_or_embedded_browser_origin(mocked_trainingpeaks):
    """The browser may send an opaque or chatgpt.com Origin on a same-tab form."""
    records, _ = mocked_trainingpeaks
    async with RunningApp() as client:
        wrong = await client.post(
            "/planner/login",
            data={"password": "incorrect"},
            headers={"Origin": "null"},
        )
        assert wrong.status_code == 401
        assert COOKIE_NAME not in client.cookies

        response = await client.post(
            "/planner/login",
            data={"password": PASSWORD},
            headers={"Origin": "null"},
        )
        assert response.status_code == 303
        csrf = Sealer(KEY).open("planner-session", client.cookies[COOKIE_NAME])["csrf"]
        assert (await client.get("/planner")).status_code == 200

        # Even with a correct owner login and an opaque Origin, only an
        # explicitly approved post carrying the session's CSRF can publish.
        rejected = await client.post(
            "/planner/publish",
            data={"csrf": "fake", "week": "1", "acknowledged": "yes"},
            headers={"Origin": "https://chatgpt.com"},
        )
        assert rejected.status_code == 403
        assert not records

        allowed = await client.post(
            "/planner/publish",
            data={"csrf": csrf, "week": "1", "acknowledged": "yes"},
            headers={"Origin": "https://chatgpt.com"},
        )
        assert allowed.status_code == 303
        assert len(records) == 6
