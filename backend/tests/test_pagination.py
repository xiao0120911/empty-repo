"""分页回归：请求决定页身份，模型可省略逐页哈希但必须保留范围与全记录统计。
这些测试同时核对完整加密证据、观察预算、跨页漂移与中文成果，不调用生产写接口。
"""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ValidationError

from app.agent import Engine, compact_evidence, initial_state
from app.catalog import TOOLS
from app.contracts import ScanQuery
from app.gateway import Gateway
from app.main import create_app
from app.pagination import observation, scan_pages
from app.store import Store, dumps
from app.upstream import UpstreamClient, UpstreamError
from test_system import login, settings, setup_users, wait, warning_record


SCAN_TOOLS = ("shop_scan", "camera_scan", "warning_scan")
LIST_TOOLS = {
    "shop_scan": "shop_list",
    "camera_scan": "camera_list",
    "warning_scan": "warning_list",
}
LIST_PATHS = {
    "shop_scan": "/api/basicData/shop/list",
    "camera_scan": "/api/facility/camera/management/page",
    "warning_scan": "/api/earlyWarn/earlyWarning/event/list",
}
GROUP_FIELDS = {
    "shop_scan": ("shopTypeName",),
    "camera_scan": ("equipmentName", "status"),
    "warning_scan": ("warnTypeMsg", "statusName"),
}


def make_records(tool, count, extra_length=0, core_length=0):
    records = []
    for index in range(count):
        identifier = str(9300000000000000000 + index)
        name = f"Business {index:04d}" + "n" * core_length
        if tool in ("shop_scan", "shop_list"):
            row = {
                "id": identifier,
                "shopName": name,
                "shopTypeName": "School" if index % 2 == 0 else "Restaurant",
                "businessSubTypeDesc": "Catering" + "b" * core_length,
                "shopAddress": f"Test address {index}" + "a" * core_length,
                "creditScore": 50 + index % 51,
            }
        elif tool in ("camera_scan", "camera_list"):
            row = {
                "id": identifier,
                "deviceId": identifier,
                "shopId": "9000000000000000001",
                "shopName": name,
                "equipmentNumber": f"DEVICE-{index:04d}",
                "cameraNumber": f"CAMERA-{index:04d}",
                "equipmentType": 1,
                "equipmentName": "Prep" if index % 2 == 0 else "Cooking",
                "status": index % 2,
                "live": index % 2,
                "orgName": "Test organization",
                "platformType": "yushi",
            }
        else:
            row = warning_record(identifier)
            row.update(
                organizationName=name,
                warnTypeMsg="Type A" if index % 2 == 0 else "Type B",
                status=1 if index % 2 == 0 else 3,
                statusName="Pending" if index % 2 == 0 else "Processed",
                subTypeMsg="Subtype" + "s" * core_length,
            )
        if extra_length:
            row["opaqueExtra"] = f"RAW-ONLY-{index:04d}:" + "x" * extra_length
            row["extraStructure"] = {"note": f"Nested raw field {index}", "values": [index]}
        records.append(row)
    return records


def page_response(records, total, **metadata):
    return {"code": 0, "msg": "Success", "data": {"total": total, "list": records, **metadata}}


def assert_original_fields(actual, original):
    assert len(actual) == len(original)
    for saved, source in zip(actual, original):
        for key, value in source.items():
            expected = str(value) if isinstance(value, int) and (key == "id" or key.endswith("Id")) else value
            assert saved[key] == expected
        assert isinstance(saved["id"], str)


def assert_statistics(tool, statistics, records):
    assert statistics["recordCount"] == len(records)
    for field in GROUP_FIELDS[tool]:
        expected = Counter(str(record[field]) for record in records)
        group = statistics["groups"][field]
        assert group["distinctGroups"] == len(expected)
        assert sum(group["top"].values()) + group["otherRecords"] == len(records)
        if len(expected) <= 20:
            assert group["top"] == dict(expected)
            assert group["otherRecords"] == 0
    if tool == "shop_scan":
        scores = [record["creditScore"] for record in records]
        credit = statistics["creditScore"]
        assert credit["knownCount"] == len(scores)
        assert credit["missingCount"] == 0
        assert credit["minimum"] == min(scores)
        assert credit["maximum"] == max(scores)
        assert credit["average"] == round(sum(scores) / len(scores), 2)


def assert_no_raw_truncation(value):
    if isinstance(value, dict):
        assert value.get("truncated") is not True
        for child in value.values():
            assert_no_raw_truncation(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_raw_truncation(child)


def assert_model_view(content, records, total):
    assert isinstance(content, str)
    assert len(content) <= 24000
    parsed = json.loads(content)
    assert_no_raw_truncation(parsed)
    assert parsed["code"] == 0
    data = parsed["data"]
    assert data["total"] == total
    preview = data["list"]
    assert preview
    assert [record["id"] for record in preview] == [record["id"] for record in records[:len(preview)]]
    assert all(isinstance(record["id"], str) for record in preview)
    assert all("opaqueExtra" not in record and "extraStructure" not in record for record in preview)
    assert "RAW-ONLY-" not in content
    view = data["modelView"]
    assert view["rawEvidencePreserved"] is True
    assert view["sourceRecords"] == len(records)
    assert view["visibleRecords"] == len(preview)
    assert view["omittedRecords"] == len(records) - len(preview)
    assert view["allCoreRecordsVisible"] is (len(preview) == len(records))
    assert data["statistics"]["recordCount"] == len(records)
    if len(preview) < len(records):
        assert view["omittedRecords"] > 0
        assert view["allCoreRecordsVisible"] is False
    else:
        assert [record["id"] for record in preview] == [record["id"] for record in records]
    return data


@asynccontextmanager
async def engine_setup(tmp_path, transport):
    config = settings(tmp_path)
    config.max_iterations = 2
    await setup_users(config)
    store = Store(config)
    await store.open()
    upstream = UpstreamClient(config, transport)
    try:
        await store.session_create("operator", "pagination-token")
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "pagination-checkpoints.sqlite")) as saver:
            await saver.setup()
            yield Engine(config, store, Gateway(store, upstream), saver), store
    finally:
        await upstream.aclose()
        await store.close()


class ScriptedModel:
    def __init__(self, tool, arguments, check_observation):
        self.tool = tool
        self.arguments = arguments
        self.check_observation = check_observation
        self.rounds = []

    async def ainvoke(self, messages):
        self.rounds.append(list(messages))
        assert len(self.rounds) <= 2, "Pagination must not consume an extra model round per page"
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if len(self.rounds) == 1:
            assert not tool_messages
            return AIMessage(content="Read the requested records.", tool_calls=[{
                "name": self.tool,
                "args": self.arguments,
                "id": "pagination-read",
                "type": "tool_call",
            }])
        assert len(tool_messages) == 1
        message = tool_messages[0]
        assert message.name == self.tool
        assert message.tool_call_id == "pagination-read"
        self.check_observation(message.content)
        assert "RAW-ONLY-" not in "\n".join(str(message.content) for message in messages)
        return AIMessage(content="The read completed; the evidence retains the reported scope.")


@pytest.mark.parametrize("tool", SCAN_TOOLS)
def test_scan_contract_defaults_and_existing_read_routes(tool):
    spec = TOOLS[tool]
    request = spec.input_model.model_validate({})
    assert isinstance(request, ScanQuery)
    assert request.keyword is None
    assert request.pageSize == 100
    assert request.maxPages == 100
    assert spec.input_model(pageSize=100, maxPages=200, keyword="Catering").maxPages == 200
    assert spec.risk == "read"
    assert spec.method == "POST"
    assert spec.path == TOOLS[LIST_TOOLS[tool]].path == LIST_PATHS[tool]


@pytest.mark.parametrize("arguments", [
    {"pageSize": 0},
    {"pageSize": 101},
    {"maxPages": 0},
    {"maxPages": 201},
    {"pageSize": True},
    {"maxPages": 1.5},
    {"keyword": "x" * 201},
    {"keyword": 123},
    {"pageNum": 1},
    {"unsafe": "unexpected"},
], ids=["zero-page-size", "oversized-page", "zero-pages", "too-many-pages", "boolean-size", "fractional-pages", "long-keyword", "numeric-keyword", "manual-page", "extra-field"])
def test_scan_query_rejects_invalid_arguments(arguments):
    with pytest.raises(ValidationError):
        ScanQuery.model_validate(arguments)


@pytest.mark.parametrize("core_length", [0, 1000], ids=["all-identifiers-fit", "explicitly-omitted-identifiers"])
def test_observation_long_shop_page_is_valid_bounded_and_non_mutating(core_length):
    records = make_records("shop_list", 100, extra_length=6000, core_length=core_length)
    original = page_response(records, 5000, pageNum=3, pageSize=100, returned=100)
    snapshot = deepcopy(original)
    assert len(dumps(original)) > 24000
    arguments = {"pageNum": 3, "pageSize": 100}
    arguments_snapshot = deepcopy(arguments)
    content = dumps(observation("shop_list", arguments, original))
    data = assert_model_view(content, records, 5000)
    assert data["pageNum"] == 3
    assert data["pageSize"] == 100
    assert data["returned"] == 100
    coverage = data["coverage"]
    assert coverage["kind"] == "page"
    assert coverage["pageNum"] == 3
    assert coverage["pageSize"] == 100
    assert coverage["returned"] == 100
    assert coverage["total"] == 5000
    assert coverage["complete"] is False
    assert coverage["nextPage"] == 4
    assert original == snapshot
    assert arguments == arguments_snapshot
    if core_length == 0:
        assert data["modelView"]["allCoreRecordsVisible"] is True
    else:
        assert data["modelView"]["omittedRecords"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
@pytest.mark.parametrize("total", [300, 350], ids=["three-pages", "four-pages"])
async def test_scan_retains_all_records_and_statistics_not_model_preview(tmp_path, tool, total):
    records = make_records(tool, total, extra_length=800, core_length=1000)
    snapshot = deepcopy(records)
    calls = []
    audits = []

    async def respond(request):
        assert request.method == "POST"
        assert request.url.path == LIST_PATHS[tool]
        assert request.headers["authorization"] == "pagination-token"
        arguments = json.loads(request.content)
        calls.append(arguments)
        assert set(arguments) == {"pageNum", "pageSize", "keyword"}
        assert arguments["pageSize"] == 100
        assert arguments["keyword"] == "Catering"
        page = arguments["pageNum"]
        rows = deepcopy(records[(page - 1) * 100:page * 100])
        for row in rows:
            row["id"] = int(row["id"])
        return httpx.Response(200, json=page_response(rows, total))

    async def audit(payload):
        audits.append(payload)

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        result = await upstream.call(TOOLS[tool], {"keyword": "Catering"}, "pagination-token", audit)
    finally:
        await upstream.aclose()
    data = result["data"]
    expected_pages = (total + 99) // 100
    assert [call["pageNum"] for call in calls] == list(range(1, expected_pages + 1))
    assert len(audits) == expected_pages
    assert all(len(audit["ids"]) == audit["returned"] and audit["evidence_hash"] for audit in audits)
    assert data["complete"] is True
    assert data["outcome"] == "complete"
    assert data["total"] == data["recordsScanned"] == total
    assert data["pagesScanned"] == expected_pages
    assert data["reason"] and data["scope"]
    assert_original_fields(data["list"], records)
    assert_statistics(tool, data["statistics"], records)
    coverage = data["coverage"]
    assert coverage["kind"] == "scan"
    assert coverage["total"] == coverage["lastObservedTotal"] == total
    assert coverage["complete"] is True
    assert coverage["recordsScanned"] == total
    assert coverage["pagesScanned"] == expected_pages
    assert coverage["nextPage"] is None
    assert coverage["scope"] == data["scope"]
    assert coverage["reason"] == data["reason"]
    assert [page["pageNum"] for page in coverage["pages"]] == list(range(1, expected_pages + 1))
    raw_snapshot = deepcopy(result)
    preview = assert_model_view(dumps(observation(tool, {"keyword": "Catering"}, result)), records, total)
    assert len(preview["list"]) < total
    assert preview["coverage"] == {key: value for key, value in coverage.items() if key != "pages"}
    assert preview["modelView"]["perPageAuditInEvidence"] is True
    assert preview["statistics"] == data["statistics"]
    assert result == raw_snapshot
    assert records == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
@pytest.mark.parametrize("scenario,retained,pages,reason_fragment", [
    ("duplicate-cross-page", 2, 2, "重复"),
    ("duplicate-within-page", 2, 2, "重复"),
    ("total-drift", 2, 2, "total"),
    ("early-short-page", 3, 2, "提前"),
    ("page-error", 2, 1, "失败"),
    ("page-limit", 2, 1, "上限"),
])
async def test_scan_incomplete_preserves_only_verified_records(tmp_path, tool, scenario, retained, pages, reason_fragment):
    records = make_records(tool, 5, extra_length=100)
    calls = []
    audits = []

    async def respond(request):
        assert request.url.path == LIST_PATHS[tool]
        arguments = json.loads(request.content)
        calls.append(arguments)
        page = arguments["pageNum"]
        assert page <= 2, "An incomplete scan must stop instead of requesting more pages"
        if scenario == "page-error" and page == 2:
            return httpx.Response(503, json={"code": 503, "msg": "Page unavailable", "data": None})
        rows = records[(page - 1) * 2:page * 2]
        if page == 2 and scenario == "duplicate-cross-page":
            rows = records[:2]
        elif page == 2 and scenario == "duplicate-within-page":
            rows = [records[2], records[2]]
        elif page == 2 and scenario == "early-short-page":
            rows = records[2:3]
        total = 6 if page == 2 and scenario == "total-drift" else 5
        return httpx.Response(200, json=page_response(rows, total))

    async def audit(payload):
        audits.append(payload)

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        result = await upstream.call(TOOLS[tool], {"pageSize": 2, "maxPages": 1 if scenario == "page-limit" else 10}, "pagination-token", audit)
    finally:
        await upstream.aclose()
    data = result["data"]
    assert result["code"] == 0
    assert data["outcome"] == "incomplete"
    assert data["complete"] is False
    assert data["total"] == 5
    assert data["recordsScanned"] == retained
    assert data["pagesScanned"] == pages
    assert reason_fragment in data["reason"]
    assert data["scope"]
    assert_original_fields(data["list"], records[:retained])
    assert_statistics(tool, data["statistics"], records[:retained])
    coverage = data["coverage"]
    assert coverage["kind"] == "scan"
    assert coverage["complete"] is False
    assert coverage["total"] == 5
    assert coverage["recordsScanned"] == retained
    assert coverage["pagesScanned"] == pages
    assert coverage["nextPage"] is not None
    assert coverage["reason"] == data["reason"]
    assert len(calls) == (1 if scenario == "page-limit" else 2)
    assert len(audits) == len(calls)
    assert all("keyword" not in call and "maxPages" not in call for call in calls)
    if scenario == "total-drift":
        assert coverage["lastObservedTotal"] == 6
    if scenario == "page-error":
        assert audits[-1]["status"] == "failed" and audits[-1]["code"] == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
@pytest.mark.parametrize("first_page_fails", [True, False], ids=["unknown-total-on-failure", "known-empty-total"])
async def test_scan_first_failure_is_unknown_not_empty(tmp_path, tool, first_page_fails):
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.url.path == LIST_PATHS[tool]
        if first_page_fails:
            return httpx.Response(503, json={"code": 503, "msg": "Unavailable", "data": None})
        return httpx.Response(200, json=page_response([], 0))

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        data = (await upstream.call(TOOLS[tool], {}, "pagination-token"))["data"]
    finally:
        await upstream.aclose()
    assert len(calls) == 1
    if first_page_fails:
        assert data["total"] is None and data["coverage"]["total"] is None
        assert "失败" in data["reason"]
    else:
        assert data["total"] == data["coverage"]["total"] == 0
    assert data["coverage"]["complete"] is (not first_page_fails)
    assert data["list"] == []
    assert data["recordsScanned"] == data["statistics"]["recordCount"] == 0
    assert data["pagesScanned"] == (0 if first_page_fails else 1)
    assert data["complete"] is (not first_page_fails)
    assert data["outcome"] == ("incomplete" if first_page_fails else "complete")
    assert data["coverage"]["nextPage"] == (1 if first_page_fails else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
@pytest.mark.parametrize("failure_page", [1, 2])
@pytest.mark.parametrize("http_status", [200, 401], ids=["business-401", "http-401"])
async def test_scan_propagates_401_without_partial_success(tmp_path, tool, failure_page, http_status):
    calls = []
    records = make_records(tool, 3)

    async def respond(request):
        assert request.url.path == LIST_PATHS[tool]
        page = json.loads(request.content)["pageNum"]
        calls.append(page)
        if page == failure_page:
            return httpx.Response(http_status, json={"code": 401, "msg": "Session expired", "data": None})
        return httpx.Response(200, json=page_response(records[:2], 3))

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError) as error:
            await upstream.call(TOOLS[tool], {"pageSize": 2}, "pagination-token")
        assert error.value.code == 401
        assert error.value.http_status == 401
        assert not error.value.uncertain
    finally:
        await upstream.aclose()
    assert calls == list(range(1, failure_page + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
async def test_scan_missing_token_does_not_send_request(tmp_path, tool):
    async def respond(request):
        pytest.fail("A scan without a platform session must not make an HTTP request")

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError) as error:
            await upstream.call(TOOLS[tool], {})
        assert error.value.code == 401
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_scan_byte_budget_accepts_boundary_and_excludes_next_whole_page(tmp_path):
    records = make_records("shop_scan", 5, extra_length=1000)
    first = TOOLS["shop_list"].response_model.model_validate(page_response(records[:2], 5)).model_dump(mode="json")
    max_bytes = len(dumps(first["data"]["list"]).encode("utf-8"))
    calls = []

    async def respond(request):
        page = json.loads(request.content)["pageNum"]
        calls.append(page)
        return httpx.Response(200, json=page_response(records[(page - 1) * 2:page * 2], 5))

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        result = await scan_pages(upstream, TOOLS["shop_scan"], ScanQuery(pageSize=2), "pagination-token", max_bytes=max_bytes)
    finally:
        await upstream.aclose()
    data = result["data"]
    assert calls == [1, 2]
    assert data["outcome"] == "incomplete" and data["complete"] is False
    assert data["total"] == 5
    assert data["recordsScanned"] == 2 and data["pagesScanned"] == 2
    assert data["coverage"]["nextPage"] == 2
    assert "8MB" in data["reason"]
    assert_original_fields(data["list"], records[:2])
    assert_statistics("shop_scan", data["statistics"], records[:2])


@pytest.mark.asyncio
async def test_scan_default_eight_megabyte_budget_counts_utf8_not_characters(tmp_path):
    records = make_records("shop_scan", 8)
    for index, record in enumerate(records):
        record["opaqueExtra"] = f"RAW-ONLY-{index}:" + "\u03bb" * 800000
    calls = []

    async def respond(request):
        page = json.loads(request.content)["pageNum"]
        calls.append(page)
        rows = records[(page - 1) * 2:page * 2]
        assert len(dumps(page_response(rows, 8)).encode("utf-8")) < 4 * 1024 * 1024
        return httpx.Response(200, json=page_response(rows, 8))

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        result = await upstream.call(TOOLS["shop_scan"], {"pageSize": 2}, "pagination-token")
    finally:
        await upstream.aclose()
    data = result["data"]
    assert calls == [1, 2, 3]
    assert data["total"] == 8
    assert data["outcome"] == "incomplete" and data["complete"] is False
    assert data["recordsScanned"] == 4 and data["pagesScanned"] == 3
    assert data["coverage"]["nextPage"] == 3
    assert "8MB" in data["reason"]
    assert_original_fields(data["list"], records[:4])
    assert_statistics("shop_scan", data["statistics"], records[:4])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_first_page", [False, True], ids=["before-first-page", "after-first-page"])
async def test_scan_ninety_second_budget_stops_without_another_request(tmp_path, monkeypatch, after_first_page):
    import app.pagination as pagination

    stamps = iter([0.0, 0.0, 90.0] if after_first_page else [0.0, 90.0])
    monkeypatch.setattr(pagination, "time", SimpleNamespace(monotonic=lambda: next(stamps)))
    records = make_records("shop_scan", 5)
    calls = []

    async def respond(request):
        calls.append(json.loads(request.content)["pageNum"])
        return httpx.Response(200, json=page_response(records[:2], 5))

    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        data = (await upstream.call(TOOLS["shop_scan"], {"pageSize": 2}, "pagination-token"))["data"]
    finally:
        await upstream.aclose()
    assert calls == ([1] if after_first_page else [])
    assert data["outcome"] == "incomplete" and data["complete"] is False
    assert "90" in data["reason"]
    if after_first_page:
        assert data["total"] == 5
    else:
        assert data["total"] is None
    assert data["recordsScanned"] == (2 if after_first_page else 0)
    assert data["pagesScanned"] == (1 if after_first_page else 0)
    assert data["coverage"]["nextPage"] == (2 if after_first_page else 1)
    assert data["statistics"]["recordCount"] == data["recordsScanned"]


@pytest.mark.asyncio
async def test_engine_long_shop_list_sends_projection_and_encrypts_every_original_record(tmp_path):
    records = make_records("shop_list", 100, extra_length=12000)
    raw = page_response(records, 5000, pageNum=3, pageSize=100, returned=100)
    snapshot = deepcopy(raw)
    calls = []
    observed = []

    async def respond(request):
        assert request.url.path == LIST_PATHS["shop_scan"]
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=raw)

    def check(content):
        data = assert_model_view(content, records, 5000)
        assert data["pageNum"] == data["coverage"]["pageNum"] == 3
        assert data["pageSize"] == 100 and data["returned"] == 100
        assert data["coverage"]["returned"] == 100
        assert data["coverage"]["complete"] is False
        assert data["modelView"]["allCoreRecordsVisible"] is True
        observed.append(data)

    model = ScriptedModel("shop_list", {"pageNum": 3, "pageSize": 100}, check)
    async with engine_setup(tmp_path, httpx.MockTransport(respond)) as (engine, store):
        engine.model = model
        rid, _ = await store.create_run("operator", "Inspect page three of the available shops.", "agent", "pagination-long-page", {})
        result = await engine.invoke(initial_state(rid, "operator", "Inspect page three of the available shops.", "agent"), rid)
        assert result["status"] == "completed", result
        assert len(model.rounds) == 2 and len(observed) == 1
        assert calls == [{"pageNum": 3, "pageSize": 100}]
        operations = await store.fetch("SELECT * FROM operations WHERE run_id=?", (rid,))
        assert len(operations) == 1
        operation = operations[0]
        assert operation["status"] == "completed" and operation["tool_name"] == "shop_list"
        assert "RAW-ONLY-" not in operation["result_cipher"]
        assert records[-1]["id"] not in operation["result_cipher"]
        evidence = json.loads(store.fernet.decrypt(operation["result_cipher"].encode()))
        expected = TOOLS["shop_list"].response_model.model_validate(raw).model_dump(by_alias=True, mode="json")
        assert evidence == expected
        assert_original_fields(evidence["data"]["list"], records)
        assert evidence["data"]["list"][-1]["opaqueExtra"] == records[-1]["opaqueExtra"]
        state = await engine.graph.aget_state({"configurable": {"thread_id": rid}})
        assert state.values["iterations"] == 2
        assert len(state.values["results"]) == 1
        assert result["approvals"] == []
        assert (await store.verify())["valid"]
    assert raw == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
async def test_engine_scan_many_pages_is_one_tool_call_and_two_model_rounds(tmp_path, tool):
    total = 1100
    records = make_records(tool, total, extra_length=500, core_length=1000)
    calls = []
    observed = []

    async def respond(request):
        assert request.method == "POST" and request.url.path == LIST_PATHS[tool]
        assert request.headers["authorization"] == "pagination-token"
        arguments = json.loads(request.content)
        calls.append(arguments)
        page = arguments["pageNum"]
        return httpx.Response(200, json=page_response(records[(page - 1) * 100:page * 100], total))

    def check(content):
        data = assert_model_view(content, records, total)
        assert data["complete"] is True and data["outcome"] == "complete"
        assert data["recordsScanned"] == total and data["pagesScanned"] == 11
        assert data["coverage"]["complete"] is True
        assert data["coverage"]["nextPage"] is None
        assert len(data["list"]) < total
        assert_statistics(tool, data["statistics"], records)
        observed.append(data)

    arguments = {"pageSize": 100, "maxPages": 100, "keyword": "Catering"}
    model = ScriptedModel(tool, arguments, check)
    prompt = "Summarize the available records across pages for regression verification."
    async with engine_setup(tmp_path, httpx.MockTransport(respond)) as (engine, store):
        engine.model = model
        rid, _ = await store.create_run("operator", prompt, "agent", "pagination-many-pages", {})
        result = await engine.invoke(initial_state(rid, "operator", prompt, "agent"), rid)
        assert result["status"] == "completed", result
        assert engine.settings.max_iterations == 2
        assert len(model.rounds) == 2 and len(observed) == 1
        assert calls == [{"pageNum": page, "pageSize": 100, "keyword": "Catering"} for page in range(1, 12)]
        operations = await store.fetch("SELECT * FROM operations WHERE run_id=?", (rid,))
        assert len(operations) == 1
        assert operations[0]["tool_name"] == tool and operations[0]["status"] == "completed"
        evidence = json.loads(store.fernet.decrypt(operations[0]["result_cipher"].encode()))
        assert_original_fields(evidence["data"]["list"], records)
        assert_statistics(tool, evidence["data"]["statistics"], records)
        state = await engine.graph.aget_state({"configurable": {"thread_id": rid}})
        assert state.values["iterations"] == 2
        assert len(state.values["results"]) == 1
        assert state.values["pending"] == []
        assert result["approvals"] == []
        assert sum(event["kind"] == "model.started" for event in result["events"]) == 2
        assert sum(event["kind"] == "tool.started" for event in result["events"]) == 1
        assert sum(event["kind"] == "evaluation.step" for event in result["events"]) == 1
        assert (await store.verify())["valid"]


def test_compact_evidence_long_page_keeps_authoritative_pagination_metadata():
    records = make_records("shop_list", 100, extra_length=60000, core_length=1000)
    raw = page_response(records, 5000, pageNum=3, pageSize=100, returned=100)
    row = {"tool": "shop_list", "arguments": {"pageNum": 3, "pageSize": 100}, **raw}
    snapshot = deepcopy(row)
    evidence = compact_evidence([row], "long-page-run")
    assert len(evidence) == 1 and len(dumps(evidence)) <= 48000
    item = evidence[0]
    assert item["source_run_id"] == "long-page-run" and item["tool"] == "shop_list"
    assert item["arguments"] == row["arguments"] and item["code"] == 0
    data = item["data"]
    assert data["total"] == 5000 and data["pageNum"] == 3
    assert data["pageSize"] == 100 and data["returned"] == 100
    coverage = data["coverage"]
    assert coverage["kind"] == "page" and coverage["total"] == 5000
    assert coverage["pageNum"] == 3 and coverage["pageSize"] == 100
    assert coverage["returned"] == 100 and coverage["nextPage"] == 4
    assert coverage["complete"] is False
    assert 0 < len(data["list"]) < len(records)
    assert data.get("contextTruncated") is True or data.get("modelView", {}).get("omittedRecords", 0) > 0
    assert_statistics("shop_scan", data["statistics"], records)
    assert "RAW-ONLY-" not in dumps(evidence)
    assert_no_raw_truncation(evidence)
    assert row == snapshot


def test_compact_evidence_scan_excerpt_keeps_full_statistics_and_coverage():
    records = make_records("shop_scan", 350, extra_length=3000, core_length=1000)
    records[-1]["shopTypeName"] = "Tail-only group"
    records[-1]["creditScore"] = 999
    counts = dict(Counter(record["shopTypeName"] for record in records))
    scores = [record["creditScore"] for record in records]
    statistics = {
        "recordCount": 350,
        "groups": {"shopTypeName": {"top": counts, "otherRecords": 0, "distinctGroups": len(counts)}},
        "creditScore": {"knownCount": 350, "missingCount": 0, "minimum": min(scores), "maximum": max(scores), "average": round(sum(scores) / 350, 2)},
    }
    coverage = {
        "kind": "scan", "total": 350, "lastObservedTotal": 350, "complete": True,
        "recordsScanned": 350, "pagesScanned": 4, "pageSize": 100, "nextPage": None,
        "scope": "Current account; not a transactional snapshot", "reason": "All returned records retained",
        "pages": [{"pageNum": page, "pageSize": 100, "total": 350, "returned": 100 if page < 4 else 50} for page in range(1, 5)],
    }
    data = {
        "total": 350, "list": records, "complete": True, "outcome": "complete",
        "recordsScanned": 350, "pagesScanned": 4, "scope": coverage["scope"],
        "reason": coverage["reason"], "coverage": coverage, "statistics": statistics,
    }
    row = {"tool": "shop_scan", "arguments": {"pageSize": 100}, "code": 0, "data": data}
    snapshot = deepcopy(row)
    evidence = compact_evidence([row], "full-scan-run")
    assert len(evidence) == 1 and len(dumps(evidence)) <= 48000
    excerpt = evidence[0]["data"]
    assert 0 < len(excerpt["list"]) < 350
    assert excerpt.get("contextTruncated") is True or excerpt.get("modelView", {}).get("omittedRecords", 0) > 0
    assert excerpt["total"] == excerpt["recordsScanned"] == 350
    assert excerpt["pagesScanned"] == 4
    assert excerpt["complete"] is True and excerpt["outcome"] == "complete"
    assert excerpt["scope"] == coverage["scope"] and excerpt["reason"] == coverage["reason"]
    assert excerpt["coverage"] == {key: value for key, value in coverage.items() if key != "pages"}
    assert excerpt["modelView"]["perPageAuditInEvidence"] is True
    assert excerpt["statistics"] == statistics
    assert_statistics("shop_scan", excerpt["statistics"], records)
    assert excerpt["statistics"]["groups"]["shopTypeName"]["top"]["Tail-only group"] == 1
    assert records[-1]["id"] not in [record["id"] for record in excerpt["list"]]
    assert "RAW-ONLY-" not in dumps(evidence)
    assert_no_raw_truncation(evidence)
    assert row == snapshot


def test_model_empty_answer_falls_back_to_full_shop_scan(tmp_path):
    config = settings(tmp_path)
    config.deepseek_api_key = "test-key"
    asyncio.run(setup_users(config))
    records = make_records("shop_scan", 105)
    requests = []

    async def respond(request):
        requests.append(request)
        if request.url.path == TOOLS["account_login"].path:
            return httpx.Response(200, json={"code": 0, "msg": "Success", "data": {
                "userId": "operator", "username": "operator", "nickname": "Operator", "accessToken": "pagination-token",
            }})
        assert request.url.path == LIST_PATHS["shop_scan"] and request.method == "POST"
        arguments = json.loads(request.content)
        assert set(arguments) == {"pageNum", "pageSize"}
        assert arguments["pageSize"] == 100
        page = arguments["pageNum"]
        return httpx.Response(200, json=page_response(records[(page - 1) * 100:page * 100], 105))

    class NoToolModel:
        def __init__(self):self.calls=0
        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content="之前已查全单位，无需再查。")

    app = create_app(config, httpx.MockTransport(respond))
    with TestClient(app) as client:
        app.state.engine.model = NoToolModel()
        csrf, _ = login(client, "operator")
        body = {"prompt": "查询所有单位信息", "idempotency_key": "pagination-fallback-full-read"}
        response = client.post("/agent-api/runs", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 200, response.text
        rid = response.json()["id"]
        result = wait(client, rid, {"completed", "failed"})
        # 模型空答不再直接失败：只读兜底推断shop_scan补查当前证据。
        assert result["status"] == "completed", result
        assert any(event["kind"] == "query.fallback_tool" for event in result["events"])
        started = [event for event in result["events"] if event["kind"] == "tool.started"]
        assert len(started) == 1 and started[0]["payload"]["tool"] == "shop_scan"
        assert result["approvals"] == []
        completed = [event for event in result["events"] if event["kind"] == "tool.completed"]
        assert len(completed) == 1
        data = completed[0]["payload"]["result"]["data"]
        assert data["complete"] is True and data["outcome"] == "complete"
        assert data["total"] == data["recordsScanned"] == 105
        assert_original_fields(data["list"], records)
        assert_statistics("shop_scan", data["statistics"], records)
        scan_requests = [request for request in requests if request.url.path == LIST_PATHS["shop_scan"]]
        assert [json.loads(request.content)["pageNum"] for request in scan_requests] == [1, 2]
        repeat = client.post("/agent-api/runs", headers={"X-CSRF-Token": csrf}, json=body)
        assert repeat.status_code == 200 and repeat.json()["id"] == rid
        assert len(requests) == 3
        assert client.get("/agent-api/audit/verify").json()["valid"]


@pytest.mark.parametrize("prompt", [
    "查询所有单位信息并删除所有单位",
    "查询所有单位信息，然后修改信用分",
    "查询所有单位信息并新增单位",
    "先删除单位再查询所有单位信息",
    "查询所有单位信息。随后删除这些单位",
], ids=["delete-suffix", "update-suffix", "create-suffix", "write-prefix", "second-write-sentence"])
def test_full_read_shortcut_rejects_mixed_write_instructions(tmp_path, prompt):
    config = settings(tmp_path)
    asyncio.run(setup_users(config))
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.url.path == TOOLS["account_login"].path, "Mixed write instructions must not trigger a read-only shortcut or a write"
        return httpx.Response(200, json={"code": 0, "msg": "Success", "data": {
            "userId": "operator", "username": "operator", "nickname": "Operator", "accessToken": "pagination-token",
        }})

    app = create_app(config, httpx.MockTransport(respond))
    with TestClient(app) as client:
        assert app.state.engine.model is None
        csrf, _ = login(client, "operator")
        response = client.post("/agent-api/runs", headers={"X-CSRF-Token": csrf}, json={
            "prompt": prompt, "idempotency_key": "pagination-reject-mixed-write",
        })
        assert response.status_code == 503, response.text
        assert len(requests) == 1
        assert client.get("/agent-api/runs").json() == []
        assert client.get("/agent-api/approvals").json() == []


def test_response_page_metadata_cannot_override_request_or_fake_completeness():
    from app.reports import public_report
    records = make_records("shop_list", 2)
    raw = page_response(records, 2, pageNum=3, pageSize=2, returned=2)
    arguments = {"pageNum": 1, "pageSize": 2}
    data = observation("shop_list", arguments, raw)["data"]
    assert data["pageNum"] == data["coverage"]["pageNum"] == 1
    assert data["coverage"]["responseMetadata"]["pageNum"] == 3
    assert data["coverage"]["metadataConsistent"] is False
    assert data["coverage"]["complete"] is False
    row = {"tool": "shop_list", "arguments": arguments, **raw}
    report = public_report("已查全全部单位。", "查询单位", [row])
    assert "已查全全部单位" not in report
    assert "响应页码/条数与请求不一致" in report
    assert "第 1 页" in report


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", SCAN_TOOLS)
async def test_scan_conflicting_response_page_is_not_retained_or_complete(tmp_path, tool):
    records = make_records(tool, 2)
    async def respond(request):
        return httpx.Response(200, json=page_response(records, 2, pageNum=9))
    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        data = (await upstream.call(TOOLS[tool], {"pageSize": 2}, "pagination-token"))["data"]
    finally:
        await upstream.aclose()
    assert data["complete"] is False and data["outcome"] == "incomplete"
    assert data["recordsScanned"] == 0 and data["total"] == 2
    assert data["coverage"]["pages"][0]["retained"] is False
    assert "矛盾" in data["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b'{"code":0,"data":null}', b'{"code":503,"msg":"wrong code"}'])
async def test_http_401_wins_over_conflicting_or_non_json_body(tmp_path, body):
    async def respond(request):
        return httpx.Response(401, content=body)
    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError) as error:
            await upstream.call(TOOLS["shop_scan"], {}, "pagination-token")
        assert error.value.code == error.value.http_status == 401
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_lookup_unknown_total_and_conflicting_page_are_not_empty_or_absent(tmp_path):
    async def respond(request):
        return httpx.Response(503, json={"code":503,"msg":"unavailable","data":None})
    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(respond))
    try:
        data = (await upstream.call(TOOLS["warning_scan"], {"match":{"id":"9300000000000001602"}}, "pagination-token"))["data"]
        assert data["total"] is None and data["outcome"] == "incomplete" and data["list"] == []
    finally:
        await upstream.aclose()
    async def wrong_page(request):
        return httpx.Response(200, json=page_response(make_records("warning_list", 1), 1, pageNum=2))
    upstream = UpstreamClient(settings(tmp_path), httpx.MockTransport(wrong_page))
    try:
        data = (await upstream.call(TOOLS["warning_scan"], {"match":{"id":"9300000000000001602"}}, "pagination-token"))["data"]
        assert data["outcome"] == "incomplete" and not data["complete"] and data["list"] == []
    finally:
        await upstream.aclose()


def test_untrusted_extra_statistics_do_not_override_retained_page_records():
    from app.reports import public_report
    records = make_records("shop_list", 2)
    raw = page_response(records, 300, statistics={"recordCount":999999,"groups":{"shopTypeName":{"topEntries":[]}}})
    args = {"pageNum":1,"pageSize":2}
    data = observation("shop_list", args, raw)["data"]
    assert_statistics("shop_scan", data["statistics"], records)
    report = public_report("total未取得", "查询单位", [{"tool":"shop_list","arguments":args,**raw}])
    assert "999999" not in report and "300" in report and "total未取得" not in report


def test_observation_long_group_labels_and_small_context_stay_bounded():
    records = make_records("shop_list", 100)
    for index, row in enumerate(records):
        row["shopTypeName"] = "共同前缀" * 1000 + str(index)
    raw = page_response(records, 5000)
    snapshot = deepcopy(raw)
    args = {"pageNum":1,"pageSize":100}
    for budget in (24000, 4000, 3000):
        view = observation("shop_list", args, raw, budget=budget)
        assert len(dumps(view)) <= budget
        data = view["data"]
        assert data["total"] == 5000 and data["statistics"]["recordCount"] == 100
        group = data["statistics"]["groups"]["shopTypeName"]
        assert sum(item["count"] for item in group.get("topEntries",[])) + group["otherRecords"] == 100
        assert len({item["labelHash"] for item in group.get("topEntries",[])}) == len(group.get("topEntries",[]))
        assert data["coverage"]["returned"] == 100
    assert raw == snapshot


def test_mixed_results_replace_model_false_completeness_and_keep_other_evidence():
    from app.reports import public_report
    rows = [{"tool":"shop_scan","arguments":{"keyword":"School"},"code":0,"data":{
        "total":300,"list":[],"complete":False,"recordsScanned":0,"pagesScanned":0,"reason":"查询失败"}},
        {"tool":"current_agencies","code":0,"data":{"id":"AG001","name":"测试机构"}}]
    report = public_report("全部单位已查全，无需进一步查询。", "查询单位和机构", rows)
    assert "全部单位已查全" not in report and "不能宣称查全" in report
    assert "School" in report and "测试机构" in report and "300" in report


def test_read_time_summary_keeps_page_keyword_and_historical_scope():
    from app.reports import display_result
    raw = page_response(make_records("shop_list", 2), 300)
    row = {"tool":"shop_list","arguments":{"pageNum":3,"pageSize":2,"keyword":"School"},**raw}
    run = {"id":"new-run","prompt":"用列表形式列出","status":"completed","result":{
        "report":"旧模型声称查全","steps":[row],"context_reused":True,"source_run_id":"old-run"},"events":[]}
    display = display_result(run)["result"]["display_report"]
    assert "School" in display and "第 3 页" in display
    assert "历史查询时为准" in display and "old-run" in display
