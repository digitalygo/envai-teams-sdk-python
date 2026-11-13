"""Typed report views, strict filters, exact wire values, and local pagination."""

from __future__ import annotations

import copy
import inspect
from typing import get_args
from urllib.parse import unquote

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient, RetryPolicy, models
from envai._generated import RESPONSE_TYPES
from envai._schema import OPERATIONS
from envai.errors import (
    AccessDeniedError,
    NotFoundError,
    ProtocolError,
    ServerError,
    ValidationError,
)

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PROJECT = "prj_" + ULID
OTHER_PROJECT = "prj_01j5abcdefghjkmnpqrstvwxyz"
WORKSPACE = "wsp_" + ULID
WORKSPACE_2 = "wsp_01j5abcdefghjkmnpqrstvwxyz"
CHAT = "cht_" + ULID
KEY = "key_" + ULID
KEY_2 = "key_01j5abcdefghjkmnpqrstvwxyz"
FROM = "2026-10-01"
TO = "2026-10-10"
USAGE_PATH = "/api/v1/projects/" + PROJECT + "/usage"
LOGS_PATH = "/api/v1/projects/" + PROJECT + "/logs"
VIEW_FILTERS = {
    "usage": {"from": FROM, "to": TO, "api_key_id": KEY, "per_page": 25},
    "logs": {
        "from": FROM, "to": TO, "api_key_id": KEY, "chat_id": CHAT,
        "type": "agent.started", "per_page": 25,
    },
    "analytics": {"from": FROM, "to": TO, "api_key_id": KEY, "chat_id": CHAT},
    "filters": {"from": FROM, "to": TO},
    "event_types": {"from": FROM, "to": TO},
}
VIEW_TYPES = {
    "usage": models.UsageReportCollection,
    "logs": models.ProjectLogCollection,
    "analytics": models.ProjectActivityAnalytics,
    "filters": models.ProjectEventTypeFilters,
    "event_types": models.ProjectEventTypeFilters,
}
ERROR_TYPES = {
    403: (AccessDeniedError, "access_denied"),
    404: (NotFoundError, "not_found"),
    422: (ValidationError, "validation_error"),
    503: (ServerError, "server_error"),
}


def _usage_day():
    key = {
        "api_key_id": KEY,
        "totals": [{"currency": "USD", "amount": "0"}],
        "invocation_count": 0,
        "unknown_invocations": 0,
        "not_submitted_invocations": 0,
        "completion_count": 0,
        "unknown_currency_observations": 1,
    }
    return {
        "date": TO,
        "totals": [
            {"currency": "EUR", "amount": "0.100000000000"},
            {"currency": "USD", "amount": "1.250000000000"},
        ],
        "invocation_count": 0,
        "unknown_invocations": 1,
        "not_submitted_invocations": 0,
        "completion_count": 0,
        "by_api_key": [key],
        "media": {
            "totals": [{"currency": "USD", "amount": "2.500000000000"}],
            "priced_observations": 2,
            "unknown_observations": 1,
            "unknown_currency_observations": 1,
            "by_api_key": [key],
        },
        "by_project": None,
        "future_marker": "kept",
    }


def _links(resource, last_page):
    return {
        "first": "https://example.test" + resource + "?page=1",
        "last": "https://example.test" + resource + "?page=" + str(last_page),
        "prev": None,
        "next": "https://remote.invalid/api/v1/projects/" + OTHER_PROJECT
        + ("/usage" if resource.endswith("usage") else "/logs") + "?page=2",
        "future_link": "kept",
    }


def _meta(current_page, last_page):
    return {
        "current_page": current_page,
        "from": current_page,
        "last_page": last_page,
        "per_page": 25,
        "to": current_page,
        "total": 2,
        "future_meta": 7,
    }


def _usage_page(current_page=1, last_page=1):
    return {
        "data": [_usage_day()],
        "links": _links(USAGE_PATH, last_page),
        "meta": _meta(current_page, last_page),
    }


def _log_entry():
    return {
        "id": "evt_" + ULID,
        "project_id": PROJECT,
        "api_key_id": None,
        "actor_api_key_id": None,
        "chat_id": CHAT,
        "workspace_id": WORKSPACE,
        "seq": 0,
        "turn_id": None,
        "type": "agent.started",
        "ts": "2026-10-10T12:00:00Z",
        "agent": [],
        "data": [],
        "future_marker": "kept",
        "future_object": {"agent": [], "data": []},
    }


def _log_page(current_page=1, last_page=1):
    return {
        "data": [_log_entry()],
        "links": _links(LOGS_PATH, last_page),
        "meta": _meta(current_page, last_page),
    }


def _normalized_log_page(current_page=1, last_page=1):
    payload = _log_page(current_page, last_page)
    payload["data"][0]["agent"] = {}
    payload["data"][0]["data"] = {}
    return payload


def _duration_distribution():
    bounds = [0, 100, 500, 1000, 2000, 5000, 10000, 30000]
    return {
        "candidate_count": 0,
        "sample_count": 0,
        "missing_count": 0,
        "invalid_count": 0,
        "zero_count": 0,
        "min_ms": None,
        "max_ms": None,
        "avg_ms": None,
        "p50_ms": None,
        "p95_ms": None,
        "buckets": [
            {
                "lower_ms": bounds[index],
                "upper_ms": bounds[index + 1] if index + 1 < len(bounds) else None,
                "count": 0,
            }
            for index in range(len(bounds))
        ],
    }


def _analytics_payload():
    daily = {
        "date": FROM,
        "event_count": 0,
        "tool_start_count": 0,
        "file_read_start_count": 0,
        "turn_failure_count": 0,
        "agent_failure_count": 0,
        "tool_failure_count": 0,
    }
    return {
        "window": {
            "from": FROM,
            "to": TO,
            "timezone": "UTC",
            "api_key_id": KEY,
            "chat_id": CHAT,
        },
        "coverage": {
            "source": "retained_chat_events",
            "aggregation": "all_retained_matching_rows",
            "execution_history_complete": False,
            "configured_retention_days": 30,
            "observed_event_count": 0,
            "oldest_observed_at": None,
            "newest_observed_at": None,
        },
        "daily": [daily, {**daily, "date": "2026-10-02"}],
        "event_types": {"items": [], "other_count": 0, "omitted_group_count": 0},
        "tools": {
            "items": [], "other_count": 0, "omitted_group_count": 0,
            "missing_name_count": 0,
        },
        "failures": {
            "turn_event_count": 0,
            "agent_event_count": 0,
            "tool_event_count": 0,
            "tool_result_unknown_status_count": 0,
        },
        "durations": {
            "tool_execution": _duration_distribution(),
            "turn_execution": _duration_distribution(),
        },
        "file_reads": [
            {"tool_name": name, "count": 0}
            for name in ("read", "wiki_read", "skill_read", "wiki_read_source", "wiki_read_artifact")
        ],
    }


def _catalog_payload():
    return {"data": {"types": ["agent.started", "turn.completed"]}}


def _view_payload(view):
    if view == "usage":
        return _usage_page(), _usage_page()
    if view == "logs":
        return _log_page(), _normalized_log_page()
    if view == "analytics":
        payload = {"data": _analytics_payload()}
        return payload, payload["data"]
    payload = _catalog_payload()
    return payload, payload["data"]


def _page_payload(view, current_page, last_page):
    return _usage_page(current_page, last_page) if view == "usage" else _log_page(
        current_page, last_page
    )


def _view_path(view):
    return USAGE_PATH if view == "usage" else LOGS_PATH


def _view_pairs(view):
    pairs = []
    if view == "logs":
        pairs.append(("view", "events"))
    elif view == "analytics":
        pairs.append(("view", "analytics"))
    elif view in ("filters", "event_types"):
        pairs.append(("view", "filters"))
    pairs.extend((key, str(value)) for key, value in VIEW_FILTERS[view].items())
    return pairs


def _client(async_, handler):
    cls = AsyncEnvaiClient if async_ else EnvaiClient
    return cls(
        "project-key",
        base_url="https://example.test/",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_retries=0, base_delay=0, max_delay=0),
    )


async def _invoke(value):
    return await value if inspect.isawaitable(value) else value


async def _close(client):
    if isinstance(client, AsyncEnvaiClient):
        await client.aclose()
    else:
        client.close()


async def _collect(iterator, async_):
    return [item async for item in iterator] if async_ else list(iterator)


def _view_call(client, view, filters=None, **kwargs):
    return getattr(client.project_reports, view)(PROJECT, filters, **kwargs)


def _page_call(client, view, page, filters):
    method = "usage_page" if view == "usage" else "logs_page"
    return getattr(client.project_reports, method)(PROJECT, page, filters)


def _iterate_call(client, view, filters, start_page):
    return getattr(client.project_reports, "iterate_" + view)(PROJECT, filters, start_page)


def test_report_union_wrappers_are_not_unwrapped():
    assert OPERATIONS["getProjectUsage"]["unwrap"] is False
    assert OPERATIONS["getProjectLogs"]["unwrap"] is False
    assert RESPONSE_TYPES["UsageReportCollection"] is models.UsageReportCollection
    assert get_args(RESPONSE_TYPES[OPERATIONS["getProjectLogs"]["response_type"]]) == (
        models.ProjectLogCollection,
        models.ProjectActivityAnalyticsResource,
        models.ProjectEventTypeFiltersResource,
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view", ["usage", "logs", "analytics", "filters", "event_types"])
async def test_typed_report_views_and_filters(async_, view):
    payload, expected = _view_payload(view)
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        result = await _invoke(_view_call(client, view, VIEW_FILTERS[view]))
        assert isinstance(result, VIEW_TYPES[view])
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "GET" and request.url.path == _view_path(view)
        assert request.url.host == "example.test"
        assert request.headers["authorization"] == "Bearer project-key"
        assert request.headers.get("x-envai-project-id") is None
        assert list(request.url.params.multi_items()) == _view_pairs(view)
        dumped = result.model_dump(mode="json", exclude_unset=True, by_alias=True)
        assert dumped == expected
        if view == "usage":
            day = result.data[0]
            assert day.totals[0].amount == "0.100000000000"
            assert day.totals[1].amount == "1.250000000000"
            assert day.by_project is None and day.unknown_invocations == 1
            assert day.future_marker == "kept" and result.meta.future_meta == 7
        elif view == "logs":
            entry = result.data[0]
            assert entry.agent == {} and entry.data == {}
            assert entry.future_object == {"agent": [], "data": []}
            assert entry.api_key_id is None and entry.actor_api_key_id is None
            assert entry.turn_id is None and entry.future_marker == "kept"
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("direct", [False, True])
async def test_bracket_list_filters_use_repeated_wire_keys(async_, direct):
    payload = _log_page(2, 2)
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    filters = {
        "from": FROM,
        "to": TO,
        "types[]": ["alpha", "beta"],
        "workspace_ids[]": [WORKSPACE, WORKSPACE_2],
        "api_key_ids[]": [KEY, KEY_2],
        "per_page": 50,
    }
    try:
        if direct:
            result = await _invoke(client.get_project_logs(
                PROJECT,
                types=filters["types[]"],
                workspace_ids=filters["workspace_ids[]"],
                api_key_ids=filters["api_key_ids[]"],
                page=2,
                per_page=50,
            ))
        else:
            result = await _invoke(_page_call(client, "logs", 2, filters))
        assert isinstance(result, models.ProjectLogCollection)
        assert len(seen) == 1
        request = seen[0]
        assert request.url.path == LOGS_PATH and request.url.host == "example.test"
        assert request.url.params.get_list("types[]") == ["alpha", "beta"]
        assert request.url.params.get_list("workspace_ids[]") == [WORKSPACE, WORKSPACE_2]
        assert request.url.params.get_list("api_key_ids[]") == [KEY, KEY_2]
        assert "types[]=alpha&types[]=beta" in unquote(str(request.url))
        expected = [("view", "events")] if not direct else []
        if not direct:
            expected.extend([("from", FROM), ("to", TO)])
        expected.extend([
            ("types[]", "alpha"), ("types[]", "beta"),
            ("workspace_ids[]", WORKSPACE), ("workspace_ids[]", WORKSPACE_2),
            ("api_key_ids[]", KEY), ("api_key_ids[]", KEY_2),
        ])
        if not direct:
            expected.append(("per_page", "50"))
        else:
            expected.append(("page", "2"))
            expected.append(("per_page", "50"))
        if not direct:
            expected.append(("page", "2"))
        assert list(request.url.params.multi_items()) == expected
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view,filters", [
    ("usage", "not-a-dictionary"),
    ("usage", {"unknown": "x"}),
    ("usage", {"from": None}),
    ("usage", {"from": 123}),
    ("usage", {"from": "2026-02-30"}),
    ("usage", {"from": "2026-10-20", "to": "2026-10-01"}),
    ("usage", {"from": "2026-01-01", "to": "2026-10-10"}),
    ("usage", {"api_key_id": "key_1"}),
    ("usage", {"per_page": 101}),
    ("usage", {"page": 0}),
    ("logs", {"type": "alpha", "types[]": ["beta"]}),
    ("logs", {"api_key_id": KEY, "api_key_ids[]": [KEY_2]}),
    ("logs", {"types[]": []}),
    ("logs", {"types[]": ["alpha", "alpha"]}),
    ("logs", {"types[]": ["type." + str(index) for index in range(51)]}),
    ("logs", {"types[]": [1]}),
    ("logs", {"types[]": "alpha"}),
    ("logs", {"api_key_ids[]": ["k" * 31]}),
    ("logs", {"type": "x" * 81}),
    ("logs", {"view": "analytics"}),
    ("analytics", {"view": "events"}),
    ("analytics", {"type": "alpha"}),
    ("analytics", {"types[]": ["alpha"]}),
    ("analytics", {"page": 1}),
    ("analytics", {"per_page": 25}),
    ("filters", {"api_key_id": KEY}),
    ("filters", {"chat_id": CHAT}),
    ("filters", {"type": "alpha"}),
    ("filters", {"page": 1}),
    ("event_types", {"per_page": 25}),
])
async def test_invalid_report_filters_fail_before_network(async_, view, filters):
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200))
    try:
        with pytest.raises(ValueError):
            await _invoke(_view_call(client, view, filters))
        assert seen == []
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("kwargs", [
    {"view": "analytics", "page": 1},
    {"view": "analytics", "types": ["alpha"]},
    {"view": "filters", "chat_id": CHAT},
    {"view": "filters", "page": 1},
    {"view": "unknown"},
    {"type": "alpha", "types": ["beta"]},
    {"api_key_id": KEY, "api_key_ids": [KEY_2]},
])
async def test_direct_project_logs_query_restrictions_fail_before_network(async_, kwargs):
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200))
    try:
        with pytest.raises(ValueError):
            await _invoke(client.get_project_logs(PROJECT, **kwargs))
        assert seen == []
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view,payload", [
    ("logs", {"data": _analytics_payload()}),
    ("analytics", _log_page()),
    ("filters", _log_page()),
    ("event_types", _log_page()),
])
async def test_report_helpers_reject_mismatched_returned_views(async_, view, payload):
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        with pytest.raises(ProtocolError):
            await _invoke(_view_call(client, view, VIEW_FILTERS[view]))
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
async def test_direct_analytics_view_rejects_event_page(async_):
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=_log_page()))
    try:
        with pytest.raises(ProtocolError):
            await _invoke(client.get_project_logs(PROJECT, view="analytics"))
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", [403, 404, 422, 503])
@pytest.mark.parametrize("view", ["usage", "logs", "analytics", "filters", "event_types"])
async def test_report_error_statuses_map_for_every_view(async_, status, view):
    cls, kind = ERROR_TYPES[status]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={
            "error": {"type": kind, "code": "report_error", "message": "x", "details": None}
        })

    client = _client(async_, handler)
    try:
        with pytest.raises(cls) as error:
            await _invoke(_view_call(client, view, VIEW_FILTERS[view]))
        assert error.value.http_status == status and error.value.code == "report_error"
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view", ["usage", "logs"])
@pytest.mark.parametrize("start_page,filter_page", [(None, 2), (2, 5)])
async def test_report_iterators_use_numeric_local_pages_and_preserve_filters(
    async_, view, start_page, filter_page
):
    seen = []
    filters = {
        "from": FROM, "to": TO, "api_key_id": KEY, "per_page": 25, "page": filter_page,
    }

    def handler(request):
        seen.append(request)
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=_page_payload(view, page, 3))

    client = _client(async_, handler)
    try:
        items = await _collect(_iterate_call(client, view, filters, start_page), async_)
        assert len(items) == 2
        assert all(isinstance(item, (
            models.UsageReportDay if view == "usage" else models.ProjectLogEntry
        )) for item in items)
        assert len(seen) == 2
        for index, request in enumerate(seen):
            assert request.method == "GET" and request.url.path == _view_path(view)
            assert request.url.host == "example.test"
            assert "remote.invalid" not in str(request.url)
            assert OTHER_PROJECT not in str(request.url)
            assert request.url.params.get("page") == str(2 + index)
            assert request.url.params.get("from") == FROM
            assert request.url.params.get("to") == TO
            assert request.url.params.get("api_key_id") == KEY
            assert request.url.params.get("per_page") == "25"
            if view == "logs":
                assert request.url.params.get("view") == "events"
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view,page,filter_page", [("usage", 1, 2), ("logs", 2, 1)])
async def test_explicit_report_page_overrides_filter_page(async_, view, page, filter_page):
    seen = []
    filters = {
        "from": FROM, "to": TO, "api_key_id": KEY, "per_page": 25, "page": filter_page,
    }
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(
        200, json=_page_payload(view, page, page)
    ))
    try:
        result = await _invoke(_page_call(client, view, page, filters))
        assert result.meta.current_page == page
        assert len(seen) == 1
        request = seen[0]
        assert request.url.params.get("page") == str(page)
        assert request.url.params.get("from") == FROM
        assert request.url.params.get("to") == TO
        assert request.url.params.get("api_key_id") == KEY
        assert request.url.params.get("per_page") == "25"
        if view == "logs":
            assert request.url.params.get("view") == "events"
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("view", ["usage", "logs"])
async def test_report_iterators_reject_non_advancing_and_invalid_pages(async_, view):
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(
        200, json=_page_payload(view, 9, 9)
    ))
    try:
        with pytest.raises(ProtocolError):
            await _collect(_iterate_call(client, view, None, None), async_)
        assert len(seen) == 1
        with pytest.raises(ValueError):
            await _collect(_iterate_call(client, view, {"page": 2}, 0), async_)
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("field", ["agent", "data"])
async def test_project_log_metadata_normalization_rejects_nonempty_arrays(async_, field):
    payload = _log_page()
    payload["data"][0][field] = ["nonempty"]
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        with pytest.raises(ProtocolError):
            await _invoke(_view_call(client, "logs"))
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
async def test_project_log_schema_rejects_nonobject_entries(async_):
    payload = _log_page()
    payload["data"][0] = "not-an-entry"
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        with pytest.raises(ProtocolError):
            await _invoke(_view_call(client, "logs"))
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
async def test_report_payload_fixtures_are_not_mutated_by_normalization(async_):
    payload = _log_page()
    original = copy.deepcopy(payload)
    client = _client(async_, lambda request: httpx.Response(200, json=payload))
    try:
        result = await _invoke(_view_call(client, "logs"))
        assert payload == original
        assert result.data[0].agent == {} and result.data[0].data == {}
    finally:
        await _close(client)
