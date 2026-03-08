"""Live preflight: verify balance, allowances, and wallet state before starting the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from config.settings import Settings


@dataclass
class PreflightReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


class LivePreflight:
    """Validate wallet and CLOB state before live trading."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, client) -> PreflightReport:
        report = PreflightReport()

        if not client.get_address():
            report.errors.append("wallet address unavailable after connection")
            return report

        collateral_raw = client.get_balance_allowance("COLLATERAL")
        if collateral_raw is None:
            report.errors.append(
                f"unable to validate USDC balance/allowance: {client.last_api_error or 'no response'}"
            )
        else:
            self._check_collateral(collateral_raw, report)

        conditional_raw = client.get_balance_allowance("CONDITIONAL")
        if conditional_raw is None:
            report.warnings.append(
                f"unable to validate conditional token approval: {client.last_api_error or 'no response'}"
            )
        else:
            self._check_conditional_approval(conditional_raw, report)

        open_orders = client.get_open_orders()
        if open_orders is None:
            report.errors.append(
                f"unable to read open orders: {client.last_api_error or 'no response'}"
            )
        else:
            count = len(open_orders)
            report.notes.append(f"open orders detected: {count}")
            if count > 0 and not self.settings.trading.allow_existing_open_orders:
                report.errors.append(
                    f"dirty wallet: found {count} open orders. Cancel them or enable ALLOW_EXISTING_OPEN_ORDERS."
                )
            elif count > 0:
                report.warnings.append(f"continuing with {count} open orders already present in the wallet")

        positions = client.get_positions()
        if positions is None:
            report.errors.append(
                f"unable to read leftover positions: {client.last_api_error or 'no response'}"
            )
        else:
            active_positions = [pos for pos in positions if self._extract_position_size(pos) > 1e-9]
            count = len(active_positions)
            report.notes.append(f"leftover positions detected: {count}")
            if count > 0 and not self.settings.trading.allow_existing_positions:
                report.errors.append(
                    f"dirty wallet: found {count} leftover positions. Close them or enable ALLOW_EXISTING_POSITIONS."
                )
            elif count > 0:
                report.warnings.append(f"continuing with {count} leftover positions already present in the wallet")

        return report

    def _check_collateral(self, raw: dict[str, Any], report: PreflightReport) -> None:
        balance = self._normalize_usdc_value(self._find_first(raw, {"balance", "availablebalance", "available"}))
        allowance = self._normalize_usdc_value(self._find_first(raw, {"allowance", "availableallowance"}))

        if balance is None:
            report.errors.append("USDC balance could not be read from the balance-allowance response")
        else:
            report.notes.append(f"available USDC balance: ${balance:.2f}")
            required_balance = self.settings.trading.max_capital + self.settings.trading.min_usdc_buffer
            if balance < required_balance:
                report.errors.append(
                    f"insufficient USDC balance: ${balance:.2f} < required ${required_balance:.2f} "
                    f"(capital {self.settings.trading.max_capital:.2f} + buffer {self.settings.trading.min_usdc_buffer:.2f})"
                )

        if allowance is None:
            report.errors.append("USDC allowance could not be read from the balance-allowance response")
        else:
            report.notes.append(f"available USDC allowance: ${allowance:.2f}")
            if allowance < self.settings.trading.max_capital:
                report.errors.append(
                    f"insufficient USDC allowance: ${allowance:.2f} < ${self.settings.trading.max_capital:.2f}"
                )

    def _check_conditional_approval(self, raw: dict[str, Any], report: PreflightReport) -> None:
        approved = self._to_bool(
            self._find_first(raw, {"approved", "isapproved", "isapprovedforall", "approvedforall"})
        )
        allowance = self._normalize_usdc_value(self._find_first(raw, {"allowance", "availableallowance"}))

        if approved is False:
            report.errors.append("conditional tokens not approved for the exchange")
            return

        if approved is True:
            report.notes.append("conditional token approval: OK")
            return

        if allowance is not None and allowance > 0:
            report.notes.append(f"conditional token allowance detected: ${allowance:.2f}")
            return

        report.warnings.append("conditional token approval not explicitly confirmed by the response")

    def _extract_position_size(self, position: dict[str, Any]) -> float:
        value = self._find_first(position, {"size", "amount", "balance", "shares"})
        parsed = self._to_float(value)
        return parsed or 0.0

    def _find_first(self, value: Any, candidate_keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower().replace("_", "")
                if normalized in candidate_keys and not isinstance(child, (dict, list)):
                    return child
            for child in value.values():
                found = self._find_first(child, candidate_keys)
                if found is not None:
                    return found

        if isinstance(value, list):
            for child in value:
                found = self._find_first(child, candidate_keys)
                if found is not None:
                    return found

        return None

    def _normalize_usdc_value(self, value: Any) -> Optional[float]:
        parsed = self._to_float(value)
        if parsed is None:
            return None
        if abs(parsed) >= 1_000_000:
            return parsed / 1_000_000
        return parsed

    def _to_float(self, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_bool(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "approved"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return None
