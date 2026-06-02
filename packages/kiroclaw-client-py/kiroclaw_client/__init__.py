"""kiroclaw-client — async Python client for the KiroClaw Gateway.

Usage::

    from kiroclaw_client import KiroClawClient

    async with KiroClawClient(app_name="my-app") as mc:
        ok = await mc.ping()
        status = await mc.get_status()
        await mc.send_message("slot-1", "hello")
"""
from kiroclaw_client.client import KiroClawClient
from kiroclaw_client.errors import KiroClawError, ErrorCode

__all__ = ["KiroClawClient", "KiroClawError", "ErrorCode"]
