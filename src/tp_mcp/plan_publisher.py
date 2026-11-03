"""Owner-only TrainingPeaks calendar publisher for the Low Gap 2027 build.

The public MCP endpoint remains read-only. This web page requires a separate
owner-password login and an explicit per-week publish action. No browser
credentials are sent to ChatGPT; no work is published on GET or login.

The training plan is provisional and requires clinical clearance before hard
sessions given the athlete's recent severe exertional breathing symptoms.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from tp_mcp.remote_auth import Sealer
from tp_mcp.tools.settings import tp_get_athlete_settings
from tp_mcp.tools.structure import compute_if_tss, parse_structure_input
from tp_mcp.tools.workouts import tp_create_workout, tp_get_workouts

log = logging.getLogger("tp-mcp.planner")

START_DATE = date(2026, 11, 9)
RACE_DATE = date(2027, 1, 31)
EXPECTED_FTP = 280
COOKIE_NAME = "tp_planner_session"
SESSION_SECONDS = 1800
AUTH_FAILURE_LIMIT = 8
AUTH_FAILURE_WINDOW = 900
# Bike minutes Tuesday through Sunday, with Monday intentionally OFF.
WEEK_DURATIONS: tuple[tuple[int, ...], ...] = (
    (60, 50, 60, 30, 135, 90),
    (70, 60, 70, 30, 180, 120),
    (45, 45, 45, 25, 120, 75),
    (75, 70, 70, 30, 180, 120),
    (80, 75, 80, 30, 210, 150),
    (50, 45, 50, 25, 135, 90),
    (80, 75, 80, 30, 210, 150),
    (85, 90, 85, 35, 240, 165),
    (55, 55, 55, 25, 150, 110),
    (85, 85, 85, 30, 240, 180),
    (75, 70, 75, 25, 210, 150),
    (50, 45, 35, 25, 30, 270),
)
PHASES = (
    "Foundation", "Foundation", "Recovery",
    "Aerobic development", "Aerobic development", "Recovery",
    "Climbing durability", "Climbing durability", "Recovery",
    "Low Gap specificity", "Low Gap specificity", "Race week",
)
DAY_LABELS = ("Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# A shared lock prevents concurrent browsers from publishing duplicate workouts
# in this single-worker Render deployment. Per-date duplicate checks also make
# retries safe across app restarts after a partial publish.
PUBLISH_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class Draft:
    date: date
    week: int
    weekday: str
    title: str
    description: str
    duration_min: int
    structure: dict[str, Any] | None
    distance_km: float | None = None

    @property
    def if_tss(self) -> tuple[float | None, float | None]:
        if self.structure is None:
            return None, None
        intensity, stress, _ = compute_if_tss(parse_structure_input(self.structure))
        return intensity, stress


def _step(name: str, seconds: int, lo: float, hi: float, kind: str = "active") -> dict[str, Any]:
    return {
        "name": name,
        "duration_seconds": seconds,
        "intensity_min": lo,
        "intensity_max": hi,
        "intensityClass": kind,
    }


def _repeat(name: str, reps: int, *steps: dict[str, Any]) -> dict[str, Any]:
    return {"type": "repetition", "name": name, "reps": reps, "steps": list(steps)}


def _format_structure(minutes: int, block: dict[str, Any] | None, *,
                      base_lo: float = 60, base_hi: float = 70,
                      warm: int = 10, cool: int = 10) -> dict[str, Any]:
    """Build exactly N minutes of native-duration structure at %FTP."""
    steps: list[dict[str, Any]] = [
        _step("Easy warm-up", warm * 60, 45, 58, "warmUp")
    ]
    block_seconds = 0
    if block:
        if block["type"] == "repetition":
            block_seconds = block["reps"] * sum(s["duration_seconds"] for s in block["steps"])
        else:
            block_seconds = block["duration_seconds"]
        steps.append(block)
    main = minutes * 60 - (warm + cool) * 60 - block_seconds
    if main < 0:
        raise ValueError("Intervals longer than the requested workout duration")
    if main:
        steps.append(_step("Steady aerobic riding; terrain first", main, base_lo, base_hi))
    steps.append(_step("Easy spin home", cool * 60, 40, 52, "coolDown"))
    return {"primaryIntensityMetric": "percentOfFtp", "steps": steps}


def _workout(day: date, week: int, index: int, minutes: int) -> Draft:
    weekday = DAY_LABELS[index]
    recovery = week in (3, 6, 9)
    taper = week == 12
    block: dict[str, Any] | None = None
    base = (59.0, 69.0)
    effort = ""
    intensity = ""

    if weekday == "Tue":
        if recovery:
            effort, intensity = "Aerobic form", "Relaxed pedaling; no hard efforts."
            base = (55, 66)
        elif taper:
            effort, intensity = "Openers", "Short controlled surges only; stay fresh."
            block = _repeat("Brief openers", 3, _step("Controlled surge", 60, 96, 105),
                            _step("Easy recovery", 180, 45, 55))
            base = (55, 65)
        elif week in (5, 8, 11):
            reps = {5: 4, 8: 5, 11: 4}[week]
            effort = "Aerobic power"
            intensity = "High intensity ONLY after medical clearance; swap for easy Z2 if not cleared."
            block = _repeat("3-minute aerobic-power repeats", reps,
                            _step("VO2 effort", 180, 108, 113),
                            _step("Recovery", 180, 45, 55))
        else:
            reps, length = (3, 8) if week <= 2 else ((3, 10) if week <= 7 else (3, 12))
            effort = "Sustained threshold"
            intensity = "Subthreshold to threshold. Hard efforts conditional on medical clearance."
            block = _repeat("Sustained power", reps,
                            _step("Progressive effort", length * 60, 88 if week <= 2 else 92,
                                  94 if week <= 2 else 99),
                            _step("Easy recovery", 4 * 60, 50, 60))
    elif weekday == "Wed":
        effort, intensity = "Endurance + cadence", "Comfortable Z2; prioritize relaxed form."
        base = (57, 70)
        if minutes >= 60 and not recovery and not taper:
            block = _repeat("Cadence skills", 3, _step("High cadence, controlled power", 180, 60, 68),
                            _step("Normal cadence", 180, 55, 65))
    elif weekday == "Thu":
        if recovery:
            effort, intensity = "Low-stress cadence", "No threshold work in recovery week."
            base = (55, 65)
        elif taper:
            effort, intensity = "Short pre-race spin", "A few brief accelerations; stop feeling fresh."
            block = _repeat("Race-week wake-up", 2, _step("Fast pedals", 30, 95, 105),
                            _step("Easy", 180, 45, 55))
            base = (55, 65)
        else:
            reps = 2 if week <= 4 else 3
            length = 10 if week <= 4 else 12
            effort = "Climbing tempo / strength"
            intensity = "Seated climbing; controlled cadence 65–80 rpm if comfortable. Conditional on clearance."
            block = _repeat("Steady climbs", reps,
                            _step("Seated climb", length * 60, 82 if week <= 4 else 86,
                                  89 if week <= 4 else 93),
                            _step("Float back down", 5 * 60, 48, 58))
            base = (57, 67)
    elif weekday == "Fri":
        effort, intensity = "Recovery spin", "Easy only; optional rest if tired."
        base = (45, 55)
    elif weekday == "Sat":
        if taper:
            effort, intensity = "Short shakeout", "Easy bike check, no fatigue."
            base = (48, 60)
        else:
            effort = "Long gravel endurance"
            intensity = "Mostly Z2. Technical skills and descending safety take priority over holding watts."
            if recovery:
                base = (55, 66)
            else:
                climb_reps = 2 if week <= 4 else 3
                climb_min = 8 if week <= 4 else (12 if week <= 8 else 15)
                block = _repeat("Climbing within long ride", climb_reps,
                                _step("Seated sustainable climb", climb_min * 60, 80, 88),
                                _step("Easy rolling", 8 * 60, 48, 60))
                base = (58, 70)
    elif weekday == "Sun":
        if week == 12:
            return Draft(day, week, weekday, "W12 Sun | LOW GAP LONG — RACE 59 mi",
                         "Grasshopper Low Gap long course: 59 miles / ~6,585 ft climbing. "
                         "B race before July Worlds. This is a race placeholder, NOT a fixed ERG workout; "
                         "the duration is provisional, and actual TSS is intentionally not estimated.",
                         minutes, None, 94.95)
        effort, intensity = "Endurance on tired legs", "Second long day; keep power lower than Saturday."
        base = (53, 67) if not recovery else (50, 62)
    else:
        raise ValueError("Unexpected day")

    if weekday == "Fri":
        structure = {
            "primaryIntensityMetric": "percentOfFtp",
            "steps": [_step("Easy recovery spin", minutes * 60, 45, 55)],
        }
    elif weekday == "Sat" and taper:
        structure = {
            "primaryIntensityMetric": "percentOfFtp",
            "steps": [_step("Light pre-race spin", minutes * 60, 48, 60)],
        }
    else:
        warm = 7 if minutes < 50 else 10
        cool = 7 if minutes < 50 else 10
        structure = _format_structure(minutes, block, base_lo=base[0],
                                      base_hi=base[1], warm=warm, cool=cool)
    title = f"W{week:02d} {weekday} | {effort}"
    description = (
        f"Low Gap 59-mi winter build; week {week}/12 ({PHASES[week-1]}). "
        f"Targets are % of cycling FTP (280 W at plan creation). {intensity} "
        "Maintain a rest day Monday. Stop any session for abnormal breathlessness, "
        "chest symptoms, dizziness or weakness. Do not perform hard workouts until "
        "evaluated and cleared after repeated severe swimming-related respiratory events."
    )
    return Draft(day, week, weekday, title, description, minutes, structure)


def plan() -> list[list[Draft]]:
    result: list[list[Draft]] = []
    for i, durations in enumerate(WEEK_DURATIONS):
        week_no = i + 1
        first = START_DATE + timedelta(days=i * 7)
        result.append([
            _workout(first + timedelta(days=j + 1), week_no, j, minutes)
            for j, minutes in enumerate(durations)
        ])
    assert result[-1][-1].date == RACE_DATE
    return result


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _layout(body: str, title: str = "TrainingPeaks planner") -> HTMLResponse:
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{_esc(title)}</title>
<style>
:root{{color-scheme:light}}
body{{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:#f7f8fb;color:#1c2936;
max-width:1100px;margin:32px auto;padding:0 20px}}
h1{{font-size:26px}} h2{{font-size:18px;margin-top:24px}}
p{{max-width:90ch}} .card{{background:white;border:1px solid #dde3ea;border-radius:12px;padding:18px;margin:16px 0}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:8px;border-bottom:1px solid #e7edf1;text-align:left;vertical-align:top}}
th{{background:#f3f6f9}}
button{{border:0;border-radius:7px;padding:10px 16px;background:#185abb;color:white;font:inherit;cursor:pointer}}
button:disabled{{background:#aaa}} input[type=password]{{padding:10px;border:1px solid #bbc7d4;border-radius:7px}}
.warning{{border-left:4px solid #b66a18;padding:12px;background:#fff6e7}}
.status{{color:#245b31}}.muted{{color:#647384}}.error{{color:#a12828}}.form-row{{margin:10px 0}}
small{{font-size:12px}}
</style></head><body>{body}</body></html>"""
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


class PlannerPortal:
    def __init__(self, *, owner_password: str, signing_key: str, public_url: str):
        self.public_url = public_url.rstrip("/")
        self.sealer = Sealer(signing_key)
        self.password_digest = hashlib.sha256(owner_password.encode()).digest()
        self.failures: deque[float] = deque()

    def routes(self) -> list[Route]:
        return [
            Route("/planner", self.index, methods=["GET"]),
            Route("/planner/login", self.login, methods=["POST"]),
            Route("/planner/publish", self.publish, methods=["POST"]),
            Route("/planner/logout", self.logout, methods=["POST"]),
        ]

    def _session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(COOKIE_NAME, "")
        claims = self.sealer.open("planner-session", token) if token else None
        if claims and claims.get("sub") == "owner" and isinstance(claims.get("csrf"), str):
            return claims
        return None

    def _origin_ok(self, request: Request) -> bool:
        return request.headers.get("origin", "") == self.public_url

    def _authorized_post(self, request: Request, csrf: Any) -> bool:
        claims = self._session(request)
        return bool(
            claims and self._origin_ok(request) and isinstance(csrf, str)
            and hmac.compare_digest(claims["csrf"], csrf)
        )

    async def index(self, request: Request) -> HTMLResponse:
        session = self._session(request)
        if not session:
            return _layout(
                '<h1>TrainingPeaks — 12-week Low Gap plan</h1>'
                '<p>This private page uses the <b>MCP_OWNER_PASSWORD</b> from your Render Environment.'
                ' It does not require a TrainingPeaks cookie or a new ChatGPT connection.</p>'
                '<form method="post" action="/planner/login" class="card">'
                '<label>Owner password <input type="password" name="password" '
                'autocomplete="current-password" required></label> '
                '<button type="submit">Open plan preview</button></form>'
                '<p class="muted">Nothing will be published until you approve an individual week.</p>'
            )
        weeks = plan()
        ftp_warning = ""
        try:
            settings = await tp_get_athlete_settings()
            power_zones = (settings.get("settings") or {}).get("powerZones") or []
            ftp = next((z.get("threshold") for z in power_zones if z.get("workoutTypeId") == 0), None)
            if ftp != EXPECTED_FTP:
                ftp_warning = (
                    f'<p class="error">Live TrainingPeaks FTP is {_esc(ftp)}, not 280 W. '
                    'Publishing is disabled until the plan is reviewed.</p>'
                )
        except Exception:
            ftp = None
            ftp_warning = '<p class="error">Could not verify current FTP. Publishing is disabled.</p>'
        published: set[tuple[str, str]] = set()
        try:
            existing = await tp_get_workouts(START_DATE.isoformat(), RACE_DATE.isoformat(), "all")
            if existing.get("isError"):
                raise RuntimeError("Could not verify the existing calendar")
            published = {(w["date"], w["title"]) for w in existing.get("workouts", [])}
        except Exception:
            ftp_warning += '<p class="error">Could not read existing calendar. Publishing is disabled.</p>'
            ftp = None

        sections: list[str] = []
        csrf = _esc(session["csrf"])
        notice = request.query_params.get("notice", "")
        if notice and len(notice) < 200:
            sections.append(f'<p class="card status">{_esc(notice)}</p>')
        for i, drafts in enumerate(weeks):
            week_no = i + 1
            done = sum((d.date.isoformat(), d.title) in published for d in drafts)
            rows = []
            for d in drafts:
                intensity, tss = d.if_tss
                status = "✓ Published" if (d.date.isoformat(), d.title) in published else "Not published"
                power = "Race / variable" if d.structure is None else "Structured %FTP"
                rows.append(
                    f"<tr><td>{_esc(d.weekday)} {d.date.isoformat()}</td>"
                    f"<td>{_esc(d.title)}</td><td>{d.duration_min} min</td>"
                    f"<td>{power}</td><td>{'—' if tss is None else f'{tss:.0f}'}</td>"
                    f"<td>{status}</td></tr>"
                )
            disabled = " disabled" if ftp != EXPECTED_FTP or done == len(drafts) else ""
            form = (
                f'<form method="post" action="/planner/publish">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="week" value="{week_no}">'
                '<div class="form-row"><label>'
                '<input type="checkbox" name="acknowledged" value="yes" required> '
                'I understand that intensive sessions require medical clearance, '
                'and I explicitly authorize creation of this week’s workouts in TrainingPeaks.'
                '</label></div>'
                f'<button type="submit"{disabled}>Publish week {week_no} '
                f'({len(drafts) - done} remaining)</button></form>'
            )
            sections.append(
                '<section class="card">'
                f'<h2>Week {week_no}: {_esc(PHASES[i])} · '
                f'{drafts[0].date.isoformat()} – {drafts[-1].date.isoformat()}</h2>'
                '<p class="muted">Monday off. Each weekday follows your established cadence.</p>'
                '<table><thead><tr><th>Day</th><th>Workout</th><th>Time</th>'
                '<th>Power</th><th>Est. TSS</th><th>Status</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>{form}</section>'
            )
        logout = (
            f'<form method="post" action="/planner/logout">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<button type="submit">Sign out</button></form>'
        )
        return _layout(
            '<h1>12-week Low Gap training calendar</h1>'
            '<p><b>November 9, 2026 – January 31, 2027.</b> '
            '59-mile Low Gap long course; 280 W FTP. '
            'Monday OFF; Tuesday/Thursday targeted power, Wednesday endurance/form, '
            'Friday easy, Saturday and Sunday longer endurance with climbing work.</p>'
            '<p class="warning"><b>Medical safety:</b> You have reported progressive, severe '
            'breathing difficulty during swimming, including symptoms persisting into cycling. '
            'This calendar is a provisional training plan, not medical clearance. '
            'Do not perform maximal or high-intensity sessions before in-person cardiopulmonary evaluation '
            'and clearance. If not cleared, substitute gentle aerobic activity or rest as advised.</p>'
            '<p>No automatic posting: review each week, then press its Publish button. '
            'Each week has up to six cycling workouts; Monday remains empty. '
            'Swim rehabilitation and strength sessions are not published by this tool. '
            'Estimated planned TSS is based on the supplied interval structure; '
            'the actual TSS will vary. The race is intentionally not an ERG workout.</p>'
            f'{ftp_warning}{"".join(sections)}{logout}'
        )

    async def login(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return _layout('<h1>Invalid request origin</h1>')
        now = time.monotonic()
        while self.failures and now - self.failures[0] >= AUTH_FAILURE_WINDOW:
            self.failures.popleft()
        if len(self.failures) >= AUTH_FAILURE_LIMIT:
            return HTMLResponse("Too many login attempts. Try again later.", status_code=429)
        form = await request.form()
        password = str(form.get("password", ""))
        test_digest = hashlib.sha256(password.encode()).digest()
        if not hmac.compare_digest(self.password_digest, test_digest):
            self.failures.append(now)
            page = _layout('<h1>Incorrect owner password</h1><p><a href="/planner">Try again</a></p>')
            page.status_code = 401
            return page
        self.failures.clear()
        session = self.sealer.seal(
            "planner-session", {"sub": "owner", "csrf": secrets.token_urlsafe(24)},
            SESSION_SECONDS
        )
        response = RedirectResponse("/planner", status_code=303)
        response.set_cookie(
            COOKIE_NAME, session, max_age=SESSION_SECONDS, secure=True,
            httponly=True, samesite="strict", path="/planner"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def logout(self, request: Request) -> Response:
        form = await request.form()
        if not self._authorized_post(request, form.get("csrf")):
            return HTMLResponse("Forbidden", status_code=403)
        response = RedirectResponse("/planner", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/planner")
        return response

    async def publish(self, request: Request) -> Response:
        form = await request.form()
        if not self._authorized_post(request, form.get("csrf")):
            return HTMLResponse("Forbidden", status_code=403)
        if form.get("acknowledged") != "yes":
            return HTMLResponse("Please explicitly approve the plan before posting.", status_code=400)
        try:
            index = int(str(form.get("week", ""))) - 1
        except ValueError:
            return HTMLResponse("Invalid week", status_code=400)
        if not (0 <= index < len(WEEK_DURATIONS)):
            return HTMLResponse("Invalid week", status_code=400)
        # Never permit publication using outdated intensity assumptions.
        settings = await tp_get_athlete_settings()
        zones = (settings.get("settings") or {}).get("powerZones") or []
        ftp = next((z.get("threshold") for z in zones if z.get("workoutTypeId") == 0), None)
        if ftp != EXPECTED_FTP:
            return HTMLResponse("FTP changed; review the plan before publishing.", status_code=409)
        drafts = plan()[index]
        async with PUBLISH_LOCK:
            existing = await tp_get_workouts(drafts[0].date.isoformat(), drafts[-1].date.isoformat(), "all")
            if existing.get("isError"):
                return HTMLResponse("Cannot verify the calendar; nothing was published.", status_code=503)
            workouts = existing.get("workouts", [])
            completed = 0
            skipped = 0
            failures: list[str] = []
            for d in drafts:
                same_day = [w for w in workouts if w.get("date") == d.date.isoformat()
                            and w.get("sport") in {"Bike", "MtnBike", "Race"}]
                if any(w.get("title") == d.title for w in same_day):
                    skipped += 1
                    continue
                if same_day:
                    skipped += 1
                    failures.append(f"{d.date}: an existing bike workout needs manual review")
                    continue
                args: dict[str, Any] = {
                    "date_str": d.date.isoformat(), "sport": "Bike", "title": d.title,
                    "description": d.description, "duration_minutes": d.duration_min,
                }
                if d.structure is not None:
                    args["structure"] = d.structure
                if d.distance_km is not None:
                    args["distance_km"] = d.distance_km
                try:
                    result = await tp_create_workout(**args)
                except Exception:
                    log.exception("Workout creation failed for %s", d.date)
                    failures.append(f"{d.date}: publisher error")
                    break
                if result.get("isError") or not result.get("success"):
                    failures.append(f"{d.date}: API rejected workout creation")
                    break
                completed += 1
            log.info("Planner week %d: created=%d skipped=%d errors=%d", index + 1,
                     completed, skipped, len(failures))
        message = f"Week {index + 1}: created {completed}; skipped {skipped}."
        if failures:
            message += " Review: " + "; ".join(failures[:3])
        return RedirectResponse("/planner?" + urlencode({"notice": message}), status_code=303)
