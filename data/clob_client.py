"""
Direct REST client for the Polymarket CLOB API.
No dependency on py-clob-client, only eth_account + requests.

Auth Level 1: EIP-712 ClobAuth message (private key signature)
Auth Level 2: HMAC-SHA256 (API secret signature)
Order signing: EIP-712 CTF Exchange Order struct
"""

from __future__ import annotations
import hashlib
import hmac as _hmac
import base64
import json
import random
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional, Any

import requests
from eth_account import Account

from config.settings import Settings
from data.models import MarketOrderBooks, Order, OrderBook, OrderBookLevel, OrderStatus, Side
from observability import logger as log


# Polymarket contract configuration

EXCHANGE_ADDRESS   = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polygon mainnet
ZERO_ADDRESS       = "0x0000000000000000000000000000000000000000"
CLOB_DOMAIN_NAME   = "ClobAuthDomain"
CLOB_VERSION       = "1"
MSG_TO_SIGN        = "This message attests that I control the given wallet"
USDC_DECIMALS      = 6   # USDC has 6 decimals
CTF_DECIMALS       = 6   # Conditional Token has 6 decimals

# Signature type: 0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE
EOA_SIG_TYPE = 0


# EIP-712 domain and types

CLOB_AUTH_DOMAIN = {
    "name": CLOB_DOMAIN_NAME,
    "version": CLOB_VERSION,
}

CLOB_AUTH_TYPES = {
    "ClobAuth": [
        {"name": "address",   "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce",     "type": "uint256"},
        {"name": "message",   "type": "string"},
    ]
}

CTF_EXCHANGE_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,
    "verifyingContract": EXCHANGE_ADDRESS,
}

CTF_ORDER_TYPES = {
    "Order": [
        {"name": "salt",          "type": "uint256"},
        {"name": "maker",         "type": "address"},
        {"name": "signer",        "type": "address"},
        {"name": "taker",         "type": "address"},
        {"name": "tokenId",       "type": "uint256"},
        {"name": "makerAmount",   "type": "uint256"},
        {"name": "takerAmount",   "type": "uint256"},
        {"name": "expiration",    "type": "uint256"},
        {"name": "nonce",         "type": "uint256"},
        {"name": "feeRateBps",    "type": "uint256"},
        {"name": "side",          "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}


@dataclass
class ApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


# Signing helpers

def _sign_eip712(private_key: str, domain: dict, types: dict, message: dict) -> str:
    """
    Sign an EIP-712 message with the private key.
    Use the full_message format for compatibility with eth_account >= 0.9.
    """
    account = Account.from_key(private_key)
    primary_type = next(k for k in types if k != "EIP712Domain")

    # Build EIP712Domain types from the domain dict
    domain_fields = []
    field_types = {"name": "string", "version": "string",
                   "chainId": "uint256", "verifyingContract": "address"}
    for key in domain:
        if key in field_types:
            domain_fields.append({"name": key, "type": field_types[key]})

    full_message = {
        "types": {
            "EIP712Domain": domain_fields,
            **types,
        },
        "domain": domain,
        "primaryType": primary_type,
        "message": message,
    }

    signed = account.sign_typed_data(full_message=full_message)
    return signed.signature.hex()


def _sign_clob_auth(private_key: str, chain_id: int, timestamp: int, nonce: int) -> str:
    """Generate the Level 1 signature for CLOB API authentication."""
    account = Account.from_key(private_key)
    domain = {**CLOB_AUTH_DOMAIN, "chainId": chain_id}
    message = {
        "address": account.address,
        "timestamp": str(timestamp),
        "nonce": nonce,
        "message": MSG_TO_SIGN,
    }
    return _sign_eip712(private_key, domain, CLOB_AUTH_TYPES, message)


def _build_hmac(secret: str, timestamp: int, method: str,
                request_path: str, body: Any = None) -> str:
    """Generate the Level 2 HMAC-SHA256 signature."""
    base64_secret = base64.urlsafe_b64decode(secret)
    msg = str(timestamp) + str(method) + str(request_path)
    if body:
        msg += str(body).replace("'", '"')
    digest = _hmac.new(base64_secret, msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _build_order_payload(
    private_key: str,
    token_id: str,
    side: Side,         # YES=BUY / NO=BUY (Polymarket always uses BUY on the specific token)
    price: float,       # 0.xx
    size: float,        # number of shares
    nonce: int = 0,
    expiration: int = 0,
    fee_rate_bps: int = 0,
) -> dict:
    """
    Build and sign a limit order for the CTF Exchange.

    On Polymarket, each token (YES or NO) is a separate Conditional Token.
    BUY YES @ 0.49 = pay 0.49 USDC for 1 YES share.
    makerAmount = USDC to pay = price * size * 10^6
    takerAmount = shares to receive = size * 10^6
    """
    account = Account.from_key(private_key)
    address = account.address

    salt = random.randint(0, 10**18)

    # Convert to integer units (6 decimals)
    maker_amount = int(round(price * size * 10**USDC_DECIMALS))
    taker_amount = int(round(size * 10**CTF_DECIMALS))

    side_int = 0  # 0 = BUY on the Polymarket exchange

    order_data = {
        "salt":          salt,
        "maker":         address,
        "signer":        address,
        "taker":         ZERO_ADDRESS,
        "tokenId":       int(token_id) if token_id.isdigit() else 0,
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "expiration":    expiration,
        "nonce":         nonce,
        "feeRateBps":    fee_rate_bps,
        "side":          side_int,
        "signatureType": EOA_SIG_TYPE,
    }

    signature = _sign_eip712(private_key, CTF_EXCHANGE_DOMAIN, CTF_ORDER_TYPES, order_data)

    # Payload format for POST /order
    return {
        "salt":          str(salt),
        "maker":         address,
        "signer":        address,
        "taker":         ZERO_ADDRESS,
        "tokenId":       str(order_data["tokenId"]),
        "makerAmount":   str(maker_amount),
        "takerAmount":   str(taker_amount),
        "expiration":    str(expiration),
        "nonce":         str(nonce),
        "feeRateBps":    str(fee_rate_bps),
        "side":          str(side_int),
        "signatureType": str(EOA_SIG_TYPE),
        "signature":     signature,
    }


# Main client

class PolymarketClient:
    """REST client for the Polymarket CLOB API with no private dependencies."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._address: str = ""
        self._creds: Optional[ApiCreds] = None
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._consecutive_api_errors = 0
        self._last_api_error = ""
        self._fee_rate_cache: dict[str, int] = {}

    def connect(self) -> bool:
        """Authenticate the wallet and obtain/validate API credentials."""
        try:
            pk = self.settings.wallet.private_key
            account = Account.from_key(pk)
            self._address = account.address
            log.info(f"wallet loaded: {self._address[:10]}...")

            if self.settings.wallet.api_key:
                # Use credentials from .env
                self._creds = ApiCreds(
                    api_key=self.settings.wallet.api_key,
                    api_secret=self.settings.wallet.api_secret,
                    api_passphrase=self.settings.wallet.api_passphrase,
                )
                log.ok(f"API creds from .env - key={self._creds.api_key[:8]}...")
            else:
                # Derive credentials from the private key
                self._creds = self._derive_api_creds()
                if not self._creds:
                    return False

            self._record_api_success()
            log.ok(f"connected to CLOB API - {self._address[:10]}...")
            return True
        except Exception as e:
            self._record_api_error("connect", e)
            return False

    def _derive_api_creds(self) -> Optional[ApiCreds]:
        """Call /auth/derive-api-key with a Level 1 signature."""
        try:
            pk = self.settings.wallet.private_key
            chain_id = self.settings.wallet.chain_id
            timestamp = int(time.time())
            nonce = 0

            signature = _sign_clob_auth(pk, chain_id, timestamp, nonce)
            headers = {
                "POLY_ADDRESS":   self._address,
                "POLY_SIGNATURE": signature,
                "POLY_TIMESTAMP": str(timestamp),
                "POLY_NONCE":     str(nonce),
                "Content-Type":   "application/json",
            }

            resp = self._session.get(
                f"{self.settings.api.clob_base_url}/auth/derive-api-key",
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()

            creds = ApiCreds(
                api_key=data["apiKey"],
                api_secret=data["secret"],
                api_passphrase=data["passphrase"],
            )
            log.ok(f"derived API creds - key={creds.api_key[:8]}...")
            log.warn("Save the derived API creds in .env: CLOB_API_KEY / CLOB_API_SECRET / CLOB_API_PASSPHRASE")
            log.warn("secret and passphrase are not printed in logs")
            return creds
        except Exception as e:
            self._record_api_error("derive_api_creds", e)
            return None

    def _l2_headers(self, method: str, path: str, body: Any = None) -> dict:
        """Genera headers HMAC Level 2 per chiamate autenticate."""
        if not self._creds:
            return {}
        timestamp = int(time.time())
        signature = _build_hmac(
            self._creds.api_secret, timestamp, method, path, body
        )
        return {
            "POLY_ADDRESS":   self._address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_API_KEY":   self._creds.api_key,
            "POLY_PASSPHRASE": self._creds.api_passphrase,
            "Content-Type":   "application/json",
        }

    def get_address(self) -> str:
        return self._address

    def get_ws_auth(self) -> Optional[dict]:
        if not self._creds:
            return None
        return {
            "apiKey": self._creds.api_key,
            "secret": self._creds.api_secret,
            "passphrase": self._creds.api_passphrase,
        }

    @property
    def consecutive_api_errors(self) -> int:
        return self._consecutive_api_errors

    @property
    def last_api_error(self) -> str:
        return self._last_api_error

    def should_trigger_kill_switch(self) -> bool:
        return self._consecutive_api_errors >= self.settings.api.max_consecutive_api_errors

    # Orderbook (public, no auth)

    def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch the orderbook for an outcome token."""
        try:
            resp = self._session.get(
                f"{self.settings.api.clob_base_url}/book",
                params={"token_id": token_id},
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            raw = resp.json()
            book = OrderBook()

            for bid in raw.get("bids", []):
                book.yes_bids.append(OrderBookLevel(
                    price=float(bid["price"]), size=float(bid["size"])
                ))
            for ask in raw.get("asks", []):
                book.yes_asks.append(OrderBookLevel(
                    price=float(ask["price"]), size=float(ask["size"])
                ))

            book.yes_bids.sort(key=lambda x: x.price, reverse=True)
            book.yes_asks.sort(key=lambda x: x.price)
            self._record_api_success()
            return book
        except Exception as e:
            self._record_api_error(f"orderbook {str(token_id)[:10]}...", e)
            return OrderBook()

    def get_market_books(self, token_id_yes: str, token_id_no: str) -> MarketOrderBooks:
        """Fetch both orderbooks for a market."""
        books = MarketOrderBooks()
        if token_id_yes:
            books.yes_book = self.get_orderbook(token_id_yes)
        if token_id_no:
            books.no_book = self.get_orderbook(token_id_no)
        return books

    def get_fee_rate(self, token_id: str, use_cache: bool = True) -> Optional[int]:
        """Fetch the fee rate in basis points for an outcome token."""
        token_id = str(token_id or "")
        if not token_id:
            return 0

        if use_cache and token_id in self._fee_rate_cache:
            return self._fee_rate_cache[token_id]

        try:
            resp = self._session.get(
                f"{self.settings.api.clob_base_url}/fee-rate",
                params={"token_id": token_id},
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            fee_rate_bps = self._extract_fee_rate_bps(resp.json())
            if fee_rate_bps is None:
                raise ValueError("fee rate could not be read from the response")
            self._fee_rate_cache[token_id] = fee_rate_bps
            self._record_api_success()
            return fee_rate_bps
        except Exception as e:
            self._record_api_error(f"get_fee_rate {token_id[:10]}...", e)
            return None

    # Orders (authenticated)

    def place_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        dry_run: bool = False,
    ) -> Optional[Order]:
        """Place a limit order."""
        if dry_run:
            log.info(f"[DRY RUN] order {side.value} @ {price:.2f} x{size:.1f}")
            return Order(
                order_id=f"dry_{uuid.uuid4().hex[:12]}",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.LIVE,
            )

        try:
            fee_rate_bps = self.get_fee_rate(token_id)
            if fee_rate_bps is None:
                log.err(f"unable to determine fee rate for token {token_id[:10]}...")
                return None
            if not self._fee_rate_allowed(fee_rate_bps):
                log.err(f"fee rate {fee_rate_bps}bps not allowed for token {token_id[:10]}...")
                return None

            payload = _build_order_payload(
                private_key=self.settings.wallet.private_key,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                fee_rate_bps=fee_rate_bps,
            )

            body_str = json.dumps(payload)
            headers = self._l2_headers("POST", "/order", body_str)

            resp = self._session.post(
                f"{self.settings.api.clob_base_url}/order",
                data=body_str,
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()

            order_id = data.get("orderID", data.get("id", ""))
            if order_id:
                self._record_api_success()
                log.ok(f"order {side.value} @ {price*100:.0f}c x{size:.1f} id={order_id[:8]}")
                return Order(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    status=OrderStatus.LIVE,
                    fee_rate_bps=fee_rate_bps,
                )
            else:
                log.warn(f"response without orderID: {data}")
                return None
        except requests.exceptions.HTTPError as e:
            detail = e.response.text[:200] if e.response is not None else str(e)
            self._record_api_error(f"place_order HTTP {e.response.status_code if e.response else '?'}", detail)
            return None
        except Exception as e:
            self._record_api_error(f"place_order {side.value} @ {price:.2f}", e)
            return None

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        """Cancel a specific order."""
        if dry_run:
            log.info(f"[DRY RUN] cancelled order {order_id[:8]}")
            return True
        try:
            path = f"/order/{order_id}"
            headers = self._l2_headers("DELETE", path)
            resp = self._session.delete(
                f"{self.settings.api.clob_base_url}{path}",
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            self._record_api_success()
            log.info(f"cancelled order {order_id[:8]}")
            return True
        except Exception as e:
            self._record_api_error(f"cancel_order {order_id[:8]}", e)
            return False

    def cancel_all(self, dry_run: bool = False) -> bool:
        """Cancel all open orders."""
        if dry_run:
            log.info("[DRY RUN] cancelled all orders")
            return True
        try:
            headers = self._l2_headers("DELETE", "/cancel-all")
            resp = self._session.delete(
                f"{self.settings.api.clob_base_url}/cancel-all",
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            self._record_api_success()
            log.info("cancelled all orders")
            return True
        except Exception as e:
            self._record_api_error("cancel_all", e)
            return False

    def get_open_orders(self) -> Optional[list[dict]]:
        """Fetch the wallet's open orders."""
        try:
            headers = self._l2_headers("GET", "/orders")
            resp = self._session.get(
                f"{self.settings.api.clob_base_url}/orders",
                params={"maker": self._address},
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            self._record_api_success()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            self._record_api_error("get_open_orders", e)
            return None

    def get_trades(self) -> list[dict]:
        """Fetch recent wallet trades."""
        try:
            headers = self._l2_headers("GET", "/trades")
            resp = self._session.get(
                f"{self.settings.api.clob_base_url}/trades",
                params={"maker": self._address},
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            self._record_api_success()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            self._record_api_error("get_trades", e)
            return []

    def get_balance_allowance(
        self,
        asset_type: str,
        token_id: str = "",
        signature_type: int = EOA_SIG_TYPE,
    ) -> Optional[dict]:
        """Recupera balance/allowance dal CLOB per collateral o conditional tokens."""
        try:
            path = "/balance-allowance"
            headers = self._l2_headers("GET", path)
            params: dict[str, Any] = {
                "asset_type": asset_type,
                "signature_type": signature_type,
            }
            if token_id:
                params["token_id"] = token_id

            resp = self._session.get(
                f"{self.settings.api.clob_base_url}{path}",
                params=params,
                headers=headers,
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            self._record_api_success()
            return resp.json()
        except Exception as e:
            self._record_api_error(f"get_balance_allowance {asset_type}", e)
            return None

    def get_positions(self, user: str = "") -> Optional[list[dict]]:
        """Fetch leftover positions from the Data API."""
        try:
            wallet = user or self._address
            resp = self._session.get(
                f"{self.settings.api.data_api_base_url}/positions",
                params={"user": wallet},
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            self._record_api_success()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            self._record_api_error("get_positions", e)
            return None

    def _record_api_success(self) -> None:
        self._consecutive_api_errors = 0
        self._last_api_error = ""

    def _record_api_error(self, operation: str, error: Any) -> None:
        self._consecutive_api_errors += 1
        self._last_api_error = f"{operation}: {error}"
        log.err(self._last_api_error)

    def _fee_rate_allowed(self, fee_rate_bps: int) -> bool:
        if fee_rate_bps < 0:
            return False
        if not self.settings.trading.allow_fee_enabled_markets and fee_rate_bps > 0:
            return False
        return fee_rate_bps <= self.settings.trading.max_allowed_fee_rate_bps

    def _extract_fee_rate_bps(self, raw_value: Any) -> Optional[int]:
        # Chiavi con "bps" esplicito (valore gia' in basis points)
        bps_keys = {
            "feeratebps",
            "fee_rate_bps",
            "makerfeeratebps",
            "maker_fee_rate_bps",
            "ratebps",
            "rate_bps",
        }
        found = self._find_first_scalar(raw_value, bps_keys)
        if found not in (None, ""):
            try:
                return int(float(found))
            except (TypeError, ValueError):
                pass

        # Chiavi senza "bps" — Polymarket restituisce spesso "feeRate": "0.0000"
        # Valore decimale (0.0 – 1.0): moltiplica per 10_000 per ottenere bps
        decimal_keys = {
            "feerate",       # "feeRate"
            "makerfeerate",  # "makerFeeRate"
            "takerfeerate",  # "takerFeeRate"
            "basefee",       # "base_fee"
            "rate",          # "rate"
            "fee",           # "fee"
        }
        found = self._find_first_scalar(raw_value, decimal_keys)
        if found not in (None, ""):
            try:
                val = float(found)
                # Valore decimale 0.0 – 1.0 → converti in bps
                if 0.0 <= val <= 1.0:
                    return int(round(val * 10_000))
                # Valore gia' in bps (> 1)
                return int(val)
            except (TypeError, ValueError):
                pass

        return None

    def _find_first_scalar(self, value: Any, candidate_keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower().replace("_", "")
                if normalized in candidate_keys and not isinstance(child, (dict, list)):
                    return child
            for child in value.values():
                found = self._find_first_scalar(child, candidate_keys)
                if found is not None:
                    return found

        if isinstance(value, list):
            for child in value:
                found = self._find_first_scalar(child, candidate_keys)
                if found is not None:
                    return found

        return None
