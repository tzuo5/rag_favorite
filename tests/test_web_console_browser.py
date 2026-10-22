"""Opt-in browser acceptance with intercepted synthetic APIs only."""

import base64
import io
import json
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import Error as PlaywrightError

pytestmark = pytest.mark.skipif(
    os.environ.get("RAG_BROWSER_TEST") != "1",
    reason="Opt-in isolated UI browser checks",
)
ENGINES = os.environ.get("RAG_UI_TEST_ENGINES", "chromium").split(",")
ARTIFACTS = Path(os.environ.get("RAG_UI_ARTIFACTS", "/tmp/rag-ui-acceptance"))
ASSETS = Path(__file__).resolve().parents[1] / "src/rag_favorite/resources/web"
ORIGIN = "https://console.example.ts.net"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def synthetic_job(number, state="queued"):
    titles = [
        "怎样煎出外脆里嫩的牛排",
        "从零理解向量检索与知识库",
        "准备一周的工作日午餐",
        "纪录片中的城市与建筑",
        "咖啡萃取的几个关键变量",
        "用清晰的笔记建立学习系统",
    ]
    return {
        "id": str(UUID(int=number + 1)),
        "title": "演示任务 · " + titles[number % len(titles)] + " · " + str(number),
        "collection": "unified",
        "state": state,
        "display_state": state,
        "state_label": {
            "queued": "等待中",
            "running": "运行中",
            "failed": "失败",
            "blocked": "受阻",
        }.get(state, state),
        "stage": "queued",
        "stage_label": "本地准备 · 语音转写",
        "model": "ASR",
        "progress": "12 / 30 片段",
        "pipeline_stage": "prepare",
        "pipeline_status": "running" if state == "running" else "waiting",
        "attempts": 1,
        "priority": 0,
        "paused": False,
        "error_code": "SOURCE_MISSING" if state == "blocked" else None,
        "reason_code": "SOURCE_MISSING" if state == "blocked" else "UNKNOWN",
        "blocked_reason": "需要补充有效来源链接"
        if state == "blocked"
        else "未确认原因",
        "queue_rank": number + 1,
        "updated_at": "2026-10-11T18:20:00Z",
        "created_at": "2026-10-10T12:00:00Z",
        "media_expired": False,
    }


def fixture_state():
    jobs = [
        synthetic_job(i, "running" if i < 3 else "blocked" if i in (5, 9) else "queued")
        for i in range(86)
    ]
    jobs[8]["title"] = (
        "演示长标题 " + "中英文 Mixed long text % _ <script>alert(1)</script> " * 9
    )
    image = Image.new("RGB", (1280, 900), "#edf2f8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((160, 120, 1120, 780), fill="white", outline="#2454d7", width=5)
    draw.text((220, 180), "SYNTHETIC LOGIN VERIFICATION", fill="black")
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return {
        "jobs": jobs,
        "actions": [],
        "requests": [],
        "offline": False,
        "authed": True,
        "paused": False,
        "action_error": False,
        "delayed": [],
        "delay_q": None,
        "xhs": {
            "service": "ready",
            "paths": {
                "owner": {"state": "valid"},
                "note": {"state": "verification_required"},
            },
            "counters": {"note_successes": 42},
            "login": None,
        },
        "frame": {
            "attempt_id": "synthetic-attempt",
            "frame_id": "synthetic-frame",
            "width": 1280,
            "height": 900,
            "image": base64.b64encode(buf.getvalue()).decode(),
            "qr": None,
            "qr_link": "https://example.invalid/synthetic-qr",
        },
    }


def snapshot(state):
    return {
        "captured_at": "2026-10-11T18:20:00Z",
        "counts": {"running": 3, "queued": 81, "blocked": 2},
        "total": 2486,
        "control_supported": True,
        "paused_pending": 4,
        "control": {"paused": state["paused"], "paused_jobs": []},
        "worker": {"ActiveState": "active"},
        "models": [
            {
                "key": key,
                "name": name,
                "state": "处理中",
                "connection": "本地服务可达",
                "activity": "当前调用 1 个任务\n最近完成 14:08 · 成功",
                "role": role,
                "task": "演示任务",
            }
            for key, name, role in [
                ("CCR", "语言与视觉模型", "内容理解"),
                ("ASR", "Whisper Medium", "语音转写"),
                ("Embedding", "知识检索编码器", "向量索引"),
            ]
        ],
        "pipeline": {
            "available": True,
            "stages": {
                key: {
                    "running": i % 2,
                    "waiting": 18 + i,
                    "retry_wait": i,
                    "blocked": 0,
                }
                for i, key in enumerate(["download", "prepare", "llm", "publish"])
            },
            "xhs_login_required": True,
        },
        "task_summary": {
            "counts": {
                "running": 3,
                "queued": 81,
                "blocked": 2,
                "complete": 1840,
                "duplicate": 92,
                "paused": 4,
            },
            "total": 2486,
            "reasons": [
                {"code": "SOURCE_MISSING", "label": "需要补充有效来源链接", "count": 2}
            ],
            "collections": ["unified"],
        },
        "call_metrics": {
            "attempts": 120,
            "http_success": 119,
            "valid_outputs": 117,
            "unknown_attempts": 2,
            "searchable_completed": 1836,
        },
        "model_calls": [
            {
                "model": "CCR",
                "operation": "summary",
                "job_id": state["jobs"][0]["id"],
                "status": "success",
                "http_status": 200,
                "stream_complete": True,
                "valid_output": True,
                "duration_seconds": 14.8,
            }
        ],
        "logs": [
            "14:08:18 演示任务完成准备，等待模型处理",
            "14:08:22 发布完成，索引可检索",
            "长日志 " + "abcdefgh0123456789" * 40,
        ],
    }


def list_response(state, query):
    jobs = list(state["jobs"])
    q = query.get("q", [""])[0].lower()
    status = query.get("state", [""])[0]
    reason = query.get("reason", [""])[0]
    jobs = [
        job
        for job in jobs
        if (
            not q
            or q in " ".join([job["title"], job["id"], job["error_code"] or ""]).lower()
        )
        and (
            not status
            or job["display_state"] == status
            or (status == "attention" and job["state"] in ("failed", "blocked"))
        )
        and (not reason or job["reason_code"] == reason)
    ]
    size = int(query.get("page_size", [40])[0])
    page = min(int(query.get("page", [1])[0]), max(1, (len(jobs) + size - 1) // size))
    return {
        "items": jobs[(page - 1) * size : page * size],
        "total": len(jobs),
        "page": page,
        "page_size": size,
        "captured_at": "2026-10-11T18:20:00Z",
    }


@pytest.fixture(scope="module", params=ENGINES)
def browser(request):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        options = {"headless": True}
        if request.param == "chromium":
            options["executable_path"] = shutil.which("google-chrome")
        elif request.param == "webkit" and os.environ.get("RAG_UI_WEBKIT_EXECUTABLE"):
            options["executable_path"] = os.environ["RAG_UI_WEBKIT_EXECUTABLE"]
        engine = getattr(p, request.param)
        instance = engine.launch(**options)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        versions_file = ARTIFACTS / "browser-versions.json"
        versions = (
            json.loads(versions_file.read_text()) if versions_file.exists() else {}
        )
        versions[request.param] = instance.version
        versions_file.write_text(json.dumps(versions, indent=2))
        yield instance, request.param
        instance.close()


@pytest.fixture
def ui(browser, request):
    instance, engine = browser
    width = getattr(request.node, "callspec", None)
    mobile = width is None or width.params.get("width", 402) < 1024
    context = instance.new_context(
        viewport={"width": 402, "height": 874}, is_mobile=mobile, has_touch=mobile
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = fixture_state()

    def intercept(route):
        request = route.request
        # WebKit routes local blob loads through interception; these URLs contain
        # only the synthetic image bytes created in this same document.
        if request.url.startswith("blob:" + ORIGIN + "/"):
            return route.continue_()
        parsed = urlsplit(request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        state["requests"].append(path)
        if path == "/":
            return route.fulfill(
                content_type="text/html; charset=utf-8",
                body=(ASSETS / "index.html").read_text(),
                headers={"Content-Security-Policy": CSP},
            )
        if path.startswith("/assets/"):
            filename = path.rsplit("/", 1)[-1]
            return route.fulfill(
                content_type="text/css"
                if filename.endswith(".css")
                else "application/javascript",
                body=(ASSETS / filename).read_text(),
            )
        if path == "/api/login":
            state["authed"] = True
            return route.fulfill(json={"csrf": "synthetic-token"})
        if not state["authed"]:
            return route.fulfill(status=401, json={"detail": "请重新登录。"})
        if state["offline"] and path.startswith("/api/"):
            if path == "/api/snapshot":
                return route.fulfill(
                    status=503,
                    json={
                        "snapshot": snapshot(state),
                        "stale": True,
                        "error": "fixture offline",
                    },
                )
            return route.fulfill(status=503, json={"detail": "合成连接中断"})
        if path == "/api/session":
            data = {"csrf": "synthetic-token"}
        elif path == "/api/snapshot":
            data = {"snapshot": snapshot(state), "stale": False}
        elif path == "/api/jobs/updates":
            ids = query.get("ids", [""])[0].split(",")
            rows = [job for job in state["jobs"] if job["id"] in ids]
            data = {
                "items": rows,
                "missing_ids": [i for i in ids if i not in {j["id"] for j in rows}],
                "captured_at": "now",
            }
        elif path == "/api/jobs":
            if query.get("q", [""])[0] == state["delay_q"]:
                state["delayed"].append(route)
                return None
            data = list_response(state, query)
        elif path.startswith("/api/jobs/"):
            item = next(
                (j for j in state["jobs"] if j["id"] == path.rsplit("/", 1)[-1]), None
            )
            if item is None:
                return route.fulfill(
                    status=404, json={"detail": "任务不存在或已不在当前知识库中。"}
                )
            data = {"item": item}
        elif path == "/api/xhs/session":
            data = state["xhs"]
        elif path == "/api/xhs/frame":
            data = state["frame"]
        elif path == "/api/action":
            action = request.post_data_json
            assert request.headers["x-csrf-token"] == "synthetic-token"
            state["actions"].append(action)
            if state["action_error"]:
                return route.fulfill(
                    status=503, json={"detail": "合成操作失败，请重试。"}
                )
            if action["action"] in ("stop", "start"):
                state["paused"] = action["action"] == "stop"
            data = {"message": "操作已完成（合成测试）"}
        elif path == "/api/xhs/action":
            action = request.post_data_json
            assert request.headers["x-csrf-token"] == "synthetic-token"
            state["actions"].append(action)
            if action["action"] == "login_start":
                state["xhs"]["login"] = {"id": "synthetic-attempt", "state": "waiting"}
            if action["action"] in {"network_pause", "network_resume"}:
                state["xhs"]["network"]["paused"] = action["action"] == "network_pause"
            data = state["xhs"]
        elif path == "/api/logout":
            state["authed"] = False
            data = {"message": "已退出"}
        else:
            return route.abort()
        return route.fulfill(content_type="application/json", body=json.dumps(data))

    context.route("**/*", intercept)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    yield page, state, errors, engine
    context.close()


def opened(ui, fragment=""):
    from playwright.sync_api import expect

    page, state, errors, engine = ui
    page.goto(ORIGIN + "/" + fragment)
    expect(page.locator("#connection-status")).to_have_text("已连接")
    return page, state, errors, engine


@pytest.mark.parametrize(
    "width,height", [(402, 874), (874, 402), (375, 812), (1440, 800), (1280, 720)]
)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_responsive_views_and_detail(ui, width, height, theme):
    from playwright.sync_api import expect

    page, _, errors, engine = ui
    page.set_viewport_size({"width": width, "height": height})
    opened(ui)
    page.evaluate("theme => applyTheme(theme)", theme)
    for view in ["overview", "tasks", "runtime", "account"]:
        page.locator(f"[data-view='{view}']").click()
        expect(page.locator("#view-" + view)).to_be_visible()
        if view == "tasks":
            expect(page.locator(".job")).to_have_count(40)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
        if (width, height) in [(402, 874), (1440, 800)]:
            page.locator("#view-eyebrow").evaluate(
                "el => el.textContent = '演示数据 / SYNTHETIC PREVIEW'"
            )
            page.screenshot(
                path=str(ARTIFACTS / f"{engine}-{width}-{theme}-{view}.png"),
                full_page=view != "tasks",
            )
    page.locator("[data-view='tasks']").click()
    if width >= 1024:
        page.locator(".job-open").first.click()
        expect(page.locator("#detail-stop")).to_be_in_viewport(ratio=1)
        page.locator("#detail-close").click()
        expect(page.locator("#detail-content")).not_to_be_visible()
    page.locator(".job-open").nth(8).click()
    expect(page.locator("#detail-content")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if (width, height) in [(402, 874), (1440, 800)]:
        page.screenshot(path=str(ARTIFACTS / f"{engine}-{width}-{theme}-detail.png"))
    if width < 1024:
        expect(page.locator("#detail-pane")).to_have_attribute("aria-modal", "true")
        assert page.locator("#detail-pane").bounding_box()["height"] <= height + 1
        page.locator("#detail-expand").click()
        expect(page.locator("#detail-expand")).to_have_text("收起")
        page.go_back()
        expect(page.locator("#detail-content")).not_to_be_visible()
        expect(page.locator(".job-open").nth(8)).to_be_focused()
    else:
        expect(page.locator("#detail-pane")).not_to_have_attribute("aria-modal", "true")
        assert (
            page.locator("#detail-pane").bounding_box()["x"]
            > page.locator("#jobs").bounding_box()["x"]
        )
    assert not errors


def test_polling_preserves_focus_and_membership_until_acknowledged(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = ui
    page.set_viewport_size({"width": 1440, "height": 800})
    opened(ui, "#tasks")
    expect(page.locator(".job")).to_have_count(40)
    original = state["jobs"][0]["id"]
    button = page.locator(".job-open").first
    button.focus()
    state["jobs"][0]["title"] = "更新后的任务标题"
    state["jobs"].insert(0, synthetic_job(1000))
    page.evaluate("refresh()")
    expect(page.locator(".job").first).to_have_attribute("data-job-id", original)
    expect(button).to_be_focused()
    expect(page.locator(".job-title").first).to_have_text("更新后的任务标题")
    expect(page.locator("#list-update")).to_be_visible()
    page.locator("#list-update").click()
    expect(page.locator(".job").first).to_have_attribute(
        "data-job-id", state["jobs"][0]["id"]
    )
    assert not errors


def test_confirmations_feedback_stale_and_login_expiry(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui)
    page.locator("#stop").click()
    expect(page.locator("#confirm-dialog")).to_be_visible()
    assert not state["actions"]
    page.locator("#confirm-cancel").click()
    assert not state["actions"]
    page.locator("#stop").click()
    page.locator("#confirm-accept").click()
    expect(page.locator("#queue-title")).to_have_text("队列已暂停")
    assert len(state["actions"]) == 1
    page.locator("[data-view='tasks']").click()
    page.locator(".job-open").nth(4).click()
    page.locator("#detail-stop").click()
    expect(page.locator("#detail-feedback")).to_contain_text("操作已完成")
    assert len(state["actions"]) == 2
    page.evaluate("refresh()")
    expect(page.locator("#detail-feedback")).to_contain_text("操作已完成")
    state["offline"] = True
    page.evaluate("refresh()")
    expect(page.locator("#detail-stop")).to_be_disabled()
    expect(page.locator(".job")).to_have_count(40)
    state["offline"] = False
    page.evaluate("refresh()")
    expect(page.locator("#detail-stop")).to_be_enabled()
    state["authed"] = False
    page.evaluate("refresh()")
    expect(page.locator("#login-panel")).to_be_visible()
    expect(page.locator("#detail-title")).to_have_text("")
    page.locator("#username").fill("synthetic")
    page.locator("#password").fill("synthetic-password")
    page.locator("#login-form button").click()
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("#model-calls .call-record")).to_have_count(1)
    expect(page.locator("#detail-content")).to_be_visible()
    state["action_error"] = True
    page.locator("#detail-stop").click()
    expect(page.locator("#detail-feedback")).to_contain_text("合成操作失败")
    page.evaluate("refresh()")
    expect(page.locator("#detail-feedback")).to_contain_text("合成操作失败")
    assert not errors


def test_full_search_pagination_and_late_response(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui, "#tasks")
    expect(page.locator(".job")).to_have_count(40)
    page.locator("#next").click()
    expect(page.locator("#page-label")).to_have_text("2 / 3")
    page.evaluate("refresh()")
    expect(page.locator("#page-label")).to_have_text("2 / 3")
    state["delay_q"] = "slow"
    page.locator("#search").fill("slow")
    page.wait_for_timeout(350)
    page.locator("#search").fill("· 85")
    expect(page.locator(".job")).to_have_count(1)
    expect(page.locator(".job-title")).to_contain_text("· 85")
    for route in state["delayed"]:
        try:
            route.fulfill(
                json={
                    "items": [],
                    "total": 0,
                    "page": 1,
                    "page_size": 40,
                    "captured_at": "old",
                }
            )
        except PlaywrightError as error:
            assert (
                "closed" in str(error)
                or "Invalid" in str(error)
                or "aborted" in str(error)
            )
    expect(page.locator(".job")).to_have_count(1)
    state["offline"] = True
    page.locator("#search").fill("new unavailable query")
    expect(page.locator(".job")).to_have_count(0)
    expect(page.locator("#jobs-status")).to_contain_text("没有可确认")
    assert not errors


def test_xhs_frame_mapping_zoom_otp_and_link(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui, "#account")
    page.locator("#xhs-login").click()
    expect(page.locator("#xhs-frame")).to_be_visible()
    expect(page.locator("#xhs-qr-link")).to_have_attribute(
        "href", "https://example.invalid/synthetic-qr"
    )
    for expanded in [False, True]:
        if expanded:
            page.locator("#xhs-frame-expand").click()
        expect(page.locator("#xhs-frame")).to_have_js_property("complete", True)
        expect(page.locator("#xhs-frame")).to_have_js_property("naturalWidth", 1280)
        page.locator("#xhs-frame").scroll_into_view_if_needed()
        box = page.locator("#xhs-frame").bounding_box()
        page.mouse.move(
            box["x"] + box["width"] * 0.12, box["y"] + min(box["height"] * 0.12, 40)
        )
        page.mouse.down()
        frame_reads = state["requests"].count("/api/xhs/frame")
        page.evaluate("document.dispatchEvent(new Event('console-refresh'))")
        assert state["requests"].count("/api/xhs/frame") == frame_reads
        page.mouse.move(
            box["x"] + box["width"] * 0.20,
            box["y"] + min(box["height"] * 0.12, 40),
            steps=3,
        )
        page.mouse.up()
        page.wait_for_timeout(1400)
    pointers = [a for a in state["actions"] if a["action"] == "pointer"]
    assert len(pointers) == 2
    for action in pointers:
        assert abs(action["points"][0][0] - 154) <= 1
        assert abs(action["points"][-1][0] - 256) <= 1
    assert all(
        0 <= point[0] < 1280 and 0 <= point[1] < 900
        for action in pointers
        for point in action["points"]
    )
    page.locator("#xhs-otp").fill("123456")
    page.locator("#xhs-otp-form button").click()
    page.wait_for_timeout(50)
    assert (
        state["actions"][-1]["action"] == "otp"
        and state["actions"][-1]["frame_id"] == "synthetic-frame"
    )
    expect(page.locator("#xhs-otp")).to_have_value("")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_detail_reload_close_scroll_and_reopen(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui, "#tasks")
    button = page.locator(".job-open").nth(20)
    button.scroll_into_view_if_needed()
    old_y = page.evaluate("scrollY")
    button.click()
    page.locator("#detail-expand").click()
    page.locator("#detail-close").click()
    expect(button).to_be_focused()
    assert abs(page.evaluate("scrollY") - old_y) < 2
    button.click()
    expect(page.locator("#detail-expand")).to_have_text("展开")
    page.reload()
    expect(page.locator("#detail-title")).to_have_text(state["jobs"][20]["title"])
    page.locator("#detail-close").click()
    expect(page.locator("#detail-content")).not_to_be_visible()
    page.goto(ORIGIN + "/#tasks?page=1&job=" + state["jobs"][1]["id"])
    expect(page.locator("#detail-title")).to_have_text(state["jobs"][1]["title"])
    page.locator("#detail-close").click()
    expect(page.locator("#view-tasks")).to_be_visible()
    expect(page.locator("#detail-content")).not_to_be_visible()
    assert not errors


def test_hidden_polling_and_synthetic_keyboard_viewport(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui, "#tasks")
    expect(page.locator(".job")).to_have_count(40)
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    requests = len(state["requests"])
    state["paused"] = True
    page.wait_for_timeout(5200)
    assert len(state["requests"]) == requests
    page.evaluate("""() => {
        delete document.hidden;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    expect(page.locator("#pause-banner")).to_be_visible()
    page.locator("#search").focus()
    page.evaluate("""() => {
        Object.defineProperty(visualViewport, 'height', {configurable: true, get: () => 400});
        visualViewport.dispatchEvent(new Event('resize'));
    }""")
    expect(page.locator("#navigation")).not_to_be_visible()
    assert page.locator("#search").evaluate("el => document.activeElement === el")
    page.evaluate("""() => {
        delete visualViewport.height;
        visualViewport.dispatchEvent(new Event('resize'));
    }""")
    expect(page.locator("#navigation")).to_be_visible()
    assert not errors


def test_reason_filter_empty_result_and_long_error(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = opened(ui)
    page.locator(".exception").first.click()
    expect(page.locator(".job")).to_have_count(2)
    expect(page.locator("#state-filter")).to_have_value("attention")
    expect(page.locator("#reason-filter")).to_have_value("SOURCE_MISSING")
    page.locator("#search").fill("no-matching-task")
    expect(page.locator(".empty-state")).to_contain_text("没有符合条件")
    job = state["jobs"][5]
    job["error_code"] = "LONG_ERROR_" + "x" * 1800
    job["reason_code"] = job["error_code"]
    job["blocked_reason"] = "需要人工处理 " + "长错误说明" * 100
    page.locator("#reason-filter").select_option("")
    page.locator("#search").fill(job["id"])
    expect(page.locator(".job")).to_have_count(1)
    page.locator(".job-open").click()
    expect(page.locator("#detail-fields")).to_contain_text(job["error_code"])
    overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
        .filter(el => el.getBoundingClientRect().right > innerWidth + 1 || el.scrollWidth > el.clientWidth + 1)
        .map(el => ({id: el.id, cls: el.className, tag: el.tagName, width: el.getBoundingClientRect().width, sw: el.scrollWidth, cw: el.clientWidth,
                    text: el.textContent.slice(0, 50)})).slice(-15)""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), {
        "overflow": json.dumps(overflow, ensure_ascii=False),
        "viewport": page.evaluate(
            "[innerWidth, document.documentElement.scrollWidth, document.body.scrollWidth, scrollX, visualViewport.width, visualViewport.scale]"
        ),
    }
    assert page.locator("#detail-scroll").evaluate(
        "el => el.scrollWidth <= el.clientWidth"
    )
    assert not errors


def test_theme_reduced_motion_zoom_layout_and_keyboard_targets(ui):
    from playwright.sync_api import expect

    page, _, errors, _ = opened(ui, "#account")
    page.locator("#theme").select_option("dark")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 720, "height": 400})
    page.locator("[data-view='tasks']").click()
    page.locator("#search").focus()
    assert (
        page.locator("#search").evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)"
        )
        >= 16
    )
    assert page.locator("[data-view='tasks']").bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_node_counter_pause_requires_verified_recovery(ui):
    from playwright.sync_api import expect

    page, state, errors, _ = ui
    state["xhs"]["network"] = {
        "node": 2,
        "nodes": 99,
        "completed": 79,
        "batch_size": 80,
        "total_completed": 159,
        "paused": False,
        "reason": "batch_complete",
    }
    opened(ui, "#account")
    expect(page.locator("#xhs-network")).to_contain_text("79/80")
    page.locator("#xhs-network-pause").click()
    expect(page.locator("#xhs-network-resume")).to_be_hidden()
    assert state["actions"][-1]["action"] == "network_pause"
    state["xhs"]["recovery"] = {"phase": "validated"}
    page.reload()
    expect(page.locator("#xhs-acceptance")).to_be_visible()
    expect(page.locator("#xhs-network-resume")).to_be_hidden()
    assert not errors
