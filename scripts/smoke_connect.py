"""Quick smoke: IBKR connect + SPY quote."""
import asyncio
from dotenv import load_dotenv

load_dotenv()

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.marketdata.client import get_marketdata_client


async def main():
    c = get_ibkr_connector()
    ok = await c.connect()
    print("IBKR:", "connected" if ok else "FAILED", f"account={getattr(c, 'account_id', None)}")
    if ok:
        s = await c.get_account_summary()
        print("NLV:", s.get("netliquidation"), "cash:", s.get("totalcashvalue"))
        await c.disconnect()
    q = await get_marketdata_client().get_quote("SPY")
    if q:
        print("SPY:", {k: q.get(k) for k in ("mid", "bid", "ask", "change_pct")})
    else:
        print("SPY quote: None")


if __name__ == "__main__":
    asyncio.run(main())
