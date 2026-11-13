"""Typed project report helpers with local numeric pagination."""

from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any, AsyncGenerator, Generator, cast

from . import models
from ._transport import RequestOptions
from .errors import ProtocolError

ULID = r"[0-7][0-9a-hjkmnp-tv-zA-HJKMNP-TV-Z]{25}"
ID_PATTERNS = {
    "api_key_id": re.compile(rf"[kK][eE][yY]_{ULID}"),
    "chat_id": re.compile(rf"[cC][hH][tT]_{ULID}"),
}
LIST_LIMITS = {
    "types[]": 80,
    "workspace_ids[]": 30,
    "api_key_ids[]": 30,
}
USAGE_KEYS = frozenset({"from", "to", "api_key_id", "page", "per_page"})
EVENT_KEYS = frozenset({
    "view", "from", "to", "api_key_id", "chat_id", "type", "types[]",
    "workspace_ids[]", "api_key_ids[]", "page", "per_page",
})
ANALYTICS_KEYS = frozenset({"view", "from", "to", "api_key_id", "chat_id"})
FILTER_KEYS = frozenset({"view", "from", "to"})
PROHIBITED_BY_VIEW = {
    "events": frozenset(),
    "analytics": frozenset({"type", "types[]", "workspace_ids[]", "api_key_ids[]", "page", "per_page"}),
    "filters": frozenset({
        "type", "api_key_id", "chat_id", "types[]", "workspace_ids[]",
        "api_key_ids[]", "page", "per_page",
    }),
}
PROJECT_LOG_VIEW_TYPES = {
    "events": models.ProjectLogCollection,
    "analytics": models.ProjectActivityAnalyticsResource,
    "filters": models.ProjectEventTypeFiltersResource,
}


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f'Project report filter "{field}" must use YYYY-MM-DD.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'Project report filter "{field}" must use YYYY-MM-DD.') from exc
    return parsed


def _list(value: Any, field: str, max_length: int) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError(f'Project report filter "{field}" must be a list of 1 to 50 values.')
    if any(type(item) is not str or not 1 <= len(item) <= max_length for item in value):
        raise ValueError(f'Project report filter "{field}" contains an invalid value.')
    if len(set(value)) != len(value):
        raise ValueError(f'Project report filter "{field}" contains duplicate values.')


def _page(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("page must be positive.")
    return value


def _per_page(value: Any) -> None:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("per_page must be between 1 and 100.")


def _validate_report_query(values: dict[str, Any], allowed: frozenset[str]) -> None:
    for key, value in values.items():
        if not isinstance(key, str) or key not in allowed:
            raise ValueError(f'Unsupported project report filter "{key}".')
        if value is None:
            raise ValueError(f'Project report filter "{key}" must not be null.')
    start = _date(values["from"], "from") if "from" in values else None
    end = _date(values["to"], "to") if "to" in values else None
    if start is not None and end is not None:
        if start > end or (end - start).days + 1 > 90:
            raise ValueError("Project report range is invalid or exceeds the maximum window.")
    for field, pattern in ID_PATTERNS.items():
        if field in values and (
            not isinstance(values[field], str) or not pattern.fullmatch(values[field])
        ):
            raise ValueError(f'Project report filter "{field}" is not a valid id.')
    if "type" in values and (
        not isinstance(values["type"], str) or len(values["type"]) > 80
    ):
        raise ValueError("Project report event type filter is invalid.")
    for field, limit in LIST_LIMITS.items():
        if field in values:
            _list(values[field], field, limit)
    if "page" in values:
        _page(values["page"])
    if "per_page" in values:
        _per_page(values["per_page"])
    if "type" in values and "types[]" in values:
        raise ValueError("Scalar and array event type filters conflict.")
    if "api_key_id" in values and "api_key_ids[]" in values:
        raise ValueError("Scalar and array api key filters conflict.")
    view = values.get("view", "events")
    if not isinstance(view, str) or view not in PROHIBITED_BY_VIEW:
        raise ValueError("Unsupported project log view.")
    if any(key in values for key in PROHIBITED_BY_VIEW[view]):
        raise ValueError("Project log view filter is prohibited.")


def _filter_values(filters: dict[str, Any] | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError("Project report filters must be a dictionary.")
    return dict(filters)


def prepare_report_query(
    filters: dict[str, Any] | None,
    allowed: frozenset[str],
    view: str | None = None,
) -> dict[str, Any]:
    values = _filter_values(filters)
    if view is not None:
        if "view" in values and values["view"] != view:
            raise ValueError("Conflicting project log view.")
        values = {"view": view, **{key: value for key, value in values.items() if key != "view"}}
    _validate_report_query(values, allowed)
    return values


def validate_project_log_query(query: dict[str, Any]) -> None:
    _validate_report_query({key: value for key, value in query.items() if value is not None}, EVENT_KEYS)


def normalize_project_log_payload(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return value
    normalized = None
    for index, entry in enumerate(value["data"]):
        if isinstance(entry, dict):
            for field in ("agent", "data"):
                current = entry.get(field)
                if isinstance(current, list) and not current:
                    if normalized is None:
                        normalized = copy.deepcopy(value)
                    normalized["data"][index][field] = {}
    return value if normalized is None else normalized


def _select_page(filters: dict[str, Any] | None, page: int | None) -> int:
    if page is not None:
        return _page(page)
    return _page(_filter_values(filters).get("page", 1))


def _with_page(
    query: dict[str, Any], page: int, filters: dict[str, Any] | None
) -> dict[str, Any]:
    prepared = dict(query)
    if page > 1 or (filters is not None and "page" in filters):
        prepared["page"] = page
    return prepared


def _pagination_bounds(result: Any, number: int, label: str) -> int:
    current = getattr(result.meta, "current_page", None)
    last = getattr(result.meta, "last_page", None)
    if (
        type(current) is not int
        or type(last) is not int
        or current != number
        or last < number
    ):
        raise ProtocolError(f"{label} pagination did not advance as requested.", raw=result)
    return last


def _request(
    client: Any,
    operation: str,
    project_id: str,
    query: dict[str, Any],
    request_options: RequestOptions | None,
) -> Any:
    return client._request(
        operation,
        path_params={"project_id": project_id},
        query=query,
        headers={},
        request_options=request_options,
    )


async def _request_async(
    client: Any,
    operation: str,
    project_id: str,
    query: dict[str, Any],
    request_options: RequestOptions | None,
) -> Any:
    return await client._request(
        operation,
        path_params={"project_id": project_id},
        query=query,
        headers={},
        request_options=request_options,
    )


class ProjectReportsService:
    def __init__(self, client: Any):
        self._client = client

    def usage(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.UsageReportCollection:
        return self.usage_page(project_id, filters=filters, request_options=request_options)

    def usage_page(
        self,
        project_id: str,
        page: int | None = None,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.UsageReportCollection:
        number = _select_page(filters, page)
        query = _with_page(prepare_report_query(filters, USAGE_KEYS), number, filters)
        result = _request(self._client, "getProjectUsage", project_id, query, request_options)
        return cast(models.UsageReportCollection, result)

    def iterate_usage(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        start_page: int | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> Generator[models.UsageReportDay, None, None]:
        number = _select_page(filters, start_page)
        while True:
            result = self.usage_page(project_id, number, filters, request_options=request_options)
            last = _pagination_bounds(result, number, "Usage")
            yield from result.data
            if number >= last:
                return
            number += 1

    def logs(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectLogCollection:
        return self.logs_page(project_id, filters=filters, request_options=request_options)

    def logs_page(
        self,
        project_id: str,
        page: int | None = None,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectLogCollection:
        number = _select_page(filters, page)
        query = _with_page(prepare_report_query(filters, EVENT_KEYS, "events"), number, filters)
        result = _request(self._client, "getProjectLogs", project_id, query, request_options)
        return cast(models.ProjectLogCollection, result)

    def iterate_logs(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        start_page: int | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> Generator[models.ProjectLogEntry, None, None]:
        number = _select_page(filters, start_page)
        while True:
            result = self.logs_page(project_id, number, filters, request_options=request_options)
            last = _pagination_bounds(result, number, "Project log")
            yield from result.data
            if number >= last:
                return
            number += 1

    def analytics(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectActivityAnalytics:
        query = prepare_report_query(filters, ANALYTICS_KEYS, "analytics")
        result = _request(self._client, "getProjectLogs", project_id, query, request_options)
        resource = cast(models.ProjectActivityAnalyticsResource, result)
        return resource.data

    def filters(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectEventTypeFilters:
        query = prepare_report_query(filters, FILTER_KEYS, "filters")
        result = _request(self._client, "getProjectLogs", project_id, query, request_options)
        resource = cast(models.ProjectEventTypeFiltersResource, result)
        return resource.data

    def event_types(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectEventTypeFilters:
        return self.filters(project_id, filters, request_options=request_options)


class AsyncProjectReportsService:
    def __init__(self, client: Any):
        self._client = client

    async def usage(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.UsageReportCollection:
        return await self.usage_page(project_id, filters=filters, request_options=request_options)

    async def usage_page(
        self,
        project_id: str,
        page: int | None = None,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.UsageReportCollection:
        number = _select_page(filters, page)
        query = _with_page(prepare_report_query(filters, USAGE_KEYS), number, filters)
        result = await _request_async(
            self._client, "getProjectUsage", project_id, query, request_options
        )
        return cast(models.UsageReportCollection, result)

    async def iterate_usage(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        start_page: int | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> AsyncGenerator[models.UsageReportDay, None]:
        number = _select_page(filters, start_page)
        while True:
            result = await self.usage_page(
                project_id, number, filters, request_options=request_options
            )
            last = _pagination_bounds(result, number, "Usage")
            for item in result.data:
                yield item
            if number >= last:
                return
            number += 1

    async def logs(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectLogCollection:
        return await self.logs_page(project_id, filters=filters, request_options=request_options)

    async def logs_page(
        self,
        project_id: str,
        page: int | None = None,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectLogCollection:
        number = _select_page(filters, page)
        query = _with_page(prepare_report_query(filters, EVENT_KEYS, "events"), number, filters)
        result = await _request_async(
            self._client, "getProjectLogs", project_id, query, request_options
        )
        return cast(models.ProjectLogCollection, result)

    async def iterate_logs(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        start_page: int | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> AsyncGenerator[models.ProjectLogEntry, None]:
        number = _select_page(filters, start_page)
        while True:
            result = await self.logs_page(
                project_id, number, filters, request_options=request_options
            )
            last = _pagination_bounds(result, number, "Project log")
            for item in result.data:
                yield item
            if number >= last:
                return
            number += 1

    async def analytics(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectActivityAnalytics:
        query = prepare_report_query(filters, ANALYTICS_KEYS, "analytics")
        result = await _request_async(
            self._client, "getProjectLogs", project_id, query, request_options
        )
        resource = cast(models.ProjectActivityAnalyticsResource, result)
        return resource.data

    async def filters(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectEventTypeFilters:
        query = prepare_report_query(filters, FILTER_KEYS, "filters")
        result = await _request_async(
            self._client, "getProjectLogs", project_id, query, request_options
        )
        resource = cast(models.ProjectEventTypeFiltersResource, result)
        return resource.data

    async def event_types(
        self,
        project_id: str,
        filters: dict[str, Any] | None = None,
        *,
        request_options: RequestOptions | None = None,
    ) -> models.ProjectEventTypeFilters:
        return await self.filters(project_id, filters, request_options=request_options)
