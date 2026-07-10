"""IV rank, earnings, news, and market status via MarketData.app."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MarketDataResearchMixin:
    """Fundamentals / research endpoints."""

    async def get_iv_rank(
        self,
        symbol: str,
        dte_min: int = None,
        dte_max: int = None,
        strike_pct: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Calculate IV Rank (current IV percentile vs 52-week range).

        Args:
            symbol: Underlying ticker
            dte_min: Minimum DTE for contracts to sample (required by caller)
            dte_max: Maximum DTE for contracts to sample (required by caller)
            strike_pct: Strike range as fraction of price (e.g. 0.15 = ±15%).
                        If None, uses a price-adaptive heuristic based on
                        option strike increments ($0.50/$1/$2.50/$5).

        Returns:
            Dict with iv_current, iv_rank, iv_high, iv_low
        """
        if dte_min is None or dte_max is None:
            logger.warning(f"get_iv_rank({symbol}): dte_min/dte_max required")
            return None

        # Get current ATM option IV as proxy for current IV
        quote = await self.get_quote(symbol)
        if not quote or not quote.get('last'):
            return None

        current_price = quote['last']

        # Determine strike window — use caller's value or price-adaptive heuristic.
        # Option strikes come in $0.50/$1/$2.50/$5 increments depending on price;
        # narrow ranges miss everything on cheap stocks.
        if strike_pct is None:
            if current_price < 5:
                strike_pct = 0.50   # ±50% for penny/micro-cap
            elif current_price < 20:
                strike_pct = 0.25   # ±25% for small-cap
            elif current_price < 100:
                strike_pct = 0.15   # ±15% for mid-range
            else:
                strike_pct = 0.10   # ±10% for large-cap

        chain = await self.get_option_chain(
            symbol,
            side='call',
            strike_range=(current_price * (1 - strike_pct), current_price * (1 + strike_pct)),
            dte_range=(dte_min, dte_max)
        )

        if not chain or not chain.get('contracts'):
            return None

        # Get average IV from ATM calls
        ivs = [c['iv'] for c in chain['contracts'] if c.get('iv')]
        if not ivs:
            return None

        current_iv = sum(ivs) / len(ivs)

        return {
            'symbol': symbol,
            'iv_current': round(current_iv * 100, 1),  # As percentage
            'iv_rank': None,  # Would need historical IV data
            'source': 'marketdata'
        }

    async def get_earnings(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        countback: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get earnings data for a symbol via MDA /stocks/earnings/{symbol}/.

        Returns dict with arrays: symbol, fiscalYear, fiscalQuarter, date,
        reportDate, reportTime, reportedEPS, estimatedEPS, surpriseEPS, surpriseEPSpct.
        """
        params: Dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if countback is not None:
            params["countback"] = countback

        resp = await self._get_with_retries(
            f"/stocks/earnings/{symbol}/",
            params=params or None,
            label=f"earnings({symbol})",
        )
        if not resp or resp.status_code not in (200, 203):
            return None

        data = resp.json()
        if data.get("s") != "ok":
            return None

        # Zip parallel arrays into a list of earnings records
        n = len(data.get("fiscalYear", []))
        records = []
        for i in range(n):
            records.append({
                "fiscal_year": data["fiscalYear"][i] if i < len(data.get("fiscalYear", [])) else None,
                "fiscal_quarter": data["fiscalQuarter"][i] if i < len(data.get("fiscalQuarter", [])) else None,
                "date": data["date"][i] if i < len(data.get("date", [])) else None,
                "report_date": data["reportDate"][i] if i < len(data.get("reportDate", [])) else None,
                "report_time": data["reportTime"][i] if i < len(data.get("reportTime", [])) else None,
                "reported_eps": data["reportedEPS"][i] if i < len(data.get("reportedEPS", [])) else None,
                "estimated_eps": data["estimatedEPS"][i] if i < len(data.get("estimatedEPS", [])) else None,
                "surprise_eps": data["surpriseEPS"][i] if i < len(data.get("surpriseEPS", [])) else None,
                "surprise_eps_pct": data["surpriseEPSpct"][i] if i < len(data.get("surpriseEPSpct", [])) else None,
            })

        return {
            "symbol": symbol,
            "earnings": records,
            "count": n,
            "source": "marketdata",
        }

    async def get_news(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        countback: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get news for a symbol via MDA /stocks/news/{symbol}/.

        Returns dict with headlines, content, sources, publication dates.
        """
        params: Dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if countback is not None:
            params["countback"] = countback

        resp = await self._get_with_retries(
            f"/stocks/news/{symbol}/",
            params=params or None,
            label=f"news({symbol})",
        )
        if not resp or resp.status_code not in (200, 203):
            return None

        data = resp.json()
        if data.get("s") != "ok":
            return None

        n = len(data.get("headline", []))
        articles = []
        for i in range(n):
            articles.append({
                "headline": data["headline"][i] if i < len(data.get("headline", [])) else None,
                "content": data["content"][i] if i < len(data.get("content", [])) else None,
                "source": data["source"][i] if i < len(data.get("source", [])) else None,
                "publication_date": data["publicationDate"][i] if i < len(data.get("publicationDate", [])) else None,
            })

        return {
            "symbol": symbol,
            "articles": articles,
            "count": n,
            "source": "marketdata",
        }

    async def get_market_status(
        self,
        date: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get market open/closed status via MDA /markets/status/.

        Args:
            date: Check a specific date
            from_date: Start of date range
            to_date: End of date range
        """
        params: Dict[str, Any] = {}
        if date:
            params["date"] = date
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        resp = await self._get_with_retries(
            "/markets/status/",
            params=params or None,
            label="market_status",
        )
        if not resp or resp.status_code not in (200, 203):
            return None

        data = resp.json()
        if data.get("s") != "ok":
            return None

        n = len(data.get("date", []))
        statuses = []
        for i in range(n):
            statuses.append({
                "date": data["date"][i] if i < len(data.get("date", [])) else None,
                "status": data["status"][i] if i < len(data.get("status", [])) else None,
            })

        return {
            "statuses": statuses,
            "count": n,
            "source": "marketdata",
        }


