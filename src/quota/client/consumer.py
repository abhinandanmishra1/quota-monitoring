import asyncio
import logging
from typing import Any, Callable, Coroutine
from httpx import AsyncClient

logger = logging.getLogger(__name__)

class SampleDownstreamConsumer:
    """
    Demonstrates the consumer lifecycle as outlined in DESIGN.md:
    1. Call /v1/quota/consume.
    2. If quota granted, execute downstream work using operation_id as token.
    3. If downstream succeeds -> finalize.
    4. If downstream confirmed fails -> refund.
    5. If downstream times out or result is ambiguous -> keep CHARGED (do not refund).
    """

    def __init__(self, client: AsyncClient):
        self.client = client

    async def execute_task(
        self,
        org_id: str,
        feature: str,
        units: int,
        idempotency_key: str,
        downstream_fn: Callable[[], Coroutine[Any, Any, Any]],
    ) -> dict[str, Any]:
        consume_resp = await self.client.post(
            "/v1/quota/consume",
            json={
                "org_id": org_id,
                "feature": feature,
                "units": units,
                "idempotency_key": idempotency_key,
            },
        )

        if consume_resp.status_code == 429:
            return {
                "status": "REJECTED_QUOTA_EXCEEDED",
                "quota_status": "REJECTED",
                "details": consume_resp.json(),
            }

        if consume_resp.status_code != 200:
            return {
                "status": "CONSUME_FAILED",
                "quota_status": "ERROR",
                "details": consume_resp.text,
            }

        consume_data = consume_resp.json()
        op_id = consume_data["operation_id"]

        try:
            result = await downstream_fn()
            
            await self.client.post(
                "/v1/quota/finalize", json={"operation_id": op_id}
            )
            return {
                "status": "COMPLETED",
                "quota_status": "FINALIZED",
                "operation_id": op_id,
                "result": result,
            }

        except asyncio.TimeoutError:
            logger.warning(
                f"Downstream operation {op_id} timed out. Keeping CHARGED for reconciliation."
            )
            return {
                "status": "TIMEOUT_AMBIGUOUS",
                "quota_status": "CHARGED",
                "operation_id": op_id,
            }

        except Exception as exc:
            logger.info(
                f"Downstream operation {op_id} confirmed failed ({exc}). Refunding units."
            )
            # here we are refunding instantly however we can add a queue here
            # refund can be done with sometime not necessary to be instant
            await self.client.post(
                "/v1/quota/refund",
                json={"operation_id": op_id, "reason": str(exc)},
            )
            return {
                "status": "FAILED_REFUNDED",
                "quota_status": "REFUNDED",
                "operation_id": op_id,
                "error": str(exc),
            }
