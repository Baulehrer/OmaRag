from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..models.domain import (
    EgressDecision,
    EgressEndpointScope,
    EgressPayloadClass,
    EgressReasonCode,
    PrivacyMode,
    PrivacyPolicy,
    WorkspaceManifest,
)
from ..models.errors import OmaRagError


class EgressPolicyError(OmaRagError):
    """A fail-closed denial whose public details contain no endpoint or content."""

    status_code = 403
    code = "EGRESS_DENIED"

    def __init__(self, decision: EgressDecision) -> None:
        self.decision = decision
        super().__init__(
            "HTTP egress was denied by the active privacy policy",
            details=decision.model_dump(mode="json"),
        )


@dataclass(frozen=True, slots=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    loopback: bool
    restricted: bool

    @property
    def key(self) -> str:
        return f"{self.scheme}|{self.host}|{self.port}"


def _opaque_endpoint_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest}"


_PRIVATE_DNS_SUFFIXES = (".internal", ".local", ".localhost", ".home.arpa", ".lan")
_METADATA_HOSTS = frozenset(
    {
        "instance-data",
        "instance-data.ec2.internal",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)


def _restricted_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ``is_global`` alone is not sufficient on all supported Python versions:
    # multicast can still report global while remaining an invalid URL-import
    # destination. Keep every non-unicast-public literal fail-closed.
    return (
        not address.is_global
        or address.is_link_local
        or address.is_loopback
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _canonical_host(value: str) -> tuple[str, bool, bool]:
    host = value.strip().rstrip(".").casefold()
    if not host or "%" in host:
        raise ValueError("invalid host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # libc accepts legacy IPv4 forms such as ``2130706433`` and ``127.1``.
        # Canonicalize them without DNS so they cannot bypass literal-range
        # checks and later resolve to loopback/private destinations.
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, UnicodeError):
            address = None
        if address is not None:
            return address.compressed, address.is_loopback, _restricted_address(address)
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid host") from exc
        restricted = (
            host == "localhost" or host in _METADATA_HOSTS or host.endswith(_PRIVATE_DNS_SUFFIXES)
        )
        return host, host == "localhost" or host.endswith(".localhost"), restricted
    return address.compressed, address.is_loopback, _restricted_address(address)


def _parse_endpoint(value: str, *, origin_only: bool = False) -> _Endpoint:
    if not value or len(value) > 4096 or any(ord(character) < 32 for character in value):
        raise ValueError("invalid endpoint")
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("invalid endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint credentials are forbidden")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("trusted endpoint must be an origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid endpoint port") from exc
    host, loopback, restricted = _canonical_host(parsed.hostname)
    return _Endpoint(
        scheme=scheme,
        host=host,
        port=port or (443 if scheme == "https" else 80),
        loopback=loopback,
        restricted=restricted,
    )


class EgressPolicy:
    """Authorize HTTP targets without ever returning their raw URLs.

    URL imports additionally treat non-public IP literals, legacy numeric IPv4
    forms, and well-known private/metadata DNS names as restricted. They need
    an exact origin allowlist entry; DNS changes never grant implicit trust.
    """

    def __init__(self, policy: PrivacyPolicy | None = None) -> None:
        self.policy = policy or PrivacyPolicy()
        trusted: set[str] = set()
        for index, value in enumerate(self.policy.trusted_endpoints):
            try:
                endpoint = _parse_endpoint(value, origin_only=True)
            except ValueError as exc:
                raise ValueError(f"trusted endpoint at index {index} is invalid") from exc
            if (
                endpoint.scheme == "http"
                and not endpoint.loopback
                and not self.policy.allow_insecure_trusted_http
            ):
                raise ValueError(
                    f"trusted endpoint at index {index} requires HTTPS or explicit HTTP opt-in"
                )
            trusted.add(endpoint.key)
        self._trusted = frozenset(trusted)

    @classmethod
    def from_workspace(
        cls,
        manifest: WorkspaceManifest,
        *,
        trusted_endpoints: Iterable[str] = (),
        allow_insecure_trusted_http: bool = False,
    ) -> EgressPolicy:
        return cls(
            PrivacyPolicy(
                mode=manifest.privacy_mode,
                trusted_endpoints=list(trusted_endpoints),
                cloud_acknowledged=manifest.cloud_acknowledged,
                allow_insecure_trusted_http=allow_insecure_trusted_http,
            )
        )

    def evaluate_http(
        self,
        url: str,
        payload_class: EgressPayloadClass = EgressPayloadClass.USER_CONTENT,
    ) -> EgressDecision:
        try:
            endpoint = _parse_endpoint(url)
        except ValueError:
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=EgressEndpointScope.INVALID,
                endpoint_id=_opaque_endpoint_id(url),
                reason=EgressReasonCode.DENY_INVALID_ENDPOINT,
            )

        endpoint_id = _opaque_endpoint_id(endpoint.key)
        trusted = endpoint.key in self._trusted
        scope = (
            EgressEndpointScope.LOOPBACK
            if endpoint.loopback
            else EgressEndpointScope.TRUSTED
            if trusted
            else EgressEndpointScope.CLOUD
        )
        # A URL source is an explicit import from another HTTP origin.  The
        # device-only promise forbids it even when that origin happens to be a
        # loopback development server; local files use the file import path.
        if (
            payload_class is EgressPayloadClass.URL_SOURCE
            and self.policy.mode is PrivacyMode.DEVICE_ONLY
        ):
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_DEVICE_ONLY,
            )
        if payload_class is EgressPayloadClass.URL_SOURCE and endpoint.restricted and not trusted:
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_UNTRUSTED,
            )
        if endpoint.loopback:
            return self._decision(
                allowed=True,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.ALLOW_LOOPBACK,
            )
        if endpoint.scheme != "https" and not (trusted and self.policy.allow_insecure_trusted_http):
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_INSECURE_TRANSPORT,
            )
        if self.policy.mode is PrivacyMode.DEVICE_ONLY:
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_DEVICE_ONLY,
            )
        if trusted:
            return self._decision(
                allowed=True,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.ALLOW_TRUSTED,
            )
        if self.policy.mode is PrivacyMode.TRUSTED_ENDPOINT:
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_UNTRUSTED,
            )
        if payload_class.content_bearing and not self.policy.cloud_acknowledged:
            return self._decision(
                allowed=False,
                payload_class=payload_class,
                scope=scope,
                endpoint_id=endpoint_id,
                reason=EgressReasonCode.DENY_CLOUD_ACK_REQUIRED,
            )
        return self._decision(
            allowed=True,
            payload_class=payload_class,
            scope=scope,
            endpoint_id=endpoint_id,
            reason=EgressReasonCode.ALLOW_CLOUD,
        )

    def authorize_http(
        self,
        url: str,
        payload_class: EgressPayloadClass = EgressPayloadClass.USER_CONTENT,
    ) -> EgressDecision:
        decision = self.evaluate_http(url, payload_class)
        if not decision.allowed:
            raise EgressPolicyError(decision)
        return decision

    def _decision(
        self,
        *,
        allowed: bool,
        payload_class: EgressPayloadClass,
        scope: EgressEndpointScope,
        endpoint_id: str,
        reason: EgressReasonCode,
    ) -> EgressDecision:
        return EgressDecision(
            allowed=allowed,
            privacy_mode=self.policy.mode,
            payload_class=payload_class,
            endpoint_scope=scope,
            endpoint_id=endpoint_id,
            reason_code=reason,
        )


def authorize_content_http(url: str, policy: PrivacyPolicy) -> EgressDecision:
    """Convenience guard for the common user-content request path."""

    return EgressPolicy(policy).authorize_http(url, EgressPayloadClass.USER_CONTENT)
