"""A contract3 server fixture for pre-existing behavior tests.

Negotiation failure/security tests use bare httpx.MockTransport instead.
"""

import inspect

import httpx

CAPABILITIES = {
    "protocol_version": 3,
    "contract_version": 3,
    "supported_contract_versions": [2, 3],
    "legacy_v2_mode": "historical_reads_replay_only",
    "families": ["turn", "agent", "chat"],
    "event_types": ["ping"],
    "tool_kinds": ["client"],
    "features": ["replay", "client_tools"],
}
ACK = {"X-Envai-Contract-Version": "3"}


def negotiated_transport(handler):
    def acknowledge(response, request):
        if 200 <= response.status_code < 300 and "x-envai-contract-version" in request.headers:
            response.headers["X-Envai-Contract-Version"] = request.headers[
                "x-envai-contract-version"
            ]
        return response

    def sync(request):
        if request.url.path.endswith("/atp/v3/capabilities"):
            return httpx.Response(200, json=CAPABILITIES, headers=ACK)
        return acknowledge(handler(request), request)

    async def async_(request):
        if request.url.path.endswith("/atp/v3/capabilities"):
            return httpx.Response(200, json=CAPABILITIES, headers=ACK)
        return acknowledge(await handler(request), request)

    return httpx.MockTransport(async_ if inspect.iscoroutinefunction(handler) else sync)
