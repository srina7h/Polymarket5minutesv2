"""
LEMA Trading Bot — Edge Score Calculator
Computes the 4-component Edge Score from the trading framework.
"""

import logging
import time
import config

logger = logging.getLogger("lema.edge")


class EdgeCalculator:
    """
    Computes Edge Score (ES) as a weighted sum of 4 sub-scores:
      ES = w₁·PriceEdge + w₂·Momentum + w₃·Sentiment + w₄·BookImbalance

    A trade is only valid when ES ≥ MIN_EDGE_THRESHOLD (default 8%).
    """

    def __init__(self):
        self.weights = config.EDGE_WEIGHTS

    def calculate(
        self,
        spot_price: float,
        oracle_open: float,
        market_probability: float,
        price_history: list,
        book_imbalance: float,
        sentiment_score: float = 0.0,
    ) -> dict:
        """
        Calculate composite edge score.

        Args:
            spot_price:         Current BTC spot price
            oracle_open:        BTC price at window open (oracle)
            market_probability: Current Polymarket UP odds (0–1)
            price_history:      List of (timestamp, price) tuples
            book_imbalance:     Order book imbalance [-1, +1]
            sentiment_score:    External sentiment (0 in prototype)

        Returns:
            dict with 'edge_score', sub-scores, 'direction', 'confidence'
        """
        pe = self._price_edge(spot_price, oracle_open, market_probability)
        ms = self._momentum_score(price_history, oracle_open)
        ss = self._sentiment_score(sentiment_score, pe["direction"])
        bi = self._book_imbalance_score(book_imbalance, pe["direction"])

        edge_score = (
            self.weights["price_edge"] * pe["score"]
            + self.weights["momentum"] * ms["score"]
            + self.weights["sentiment"] * ss["score"]
            + self.weights["book_imbalance"] * bi["score"]
        )

        # Determine confidence level
        if edge_score >= config.HIGH_CONVICTION_THRESHOLD:
            confidence = "VERY_HIGH"
        elif edge_score >= config.MIN_EDGE_THRESHOLD:
            confidence = "HIGH"
        elif edge_score >= 0.05:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return {
            "edge_score": round(edge_score, 4),
            "direction": pe["direction"],
            "confidence": confidence,
            "estimated_prob": round(pe["estimated_prob"], 4),
            "market_prob": round(market_probability, 4),
            "sub_scores": {
                "price_edge": round(pe["score"], 4),
                "momentum": round(ms["score"], 4),
                "sentiment": round(ss["score"], 4),
                "book_imbalance": round(bi["score"], 4),
            },
            "details": {
                "spot_delta": round(pe["spot_delta"], 2),
                "magnitude": round(pe["magnitude"], 4),
                "direction_consistency": ms["consistency"],
                "intervals_checked": ms["intervals"],
            },
        }

    # ── Sub-score: Price Edge (w=0.40) ──────

    def _price_edge(
        self, spot: float, oracle_open: float, market_prob: float
    ) -> dict:
        delta = spot - oracle_open
        direction = "UP" if delta >= 0 else "DOWN"
        magnitude = min(abs(delta) / config.PRICE_NORMALIZATION_USD, 1.0)
        estimated_prob = 0.50 + (0.45 * magnitude)

        # If direction is DOWN, we're estimating P(DOWN),
        # and market_prob is P(UP), so compare with 1 - market_prob
        if direction == "UP":
            relevant_market_prob = market_prob
        else:
            relevant_market_prob = 1.0 - market_prob

        if relevant_market_prob > 0:
            score = max(
                0, (estimated_prob - relevant_market_prob) / relevant_market_prob
            )
        else:
            score = 0.0

        # Cap at 1.0
        score = min(score, 1.0)

        return {
            "score": score,
            "direction": direction,
            "spot_delta": delta,
            "magnitude": magnitude,
            "estimated_prob": estimated_prob,
        }

    # ── Sub-score: Momentum (w=0.30) ────────

    def _momentum_score(self, price_history: list, oracle_open: float) -> dict:
        """
        Check directional consistency over 30-second intervals.
        Score = (intervals_in_direction / total) - 0.50, capped [0, 0.50].
        """
        if len(price_history) < 10:
            return {"score": 0.0, "consistency": 0, "intervals": 0}

        now = time.time()
        # Create 30-second buckets for the last 4 minutes
        interval_seconds = 30
        num_intervals = 8  # 4 minutes / 30s each
        intervals_up = 0
        intervals_down = 0
        total = 0

        for i in range(num_intervals):
            end_t = now - (i * interval_seconds)
            start_t = end_t - interval_seconds

            prices_in = [p for t, p in price_history if start_t <= t < end_t]
            if len(prices_in) >= 2:
                total += 1
                if prices_in[-1] > prices_in[0]:
                    intervals_up += 1
                elif prices_in[-1] < prices_in[0]:
                    intervals_down += 1

        if total == 0:
            return {"score": 0.0, "consistency": 0, "intervals": 0}

        dominant = max(intervals_up, intervals_down)
        consistency = dominant / total
        score = max(0, consistency - 0.50)

        # Apply time-weighting: recent intervals count more
        # (simplified: just use the raw score, capped at 0.50)
        score = min(score, 0.50)

        return {
            "score": score,
            "consistency": round(consistency, 2),
            "intervals": total,
            "up": intervals_up,
            "down": intervals_down,
        }

    # ── Sub-score: Sentiment (w=0.15) ───────

    def _sentiment_score(
        self, raw_sentiment: float, direction: str
    ) -> dict:
        """
        Placeholder for Twitter/NLP sentiment.
        In the prototype, this always returns 0.0.
        """
        # Future: integrate VADER / BERT on Twitter feed
        return {"score": max(0, raw_sentiment), "raw": raw_sentiment}

    # ── Sub-score: Book Imbalance (w=0.15) ──

    def _book_imbalance_score(
        self, imbalance: float, direction: str
    ) -> dict:
        """
        Use order book imbalance. Positive = bid-heavy (UP),
        negative = ask-heavy (DOWN). Score only if aligned with direction.
        """
        if direction == "UP" and imbalance > 0:
            score = min(abs(imbalance), 0.50)
        elif direction == "DOWN" and imbalance < 0:
            score = min(abs(imbalance), 0.50)
        else:
            score = 0.0

        return {"score": score, "imbalance": imbalance}
