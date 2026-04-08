"""
Core mathematical models for the unified scoring engine.
Each model returns a value in [0, 1] for the Score formula.
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ══════════════════════════════════════════════════════════════
# 1. Expected Value (EV) — Weight 0.35
# ══════════════════════════════════════════════════════════════

@dataclass
class EVResult:
    raw: float       # Raw EV per share (true_prob - price - fees)
    normalized: float # [0, 1] for Score formula
    passes_filter: bool


def compute_ev(
    true_prob: float,
    entry_price: float,
    fees: float = 0.005,
    min_edge: float = 0.03,
    max_edge: float = 0.20,
) -> EVResult:
    """
    Expected Value component.
    
    EV = p - entry_price - fees
    Normalized for Score: clamp(EV / max_edge, 0, 1)
    """
    raw = true_prob - entry_price - fees
    normalized = clamp(raw / max_edge, 0.0, 1.0) if max_edge > 0 else 0.0
    return EVResult(
        raw=raw,
        normalized=normalized,
        passes_filter=raw >= min_edge,
    )


# ══════════════════════════════════════════════════════════════
# 2. KL Divergence — Weight 0.20
# ══════════════════════════════════════════════════════════════

@dataclass
class KLResult:
    raw: float        # Raw KL divergence
    normalized: float # [0, 1] for Score formula
    mispricing: float # Dollar amount of mispricing


def kl_divergence_binary(p: float, q: float) -> float:
    """
    KL divergence for binary distributions.
    KL(P || Q) = p*ln(p/q) + (1-p)*ln((1-p)/(1-q))
    """
    p = clamp(p, 1e-6, 1 - 1e-6)
    q = clamp(q, 1e-6, 1 - 1e-6)
    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def compute_kl(
    market_price: float,
    related_price: float,
    relationship: str = "independent",
    threshold: float = 0.10,
) -> KLResult:
    """
    KL divergence component for arbitrage detection.
    
    Args:
        market_price: YES price in current market
        related_price: YES price in related market
        relationship: "subset" | "superset" | "independent"
                     "subset" = this market outcome implies related market outcome
        threshold: KL above this is strongly exploitable
    """
    raw_kl = 0.0
    mispricing = 0.0
    
    if relationship == "subset":
        # If this market resolves YES, related MUST resolve YES
        # Constraint: related_price >= market_price
        if related_price < market_price - 0.005:
            raw_kl = kl_divergence_binary(market_price, related_price)
            mispricing = market_price - related_price
    
    elif relationship == "superset":
        # Related market outcome implies this market outcome
        # Constraint: market_price >= related_price
        if market_price < related_price - 0.005:
            raw_kl = kl_divergence_binary(related_price, market_price)
            mispricing = related_price - market_price
    
    else:
        # Independent: check direct complement arbitrage
        raw_kl = abs(kl_divergence_binary(market_price, related_price))
    
    normalized = clamp(raw_kl / threshold, 0.0, 1.0)
    
    return KLResult(raw=raw_kl, normalized=normalized, mispricing=mispricing)


def check_complement_arb(yes_price: float, no_price: float) -> float:
    """Check if YES + NO ≠ $1. Returns arbitrage profit per share."""
    total = yes_price + no_price
    if total < 1.0 - 0.005:
        return 1.0 - total
    return 0.0


# ══════════════════════════════════════════════════════════════
# 3. Bayesian DeltaP — Weight 0.20
# ══════════════════════════════════════════════════════════════

@dataclass
class BayesianResult:
    probability: float   # Current P(YES)
    delta_p: float       # P_new - P_old
    normalized: float    # [0, 1] for Score formula
    confidence: float    # [0, 1] how confident we are


class BayesianEstimator:
    """Beta-Binomial conjugate prior for real-time probability updating."""
    
    def __init__(self, prior: float = 0.5, strength: float = 50.0):
        self.alpha = prior * strength
        self.beta = (1 - prior) * strength
        self._prob_history: List[float] = []
    
    def update(
        self,
        price_move: float,
        volume_ratio: float = 1.0,
        trade_side: int = 0,  # +1 buy, -1 sell, 0 unknown
    ):
        """Update posterior with new observation."""
        # Weight by volume spike
        vol_weight = min(volume_ratio / 2.0, 3.0)
        
        # Weight by price movement magnitude
        move_weight = abs(price_move) * 100
        
        if price_move > 0 or trade_side > 0:
            self.alpha += vol_weight * move_weight + 0.5
        elif price_move < 0 or trade_side < 0:
            self.beta += vol_weight * move_weight + 0.5
        
        self._prob_history.append(self.probability)
    
    @property
    def probability(self) -> float:
        return self.alpha / (self.alpha + self.beta)
    
    @property
    def confidence(self) -> float:
        total = self.alpha + self.beta
        return clamp(total / 100.0, 0.0, 1.0)


def compute_delta_p(
    estimator: BayesianEstimator,
    lookback: int = 20,
    delta_max: float = 0.05,
) -> BayesianResult:
    """
    Bayesian DeltaP component.
    
    ΔP = P_current - P_lookback
    Normalized: clamp(max(ΔP, 0) / delta_max, 0, 1)
    
    Only positive ΔP contributes (momentum in our favor).
    """
    prob = estimator.probability
    
    if len(estimator._prob_history) >= lookback:
        old_prob = estimator._prob_history[-lookback]
    elif estimator._prob_history:
        old_prob = estimator._prob_history[0]
    else:
        old_prob = prob
    
    delta_p = prob - old_prob
    
    # Only positive momentum counts
    normalized = clamp(max(delta_p, 0.0) / delta_max, 0.0, 1.0)
    
    return BayesianResult(
        probability=prob,
        delta_p=delta_p,
        normalized=normalized,
        confidence=estimator.confidence,
    )


# ══════════════════════════════════════════════════════════════
# 4. LMSR Edge — Weight 0.15
# ══════════════════════════════════════════════════════════════

@dataclass
class LMSRResult:
    price_before: float
    price_after: float
    slippage: float
    net_edge: float    # Favorable movement after slippage
    normalized: float  # [0, 1] for Score formula


class LMSRModel:
    """Logarithmic Market Scoring Rule for price impact estimation."""
    
    def __init__(self, b: float = 100.0):
        self.b = b
    
    def price(self, q_yes: float, q_no: float) -> float:
        ratio = clamp(q_yes / self.b, -20, 20)
        e_yes = math.exp(ratio)
        e_no = math.exp(clamp(q_no / self.b, -20, 20))
        return e_yes / (e_yes + e_no)
    
    def estimate_b_from_spread(self, spread: float, mid: float) -> float:
        """Estimate liquidity parameter from observed spread."""
        if spread <= 0 or mid <= 0 or mid >= 1:
            return 200.0
        # For small spreads near any price: spread ≈ trade_size / b * price * (1-price)
        # Rough: b ≈ 1 / (spread * 4) for mid=0.5
        return max(10.0, 1.0 / (spread * 4 + 1e-6))
    
    def compute_impact(
        self,
        current_price: float,
        trade_size: float,
        b: Optional[float] = None,
    ) -> LMSRResult:
        """
        Estimate price movement from a trade.
        
        Args:
            current_price: Current YES price (0-1)
            trade_size: Dollar amount to trade
            b: Liquidity parameter (auto-estimated if None)
        """
        if b is None:
            b = self.b
        
        # Derive q_yes, q_no from current price
        if current_price <= 0 or current_price >= 1:
            return LMSRResult(current_price, current_price, 0, 0, 0)
        
        log_ratio = clamp(math.log(current_price / (1 - current_price)), -10, 10)
        q_yes = b * log_ratio / 2  # Approximate
        q_no = -b * log_ratio / 2
        
        # Shares bought
        shares = trade_size / current_price if current_price > 0 else 0
        
        # Price after buy
        q_yes_new = q_yes + shares
        price_after = self.price(q_yes_new, q_no)
        
        slippage = price_after - current_price
        
        # Net edge: if price moves in our favor (up for YES buyer)
        # AND our true probability is above the AFTER price
        # The favorable movement is the slippage itself
        net_edge = max(slippage, 0)  # We benefit from price going up after we buy
        
        normalized = clamp(net_edge / 0.02, 0.0, 1.0)  # 2% max expected
        
        return LMSRResult(
            price_before=current_price,
            price_after=price_after,
            slippage=slippage,
            net_edge=net_edge,
            normalized=normalized,
        )


# ══════════════════════════════════════════════════════════════
# 5. Stoikov Risk — Weight -0.10 (PENALTY)
# ══════════════════════════════════════════════════════════════

@dataclass
class StoikovResult:
    reservation_price: float
    deviation: float     # |mid - reservation|
    spread_risk: float   # Normalized spread risk
    total_risk: float    # Combined risk metric
    normalized: float    # [0, 1] for Score formula


def compute_stoikov_risk(
    mid_price: float,
    best_bid: float,
    best_ask: float,
    position: float = 0.0,
    volatility: float = 0.05,
    gamma: float = 0.5,
    risk_max: float = 0.05,
    max_spread: float = 0.03,
) -> StoikovResult:
    """
    Stoikov risk component (PENALTY — subtracted from Score).
    
    Higher risk → lower score → less likely to trade.
    """
    # Reservation price (inventory-adjusted fair value)
    reservation = mid_price - position * gamma * (volatility ** 2)
    
    # Deviation from fair value
    deviation = abs(mid_price - reservation)
    
    # Spread risk
    spread = best_ask - best_bid
    spread_risk = clamp(spread / max_spread, 0.0, 1.0)
    
    # Combined: 70% inventory risk + 30% spread risk
    inventory_risk = clamp(deviation / risk_max, 0.0, 1.0)
    total_risk = 0.7 * inventory_risk + 0.3 * spread_risk
    
    return StoikovResult(
        reservation_price=reservation,
        deviation=deviation,
        spread_risk=spread_risk,
        total_risk=total_risk,
        normalized=total_risk,  # Higher = worse
    )


# ══════════════════════════════════════════════════════════════
# UNIFIED SCORE
# ══════════════════════════════════════════════════════════════

@dataclass
class ScoreResult:
    ev: float
    kl: float
    delta_p: float
    lmsr: float
    risk: float
    total: float
    
    # Component details
    ev_detail: Optional[EVResult] = None
    kl_detail: Optional[KLResult] = None
    bayesian_detail: Optional[BayesianResult] = None
    lmsr_detail: Optional[LMSRResult] = None
    stoikov_detail: Optional[StoikovResult] = None
    
    # Decision
    should_trade: bool = False
    reasons: List[str] = field(default_factory=list)


def compute_score(
    ev_norm: float,
    kl_norm: float,
    delta_p_norm: float,
    lmsr_norm: float,
    risk_norm: float,
    ev_raw: float = 0.0,
    spread: float = 0.0,
    liquidity: float = 0.0,
    market_age_seconds: float = 999,
    daily_trades: int = 0,
    drawdown: float = 0.0,
    threshold: float = 0.65,
) -> ScoreResult:
    """
    THE UNIFIED DECISION FUNCTION.
    
    Score = 0.35*EV + 0.20*KL + 0.20*ΔP + 0.15*LMSR - 0.10*Risk
    
    Trade if Score > threshold AND all hard filters pass.
    """
    # ── Calculate Score ──
    score = (
        0.35 * ev_norm
        + 0.20 * kl_norm
        + 0.20 * delta_p_norm
        + 0.15 * lmsr_norm
        - 0.10 * risk_norm
    )
    
    # ── Hard Filters ──
    reasons = []
    filters_pass = True
    
    if ev_raw < 0.03:
        filters_pass = False
        reasons.append(f"EV too low: {ev_raw:.4f} < 0.03")
    
    if spread > 0.03:
        filters_pass = False
        reasons.append(f"Spread too wide: {spread:.4f} > 0.03")
    
    if liquidity < 10000:
        filters_pass = False
        reasons.append(f"Liquidity too low: ${liquidity:.0f} < $10K")
    
    if market_age_seconds > 300:  # No trade in 5 min
        reasons.append(f"Warning: no recent trades ({market_age_seconds:.0f}s)")
    
    if daily_trades >= 20:
        filters_pass = False
        reasons.append(f"Daily trade limit: {daily_trades} >= 20")
    
    if drawdown >= 0.15:
        filters_pass = False
        reasons.append(f"Drawdown limit: {drawdown:.1%} >= 15%")
    
    should_trade = score > threshold and filters_pass
    
    if should_trade:
        reasons.append(f"SCORE PASS: {score:.4f} > {threshold}")
    
    return ScoreResult(
        ev=ev_norm,
        kl=kl_norm,
        delta_p=delta_p_norm,
        lmsr=lmsr_norm,
        risk=risk_norm,
        total=score,
        should_trade=should_trade,
        reasons=reasons,
    )
