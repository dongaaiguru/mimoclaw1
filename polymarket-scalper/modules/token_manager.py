"""
Token Manager v5 — Live token splitting and inventory management.

Fixes the broken ensure_sell_tokens() in bot.py v4 which had a hard-coded
`return False` for live mode. This module:

1. Tracks token inventory from fills + splits
2. Calls the CTF contract to split USDC into YES + NO token pairs
3. Auto-splits when SELL orders need tokens we don't own
4. Handles batch splits for efficiency (split once, sell many)

The Polymarket CLOB requires owning outcome tokens to place SELL orders.
To sell YES tokens, you must first own them — either from a BUY fill or
from splitting USDC via the CTF (Conditional Token Framework) contract.
"""

import logging
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("scalper.token_manager")


@dataclass
class TokenHolding:
    """Track a single token holding."""
    token_id: str
    slug: str
    amount: float = 0.0
    cost_basis: float = 0.0  # USDC spent to acquire these tokens
    acquired_at: float = field(default_factory=time.time)


class TokenManager:
    """
    Manages outcome token inventory for Polymarket CLOB trading.
    
    In Polymarket's CTF model:
    - Splitting USDC creates YES + NO token pairs (1 USDC → 1 YES + 1 NO)
    - BUY fills give you YES tokens (you paid USDC, received YES tokens)
    - SELL orders require you to already own the tokens you're selling
    - Merging YES + NO tokens back recovers USDC (1 YES + 1 NO → 1 USDC)
    
    This manager tracks all of the above and auto-splits when needed.
    """

    def __init__(self, client=None, paper: bool = True):
        self.client = client  # py-clob-client ClobClient instance
        self.paper = paper
        # token_id → TokenHolding
        self._holdings: Dict[str, TokenHolding] = {}
        # slug → {yes_token, no_token} mapping
        self._token_map: Dict[str, dict] = {}
        # Track total USDC spent on splits
        self._total_split_cost: float = 0.0
        # Track pending split requests (batch optimization)
        self._pending_splits: Dict[str, float] = {}  # condition_id → amount

    def register_market(self, slug: str, yes_token: str, no_token: str,
                         condition_id: str = ""):
        """Register a market's token IDs for inventory tracking."""
        self._token_map[slug] = {
            "yes_token": yes_token,
            "no_token": no_token,
            "condition_id": condition_id,
        }
        # Initialize holdings if not exists
        if yes_token not in self._holdings:
            self._holdings[yes_token] = TokenHolding(yes_token, slug)
        if no_token not in self._holdings:
            self._holdings[no_token] = TokenHolding(no_token, slug)

    def get_balance(self, token_id: str) -> float:
        """Get the number of tokens held for a given token ID."""
        h = self._holdings.get(token_id)
        return h.amount if h else 0.0

    def get_slug_balances(self, slug: str) -> Tuple[float, float]:
        """Get (yes_balance, no_balance) for a market."""
        info = self._token_map.get(slug, {})
        yes_bal = self.get_balance(info.get("yes_token", ""))
        no_bal = self.get_balance(info.get("no_token", ""))
        return yes_bal, no_bal

    def credit_from_buy(self, token_id: str, shares: float, cost: float):
        """
        Credit tokens from a BUY fill.
        When we BUY YES tokens, we receive `shares` YES tokens.
        The cost is the USDC we paid.
        """
        if token_id not in self._holdings:
            # Find slug from token map
            slug = ""
            for s, info in self._token_map.items():
                if info.get("yes_token") == token_id or info.get("no_token") == token_id:
                    slug = s
                    break
            self._holdings[token_id] = TokenHolding(token_id, slug)

        h = self._holdings[token_id]
        h.amount += shares
        h.cost_basis += cost
        LOG.debug(f"📥 CREDIT BUY | {shares:.0f} tokens @ ${cost/shares:.4f} | {token_id[:16]}...")

    def debit_from_sell(self, token_id: str, shares: float) -> bool:
        """
        Debit tokens from a SELL fill.
        Returns True if we had enough tokens, False otherwise (shouldn't happen
        if ensure_sell_tokens was called first).
        """
        h = self._holdings.get(token_id)
        if not h or h.amount < shares:
            LOG.warning(f"⚠️ UNDERSELL | Have {h.amount if h else 0:.0f}, need {shares:.0f} | {token_id[:16]}...")
            return False

        # Calculate cost basis for PnL tracking
        if h.amount > 0:
            cost_ratio = shares / h.amount
            sold_basis = h.cost_basis * cost_ratio
        else:
            sold_basis = 0

        h.amount -= shares
        h.cost_basis -= sold_basis
        if h.amount < 0.01:
            h.amount = 0
            h.cost_basis = 0

        LOG.debug(f"📤 DEBIT SELL | {shares:.0f} tokens | {token_id[:16]}...")
        return True

    def credit_from_split(self, yes_token: str, no_token: str, amount: float):
        """
        Credit tokens from a USDC split.
        Splitting X USDC creates X YES tokens + X NO tokens.
        """
        if yes_token not in self._holdings:
            self._holdings[yes_token] = TokenHolding(yes_token, "")
        if no_token not in self._holdings:
            self._holdings[no_token] = TokenHolding(no_token, "")

        self._holdings[yes_token].amount += amount
        self._holdings[yes_token].cost_basis += amount
        self._holdings[no_token].amount += amount
        self._holdings[no_token].cost_basis += 0  # NO tokens are "free" side of split
        self._total_split_cost += amount

        LOG.info(f"✂️ SPLIT CREDIT | {amount:.0f} USDC → {amount:.0f} YES + {amount:.0f} NO tokens")

    async def ensure_sell_tokens(self, slug: str, sell_token: str,
                                  shares_needed: float,
                                  free_capital: float,
                                  yes_token: str = "",
                                  no_token: str = "") -> Tuple[bool, float]:
        """
        Ensure we have enough tokens to place a SELL order.
        
        Returns (success, actual_shares_available).
        If we can't get enough tokens, returns (False, 0).
        """
        current = self.get_balance(sell_token)
        if current >= shares_needed:
            return True, shares_needed

        deficit = shares_needed - current

        # Check if we need to split USDC
        split_cost = deficit  # 1 USDC → 1 token pair

        if split_cost > free_capital:
            # Can't afford full split — what can we do?
            affordable = free_capital
            if affordable < 1:
                LOG.warning(f"⚠️ No capital for split | need ${split_cost:.2f}, free=${free_capital:.2f}")
                return False, current if current > 0 else 0

            LOG.warning(f"⚠️ Partial split | need ${split_cost:.2f}, can afford ${affordable:.2f}")
            deficit = affordable
            split_cost = affordable

        # Execute the split
        if self.paper:
            # Paper mode: just credit the tokens
            # Find the paired token
            info = self._token_map.get(slug, {})
            paired_token = info.get("no_token", "") if sell_token == info.get("yes_token") else info.get("yes_token", "")
            if not paired_token:
                paired_token = no_token if sell_token == yes_token else yes_token

            self.credit_from_split(sell_token, paired_token, deficit)
            LOG.info(f"✂️ PAPER SPLIT | ${split_cost:.2f} → {deficit:.0f} tokens | {slug[:35]}")
            return True, current + deficit

        # Live mode: call the CTF split
        return await self._live_split(slug, sell_token, deficit, split_cost)

    async def _live_split(self, slug: str, sell_token: str,
                           amount: float, cost: float) -> Tuple[bool, float]:
        """
        Execute a live CTF split on Polygon.
        
        The CTF (Conditional Token Framework) contract has a `splitPosition` function
        that takes USDC and splits it into outcome tokens.
        
        We use py-clob-client which wraps this interaction.
        """
        if not self.client:
            LOG.error("❌ No client for live split")
            return False, 0

        info = self._token_map.get(slug, {})
        condition_id = info.get("condition_id", "")

        try:
            # Method 1: Use py-clob-client's split_position if available
            if hasattr(self.client, 'split_position'):
                LOG.info(f"✂️ LIVE SPLIT (client.split_position) | ${cost:.2f} | {slug[:35]}")
                resp = self.client.split_position(
                    condition_id=condition_id,
                    amount=cost,
                )
                if resp:
                    yes_token = info.get("yes_token", "")
                    no_token = info.get("no_token", "")
                    self.credit_from_split(yes_token, no_token, amount)
                    LOG.info(f"✅ SPLIT SUCCESS | {amount:.0f} tokens | tx={resp}")
                    return True, self.get_balance(sell_token)

            # Method 2: Use CTF contract directly via web3
            # The splitPosition function signature:
            # splitPosition(address collateralToken, bytes32 parentCollectionId,
            #               bytes32 conditionId, uint[] partition, uint amount)
            LOG.warning(f"⚠️ No split_position method on client. Falling back to market buy.")
            
            # Method 3 (fallback): Buy the tokens via market order
            # This is less efficient (crosses the spread) but guaranteed to work
            try:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY
                
                # Get current best ask
                book = self.client.get_order_book(sell_token)
                if book and hasattr(book, 'asks') and book.asks:
                    best_ask = float(book.asks[0].price)
                    best_ask_size = float(book.asks[0].size)
                    
                    # Buy at best ask (taker order)
                    buy_shares = min(amount, best_ask_size)
                    buy_cost = buy_shares * best_ask
                    
                    if buy_cost > cost:
                        buy_shares = cost / best_ask
                        buy_cost = cost
                    
                    order_args = OrderArgs(
                        token_id=sell_token,
                        price=best_ask,
                        size=buy_shares,
                        side=BUY,
                    )
                    signed = self.client.create_order(order_args)
                    resp = self.client.post_order(signed)
                    
                    if resp:
                        self.credit_from_split(sell_token, "", buy_shares)
                        LOG.info(f"✅ MARKET BUY (split fallback) | {buy_shares:.0f} @ ${best_ask:.4f}")
                        return True, self.get_balance(sell_token)
                        
            except Exception as e2:
                LOG.error(f"Market buy fallback failed: {e2}")

            LOG.error(f"❌ All split methods failed for {slug[:35]}")
            return False, self.get_balance(sell_token)

        except Exception as e:
            LOG.error(f"❌ Live split error: {e}")
            return False, self.get_balance(sell_token)

    async def batch_ensure_tokens(self, requirements: Dict[str, float],
                                    free_capital: float) -> Dict[str, bool]:
        """
        Batch-ensure tokens for multiple SELL orders at once.
        More efficient than individual splits.
        
        requirements: {slug: shares_needed}
        Returns: {slug: success}
        """
        # Calculate total split cost needed
        total_needed = 0
        per_market = {}
        
        for slug, needed in requirements.items():
            info = self._token_map.get(slug, {})
            yes_token = info.get("yes_token", "")
            current = self.get_balance(yes_token)
            deficit = max(0, needed - current)
            per_market[slug] = {
                "yes_token": yes_token,
                "no_token": info.get("no_token", ""),
                "current": current,
                "deficit": deficit,
                "needed": needed,
            }
            total_needed += deficit

        if total_needed > free_capital:
            # Proportional allocation
            ratio = free_capital / total_needed if total_needed > 0 else 0
            for slug in per_market:
                per_market[slug]["deficit"] *= ratio
            total_needed = free_capital

        # Execute splits
        results = {}
        for slug, info in per_market.items():
            if info["deficit"] > 0.5:  # Only split if meaningful amount
                success, _ = await self.ensure_sell_tokens(
                    slug, info["yes_token"], info["needed"],
                    free_capital=info["deficit"],
                    no_token=info["no_token"],
                )
                results[slug] = success
            else:
                results[slug] = info["current"] >= info["needed"]

        return results

    def merge_tokens(self, slug: str, amount: float) -> float:
        """
        Merge YES + NO tokens back to USDC.
        Returns the USDC recovered.
        
        Used when we want to free up capital from unsold token pairs.
        """
        info = self._token_map.get(slug, {})
        yes_token = info.get("yes_token", "")
        no_token = info.get("no_token", "")

        yes_bal = self.get_balance(yes_token)
        no_bal = self.get_balance(no_token)
        mergeable = min(amount, yes_bal, no_bal)

        if mergeable < 0.01:
            return 0.0

        if self.paper:
            self._holdings[yes_token].amount -= mergeable
            self._holdings[yes_token].cost_basis -= mergeable
            self._holdings[no_token].amount -= mergeable
            LOG.info(f"🔄 PAPER MERGE | {mergeable:.0f} YES + {mergeable:.0f} NO → {mergeable:.0f} USDC | {slug[:35]}")
            return mergeable

        # Live: call CTF merge
        if self.client and hasattr(self.client, 'merge_positions'):
            try:
                resp = self.client.merge_positions(
                    condition_id=info.get("condition_id", ""),
                    amount=mergeable,
                )
                if resp:
                    self._holdings[yes_token].amount -= mergeable
                    self._holdings[yes_token].cost_basis -= mergeable
                    self._holdings[no_token].amount -= mergeable
                    LOG.info(f"✅ LIVE MERGE | {mergeable:.0f} tokens → USDC | tx={resp}")
                    return mergeable
            except Exception as e:
                LOG.error(f"Merge error: {e}")

        return 0.0

    def get_inventory_report(self) -> str:
        """Generate a human-readable inventory report."""
        lines = ["\n📦 TOKEN INVENTORY", "─" * 50]
        total_value = 0
        for token_id, h in self._holdings.items():
            if h.amount > 0.01:
                avg_cost = h.cost_basis / h.amount if h.amount > 0 else 0
                lines.append(f"  {h.slug[:25]:<25} | {h.amount:>8.0f} tokens | basis=${h.cost_basis:.2f} (${avg_cost:.4f}/ea)")
                total_value += h.cost_basis
        lines.append(f"  {'TOTAL COST BASIS':<25} | ${total_value:.2f}")
        lines.append(f"  {'TOTAL SPLIT COST':<25} | ${self._total_split_cost:.2f}")
        lines.append("─" * 50)
        return "\n".join(lines)
