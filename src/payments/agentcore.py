"""Thin AgentCore Payments data-plane client + x402 HTTP interceptor.

Two IAM roles back the data plane (created by the prototype's setup_roles.sh):

  * ManagementRole       → create/get payment instruments (wallets), balances,
                           and create/get payment sessions. Explicitly DENIED
                           ProcessPayment.
  * ProcessPaymentRole   → ProcessPayment only.

We assume whichever role an operation needs via STS and build a fresh
`bedrock-agentcore` client with the temporary credentials. STS creds last ~1h;
clients are cached per role and lazily rebuilt, which is ample for request-scoped
use. The x402 interceptor (initial request → read 402 → ProcessPayment → attach
proof → retry) is ported from the reference prototype, generalized to take the
per-user instrument and per-query session as call arguments, with an `on_quote`
hook so callers can enforce spend caps before any payment is signed.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date, datetime
from http import HTTPStatus
from typing import Any, Callable

import boto3
import requests

from src.payments.config import PaymentsConfig, get_payments_config


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def quote_usd_from_payload(payload: dict[str, Any], decimals: int) -> float | None:
    """Best-effort USD price of an x402 accept option.

    x402 quotes carry the price in the asset's atomic units (USDC = 6 decimals)
    under `maxAmountRequired` (preferred) or `amount`. Returns None when neither
    is parseable so callers can decide how to treat an unknown-cost resource.
    """
    raw = payload.get("maxAmountRequired", payload.get("amount"))
    if raw is None:
        return None
    try:
        return int(raw) / (10 ** decimals)
    except (TypeError, ValueError):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


class AgentCorePayments:
    """Role-scoped access to AgentCore Payments data-plane operations."""

    def __init__(self, config: PaymentsConfig | None = None) -> None:
        self.config = config or get_payments_config()
        self._clients: dict[str, Any] = {}

    # ── client / role plumbing ────────────────────────────────────────
    def _client_for(self, role_arn: str):
        cached = self._clients.get(role_arn)
        if cached is not None:
            return cached
        session_kwargs: dict[str, Any] = {"region_name": self.config.aws_region}
        if self.config.aws_profile:
            session_kwargs["profile_name"] = self.config.aws_profile
        base = boto3.Session(**session_kwargs)
        creds = base.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"hf-payments-{int(datetime.now().timestamp())}",
        )["Credentials"]
        client_kwargs: dict[str, Any] = {"region_name": self.config.aws_region}
        if self.config.dp_endpoint:
            client_kwargs["endpoint_url"] = self.config.dp_endpoint
        client = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.config.aws_region,
        ).client("bedrock-agentcore", **client_kwargs)
        self._clients[role_arn] = client
        return client

    @property
    def _mgmt(self):
        return self._client_for(self.config.management_role_arn)

    @property
    def _pay(self):
        return self._client_for(self.config.process_payment_role_arn)

    # ── wallets (payment instruments) ─────────────────────────────────
    def create_embedded_wallet(self, user_id: str, email: str) -> dict[str, Any]:
        """Create one CDP Embedded Wallet for `user_id`. Returns the instrument
        id, wallet address, and the WalletHub redirect URL the user must open to
        grant delegated-signing permission before payments will succeed."""
        details = {
            "embeddedCryptoWallet": {
                "network": self.config.wallet_network,
                "linkedAccounts": [{"email": {"emailAddress": email}}],
            }
        }
        resp = self._mgmt.create_payment_instrument(
            paymentManagerArn=self.config.manager_arn,
            paymentConnectorId=self.config.connector_id,
            paymentInstrumentType="EMBEDDED_CRYPTO_WALLET",
            paymentInstrumentDetails=details,
            userId=user_id,
            clientToken=str(uuid.uuid4()),
        )
        return self._instrument_summary(resp.get("paymentInstrument", {}))

    def get_wallet(self, user_id: str, instrument_id: str) -> dict[str, Any]:
        resp = self._mgmt.get_payment_instrument(
            paymentManagerArn=self.config.manager_arn,
            paymentConnectorId=self.config.connector_id,
            paymentInstrumentId=instrument_id,
            userId=user_id,
        )
        return self._instrument_summary(resp.get("paymentInstrument", {}))

    def get_wallet_balance(self, user_id: str, instrument_id: str) -> dict[str, Any] | None:
        """Best-effort balance read. Returns None on any error so callers can
        still surface wallet metadata when the balance API is unavailable."""
        try:
            resp = self._mgmt.get_payment_instrument_balance(
                paymentManagerArn=self.config.manager_arn,
                paymentConnectorId=self.config.connector_id,
                paymentInstrumentId=instrument_id,
                userId=user_id,
            )
            resp.pop("ResponseMetadata", None)
            return _json_safe(resp)
        except Exception:
            return None

    @staticmethod
    def _instrument_summary(instrument: dict[str, Any]) -> dict[str, Any]:
        details = instrument.get("paymentInstrumentDetails", {})
        wallet = details.get("embeddedCryptoWallet") or details.get("cryptoWallet") or {}
        return {
            "payment_instrument_id": instrument.get("paymentInstrumentId"),
            "wallet_address": wallet.get("walletAddress"),
            "redirect_url": wallet.get("redirectUrl") or details.get("redirectUrl"),
            "status": instrument.get("status"),
        }

    # ── sessions (carry the per-query spend cap) ──────────────────────
    def create_session(self, user_id: str, max_spend_usd: float) -> str:
        resp = self._mgmt.create_payment_session(
            paymentManagerArn=self.config.manager_arn,
            expiryTimeInMinutes=self.config.session_expiry_minutes,
            limits={
                "maxSpendAmount": {
                    "value": f"{max_spend_usd:.6f}",
                    "currency": "USD",
                }
            },
            userId=user_id,
            clientToken=str(uuid.uuid4()),
        )
        return resp["paymentSession"]["paymentSessionId"]

    # ── payment ───────────────────────────────────────────────────────
    def process_payment(
        self,
        *,
        user_id: str,
        session_id: str,
        instrument_id: str,
        x402_payload: dict[str, Any],
        x402_version: int,
    ) -> dict[str, Any]:
        payload = dict(x402_payload)
        if x402_version >= 2:
            for key in ("description", "mimeType", "resource", "outputSchema"):
                payload.pop(key, None)
        resp = self._pay.process_payment(
            userId=user_id,
            paymentManagerArn=self.config.manager_arn,
            paymentSessionId=session_id,
            paymentInstrumentId=instrument_id,
            paymentType="CRYPTO_X402",
            paymentInput={"cryptoX402": {"version": str(x402_version), "payload": payload}},
            clientToken=str(uuid.uuid4()),
        )
        resp.pop("ResponseMetadata", None)
        return _json_safe(resp)


class QuoteRejected(Exception):
    """Raised by an `on_quote` hook to abort before any payment is signed."""


class X402Interceptor:
    """Deterministic paid-request flow for a single (user, session, wallet).

    Ported from the reference prototype's HeuristX402Client; the fixed env
    session/instrument are replaced with the per-call values handed in here, and
    an `on_quote(quote_usd)` hook fires after the price is known but before
    ProcessPayment, so spend caps can veto the payment.
    """

    def __init__(
        self,
        agentcore: AgentCorePayments,
        *,
        user_id: str,
        session_id: str,
        instrument_id: str,
        default_network: str,
        on_quote: Callable[[float | None], None] | None = None,
        http_session: requests.Session | None = None,
    ) -> None:
        self.ac = agentcore
        self.user_id = user_id
        self.session_id = session_id
        self.instrument_id = instrument_id
        self.default_network = default_network
        self.on_quote = on_quote
        self.http = http_session or requests.Session()

    def _request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> requests.Response:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = json.dumps(body) if body is not None else None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return self.http.request(method, url, headers=request_headers, data=data, timeout=timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(attempt)
                    continue
                raise last_error
        raise last_error  # pragma: no cover

    @staticmethod
    def _parse_payment_info(response: requests.Response) -> tuple[dict[str, Any], int]:
        header = response.headers.get("PAYMENT-REQUIRED", "")
        if header:
            info = _json_safe(json.loads(base64.b64decode(header)))
            return info, int(info.get("x402Version", 2))
        info = _json_safe(response.json())
        return info, int(info.get("x402Version", 1))

    @classmethod
    def _extract_requirement(
        cls, response: requests.Response
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        info, version = cls._parse_payment_info(response)
        accepts = info.get("accepts", [])
        if not accepts:
            raise ValueError("402 response missing accepts payload")
        return info, accepts[0], version

    def _canonicalize(self, url: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        # POST quotes can omit fields the signer needs; a GET to the same URL
        # usually returns the canonical requirement. Fall back to the original
        # on any error.
        if method.upper() == "GET":
            return payload
        try:
            resp = self._request("GET", url, body=None, timeout=30)
            if resp.status_code != HTTPStatus.PAYMENT_REQUIRED:
                return payload
            _, canonical, _ = self._extract_requirement(resp)
            return canonical
        except Exception:
            return payload

    def _build_header(
        self,
        accepted: dict[str, Any],
        pay_result: dict[str, Any],
        version: int,
        payment_info: dict[str, Any] | None,
    ) -> tuple[str, str]:
        crypto = pay_result["paymentOutput"]["cryptoX402"]
        proof = crypto.get("payload", crypto)
        if version >= 2:
            wrapper: dict[str, Any] = {"x402Version": 2, "payload": proof, "accepted": accepted}
            resource = (payment_info or {}).get("resource")
            if resource:
                wrapper["resource"] = resource
            return "PAYMENT-SIGNATURE", base64.b64encode(json.dumps(wrapper).encode()).decode()
        wrapper = {
            "x402Version": 1,
            "scheme": accepted.get("scheme", "exact"),
            "network": accepted.get("network", self.default_network),
            "payload": proof,
        }
        return "X-PAYMENT", base64.b64encode(json.dumps(wrapper).encode()).decode()

    def fetch_with_payment(
        self,
        url: str,
        body: dict[str, Any] | None = None,
        method: str = "POST",
        max_attempts: int = 6,
    ) -> dict[str, Any]:
        """Returns a result dict with `status` in {success, success_without_payment,
        unexpected_initial_status, missing_accepts, payment_failed,
        insufficient_balance, retry_failed_*, retry_exhausted} plus `quote_usd`
        once known. Raises QuoteRejected if the on_quote hook vetoes the price."""
        result: dict[str, Any] = {"url": url, "method": method, "quote_usd": None}
        first = self._request(method, url, body=body, timeout=30)
        result["initial_status_code"] = first.status_code

        if first.status_code == HTTPStatus.OK:
            result["status"] = "success_without_payment"
            result["response"] = self._safe_json(first)
            return result
        if first.status_code != HTTPStatus.PAYMENT_REQUIRED:
            result["status"] = "unexpected_initial_status"
            result["response_text"] = first.text[:2000]
            return result

        try:
            payment_info, accepted, version = self._extract_requirement(first)
        except Exception as exc:
            result["status"] = "missing_accepts"
            result["error"] = str(exc)
            return result

        canonical = self._canonicalize(url, method, accepted)
        result["x402_version"] = version
        quote_usd = quote_usd_from_payload(canonical, self.ac.config.usdc_decimals)
        result["quote_usd"] = quote_usd

        # Spend-cap gate: fires before any money moves. A raise here aborts the
        # whole call and no PaymentSession spend is consumed.
        if self.on_quote is not None:
            self.on_quote(quote_usd)

        pay_result = self.ac.process_payment(
            user_id=self.user_id,
            session_id=self.session_id,
            instrument_id=self.instrument_id,
            x402_payload=canonical,
            x402_version=version,
        )
        result["payment_result_status"] = pay_result.get("status")
        if pay_result.get("status") != "PROOF_GENERATED":
            result["status"] = "payment_failed"
            result["payment_result"] = pay_result
            return result

        header_name, header_value = self._build_header(canonical, pay_result, version, payment_info)
        for attempt in range(1, max_attempts + 1):
            retry = self._request(method, url, body=body, headers={header_name: header_value})
            if retry.status_code == HTTPStatus.OK:
                result["status"] = "success"
                result["attempts"] = attempt
                result["response"] = self._safe_json(retry)
                return result
            if retry.status_code == HTTPStatus.PAYMENT_REQUIRED:
                retry_body = self._safe_json(retry)
                if isinstance(retry_body, dict) and retry_body.get("error") == "insufficient_balance":
                    result["status"] = "insufficient_balance"
                    result["attempts"] = attempt
                    result["response"] = retry_body
                    return result
                if attempt < max_attempts:
                    time.sleep(2 * attempt)
                    continue
            result["status"] = f"retry_failed_{retry.status_code}"
            result["attempts"] = attempt
            result["response"] = self._safe_json(retry)
            return result

        result["status"] = "retry_exhausted"
        return result

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return _json_safe(response.json())
        except Exception:
            return response.text[:2000]


_SINGLETON: AgentCorePayments | None = None


def get_agentcore() -> AgentCorePayments:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = AgentCorePayments()
    return _SINGLETON
