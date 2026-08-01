import asyncio
import json
import os
from pathlib import Path

import httpx
import websockets
from hermes_fleet.security import sign_payload


async def main() -> None:
    credentials = json.loads(Path("data/parrot-local.json").read_text())
    token = os.environ["HFC_OPERATOR_TOKEN"]
    body = {"status": "online", "metrics": {"demo": 1}, "capabilities": {"stream_verified": True}}
    async with websockets.connect(f"ws://127.0.0.1:8876/api/v1/events/ws?token={token}") as websocket:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"http://127.0.0.1:8876/api/v1/nodes/{credentials['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": sign_payload(credentials["node_secret"], body)})
            response.raise_for_status()
        print(await asyncio.wait_for(websocket.recv(), timeout=5))


asyncio.run(main())
