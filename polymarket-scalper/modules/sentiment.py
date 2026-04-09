"""
Sentiment Trader v5 — Trade ON news, not just avoid it.

The v4 bot only uses news for AVOIDANCE (pull orders on breaking news).
This module adds ALPHA: when news breaks that's clearly bullish/bearish
for a market, ENTER before the price fully adjusts.

Strategy:
1. Detect breaking news via RSS feeds
2. Classify sentiment (bullish/bearish/neutral) for each market
3. If sentiment is strong enough, place directional orders
4. Speed advantage: we act on the headline before most market participants
5. Decay: sentiment loses strength over time (news gets priced in)

The edge is SPEED. On Polymarket, most participants are retail.
When Reuters drops a headline, there's a 5-30 second window where
the price hasn't adjusted yet. That's our window.
"""

import re
import time
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict

import aiohttp

LOG = logging.getLogger("scalper.sentiment")


@dataclass
class SentimentSignal:
    """A sentiment signal derived from news."""
    headline: str
    source: str
    timestamp: float
    keywords_matched: List[str]
    sentiment: str  # "bullish", "bearish", "neutral"
    strength: float  # 0.0 to 1.0
    affected_markets: List[str]  # slugs of markets likely affected
    decay_rate: float = 0.5  # half-life in minutes

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def current_strength(self) -> float:
        """Exponentially decayed strength."""
        age_minutes = self.age_seconds / 60
        return self.strength * (0.5 ** (age_minutes / self.decay_rate))


class SentimentTrader:
    """
    News-driven trading engine.
    
    Two modes:
    1. DEFENSIVE (existing v4 behavior): pull orders on adverse news
    2. OFFENSIVE (new): enter positions based on bullish/bearish news
    
    Offensive mode targets:
    - Political events (elections, appointments, firings)
    - Legal outcomes (court rulings, indictments)
    - Sports results (game scores, team news)
    - Economic data (Fed decisions, employment)
    - Geopolitics (ceasefires, sanctions, wars)
    """

    # ─── News sources ───────────────────────────────────────

    RSS_FEEDS = [
        ("Reuters World", "https://feeds.reuters.com/reuters/worldNews"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("Reuters Politics", "https://feeds.reuters.com/reuters/domesticNews"),
        ("AP News", "https://rsshub.app/apnews/topics/apf-topnews"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("BBC US", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
    ]

    # ─── Sentiment keywords ─────────────────────────────────
    # Each keyword maps to (category, bullish_for, bearish_for)

    BULLISH_KEYWORDS = {
        # Political — positive for the person/party mentioned
        "wins": ("election", 0.8, []),
        "elected": ("election", 0.9, []),
        "appointed": ("political", 0.7, []),
        "confirmed": ("legal", 0.6, []),
        "approved": ("legal", 0.7, []),
        "ruling": ("legal", 0.5, []),
        "victory": ("general", 0.7, []),
        "surges": ("market", 0.6, []),
        "breakthrough": ("general", 0.7, []),
        "deal": ("geopolitics", 0.6, []),
        "agreement": ("geopolitics", 0.6, []),
        "ceasefire": ("geopolitics", 0.8, []),
        "peace": ("geopolitics", 0.7, []),
        "rallies": ("market", 0.5, []),
        "soars": ("market", 0.6, []),
        "record": ("market", 0.5, []),
    }

    BEARISH_KEYWORDS = {
        "loses": ("election", 0.8, []),
        "defeated": ("election", 0.8, []),
        "fired": ("political", 0.8, []),
        "resigns": ("political", 0.7, []),
        "indicted": ("legal", 0.8, []),
        "charged": ("legal", 0.7, []),
        "guilty": ("legal", 0.9, []),
        "convicted": ("legal", 0.9, []),
        "denied": ("legal", 0.6, []),
        "rejected": ("legal", 0.6, []),
        "crash": ("market", 0.7, []),
        "plunges": ("market", 0.7, []),
        "war": ("geopolitics", 0.7, []),
        "invasion": ("geopolitics", 0.8, []),
        "attack": ("geopolitics", 0.7, []),
        "sanctions": ("geopolitics", 0.5, []),
        "canceled": ("general", 0.5, []),
        "suspended": ("general", 0.5, []),
        "banned": ("general", 0.6, []),
        "dead": ("general", 0.8, []),
        "dies": ("general", 0.8, []),
        "killed": ("general", 0.8, []),
    }

    # ─── Market-category matching ───────────────────────────

    CATEGORY_KEYWORDS = {
        "trump": ["trump", "white house", "president", "noem", "maga", "gop"],
        "biden": ["biden", "democrat", "dnc"],
        "crypto": ["bitcoin", "btc", "ethereum", "crypto", "coinbase"],
        "tech": ["apple", "google", "openai", "meta", "tesla", "musk", "nvidia"],
        "sports_nfl": ["nfl", "super bowl", "quarterback", "touchdown", "chiefs", "eagles"],
        "sports_nba": ["nba", "lakers", "celtics", "warriors", "basketball"],
        "sports_soccer": ["soccer", "premier league", "champions league", "world cup", "fifa", "uefa"],
        "geopolitics": ["israel", "gaza", "ukraine", "russia", "china", "taiwan", "iran", "north korea"],
        "economy": ["fed", "interest rate", "inflation", "gdp", "employment", "jobs"],
        "election": ["election", "vote", "poll", "candidate", "primary", "ballot"],
        "legal": ["supreme court", "scotus", "indictment", "trial", "verdict"],
    }

    def __init__(self, head_start_seconds: float = 15.0):
        """
        Args:
            head_start_seconds: how many seconds of head start we expect
                               to have before price fully adjusts to news
        """
        self.head_start = head_start_seconds
        self.seen_headlines: Set[str] = set()
        self.signals: List[SentimentSignal] = []
        self._last_check = 0
        self._check_interval = 10  # seconds between RSS checks

        # Track signal → trade outcomes for learning
        self._signal_outcomes: List[dict] = []

    async def check_news(self, session: aiohttp.ClientSession,
                          market_questions: Dict[str, str]) -> List[SentimentSignal]:
        """
        Check RSS feeds for new sentiment signals.
        
        Args:
            session: aiohttp session
            market_questions: {slug: question} for all active markets
        
        Returns:
            List of new SentimentSignals
        """
        now = time.time()
        if now - self._last_check < self._check_interval:
            return []

        self._last_check = now
        new_signals = []

        for feed_name, url in self.RSS_FEEDS:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    headlines = self._parse_rss(text)

                    for headline in headlines:
                        if headline in self.seen_headlines:
                            continue
                        self.seen_headlines.add(headline)

                        signal = self._analyze_headline(headline, feed_name, market_questions)
                        if signal and signal.strength > 0.3:
                            new_signals.append(signal)
                            self.signals.append(signal)

                            emoji = "🟢" if signal.sentiment == "bullish" else ("🔴" if signal.sentiment == "bearish" else "⚪")
                            LOG.info(f"📰 SENTIMENT | {emoji} {signal.sentiment.upper()} ({signal.strength:.1f}) | "
                                    f"{headline[:60]} | markets: {signal.affected_markets[:3]}")

            except Exception as e:
                LOG.debug(f"News feed error ({feed_name}): {e}")

        # Prune old signals
        self.signals = [s for s in self.signals if s.current_strength > 0.05]
        self.signals = self.signals[-100:]

        # Prune old headlines
        if len(self.seen_headlines) > 500:
            self.seen_headlines = set(list(self.seen_headlines)[-200:])

        return new_signals

    def _parse_rss(self, xml_text: str) -> List[str]:
        """Parse RSS XML to extract headlines."""
        titles = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.iter("item"):
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    titles.append(title_elem.text.strip().lower())
        except ET.ParseError:
            titles = re.findall(r"<title[^>]*>([^<]+)</title>", xml_text)
            titles = [t.strip().lower() for t in titles if len(t.strip()) > 10]
        return titles[:20]

    def _analyze_headline(self, headline: str, source: str,
                            market_questions: Dict[str, str]) -> Optional[SentimentSignal]:
        """
        Analyze a headline for sentiment and market relevance.
        
        Returns a SentimentSignal if the headline is actionable.
        """
        words = set(headline.split())

        # ─── Determine sentiment ────────────────────────────

        bullish_score = 0.0
        bearish_score = 0.0
        matched_keywords = []

        for keyword, (category, strength, _) in self.BULLISH_KEYWORDS.items():
            if keyword in headline:
                bullish_score += strength
                matched_keywords.append(f"+{keyword}")

        for keyword, (category, strength, _) in self.BEARISH_KEYWORDS.items():
            if keyword in headline:
                bearish_score += strength
                matched_keywords.append(f"-{keyword}")

        if not matched_keywords:
            return None

        # Net sentiment
        if bullish_score > bearish_score * 1.3:
            sentiment = "bullish"
            strength = min(1.0, bullish_score / 2)
        elif bearish_score > bullish_score * 1.3:
            sentiment = "bearish"
            strength = min(1.0, bearish_score / 2)
        else:
            sentiment = "neutral"
            strength = 0.3

        # ─── Match to markets ───────────────────────────────

        affected_markets = []
        headline_categories = self._get_headline_categories(headline)

        for slug, question in market_questions.items():
            question_lower = question.lower()
            question_categories = self._get_headline_categories(question_lower)

            # Category overlap
            if headline_categories & question_categories:
                affected_markets.append(slug)
                continue

            # Word overlap (for non-categorized matches)
            question_words = set(question_lower.split())
            meaningful_overlap = (words & question_words) - {"the", "a", "an", "in", "on", "at", "to", "for", "of", "is", "will", "be", "by", "and", "or", "from", "with", "that", "this"}
            if len(meaningful_overlap) >= 2:
                affected_markets.append(slug)

        if not affected_markets:
            return None

        return SentimentSignal(
            headline=headline,
            source=source,
            timestamp=time.time(),
            keywords_matched=matched_keywords,
            sentiment=sentiment,
            strength=strength,
            affected_markets=affected_markets,
        )

    def _get_headline_categories(self, text: str) -> Set[str]:
        """Determine which categories a text belongs to."""
        categories = set()
        text_lower = text.lower()
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    categories.add(category)
                    break
        return categories

    def get_market_sentiment(self, slug: str, question: str) -> Tuple[str, float, str]:
        """
        Get the current net sentiment for a specific market.
        
        Returns (sentiment, strength, reason).
        Sentiment: "bullish", "bearish", or "neutral"
        """
        if not self.signals:
            return "neutral", 0.0, ""

        question_lower = question.lower()
        question_categories = self._get_headline_categories(question_lower)
        question_words = set(question_lower.split())

        total_bullish = 0.0
        total_bearish = 0.0
        strongest_signal = None

        for signal in reversed(self.signals):
            current_strength = signal.current_strength
            if current_strength < 0.1:
                continue

            headline_lower = signal.headline.lower()
            headline_words = set(headline_lower.split())

            # Check relevance
            headline_categories = self._get_headline_categories(headline_lower)
            relevant = bool(headline_categories & question_categories)

            if not relevant:
                meaningful_overlap = (headline_words & question_words) - {"the", "a", "an", "in", "on", "at", "to", "for", "of", "is", "will", "be", "by", "and", "or"}
                relevant = len(meaningful_overlap) >= 2

            if not relevant:
                continue

            if signal.sentiment == "bullish":
                total_bullish += current_strength
            elif signal.sentiment == "bearish":
                total_bearish += current_strength

            if strongest_signal is None or current_strength > strongest_signal.current_strength:
                strongest_signal = signal

        # Determine net sentiment
        net = total_bullish - total_bearish
        if abs(net) < 0.2:
            return "neutral", 0.0, ""

        if net > 0:
            return "bullish", min(1.0, net), strongest_signal.headline[:60] if strongest_signal else ""
        else:
            return "bearish", min(1.0, abs(net)), strongest_signal.headline[:60] if strongest_signal else ""

    def should_trade_on_sentiment(self, slug: str, question: str,
                                    current_price: float) -> Tuple[bool, str, float, str]:
        """
        Determine if we should enter a trade based on sentiment.
        
        Returns (should_trade, side, target_price, reason).
        
        The strategy:
        - Strong bullish sentiment → BUY YES (price will rise)
        - Strong bearish sentiment → BUY NO / SELL YES (price will fall)
        - Only trade if we have a head start advantage
        """
        sentiment, strength, headline = self.get_market_sentiment(slug, question)

        if strength < 0.4:
            return False, "", 0, ""

        # Check if signal is fresh enough (within head start window)
        for signal in reversed(self.signals):
            if signal.current_strength > 0.3:
                if slug in signal.affected_markets:
                    if signal.age_seconds < self.head_start:
                        if sentiment == "bullish":
                            # Price will likely rise — buy now
                            target = min(current_price + 0.03, 0.95)
                            return True, "BUY", target, f"BULLISH ({strength:.1f}): {headline}"
                        elif sentiment == "bearish":
                            # Price will likely fall — sell now or short
                            target = max(current_price - 0.03, 0.05)
                            return True, "SELL", target, f"BEARISH ({strength:.1f}): {headline}"

        return False, "", 0, ""

    def report(self) -> str:
        """Human-readable sentiment report."""
        if not self.signals:
            return "📰 No sentiment signals"

        active = [s for s in self.signals if s.current_strength > 0.1]
        if not active:
            return "📰 No active sentiment signals"

        lines = [f"\n📰 SENTIMENT SIGNALS ({len(active)} active)", "─" * 60]
        for s in active[-10:]:
            emoji = "🟢" if s.sentiment == "bullish" else ("🔴" if s.sentiment == "bearish" else "⚪")
            age = s.age_seconds
            lines.append(f"  {emoji} [{s.current_strength:.1f}] {s.headline[:50]}")
            lines.append(f"     {s.sentiment} | {age:.0f}s ago | {len(s.affected_markets)} markets | {s.source}")
        lines.append("─" * 60)
        return "\n".join(lines)
