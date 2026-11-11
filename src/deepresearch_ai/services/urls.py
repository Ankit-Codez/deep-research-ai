"""URL validation and conservative deduplication; never trust HTML canonical tags."""

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


class UnsafeURL(ValueError):
    """The URL is not eligible for public web retrieval."""


def public_ip(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
        if isinstance(parsed, ipaddress.IPv6Address) and (
            parsed.sixtofour or parsed.teredo
            or parsed in ipaddress.ip_network("64:ff9b::/96")
            or parsed in ipaddress.ip_network("64:ff9b:1::/48")
        ):
            return False
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return parsed.is_global and not parsed.is_multicast and not parsed.is_reserved
    except ValueError:
        return False


def validate_web_url(value: str) -> httpx.URL:
    if (
        not isinstance(value, str) or len(value) > 4096
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise UnsafeURL("URL contains invalid characters or is too long.")
    try:
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"} or not url.host
            or url.userinfo or "%" in url.host
            or url.port not in {None, 80 if url.scheme == "http" else 443}
        ):
            raise UnsafeURL("Only HTTP(S) URLs on their standard ports are allowed.")
        host = url.host.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
            raise UnsafeURL("Local network names are blocked.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise UnsafeURL("Single-label network names are blocked.")
        else:
            if not public_ip(host):
                raise UnsafeURL("Non-public addresses are blocked.")
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError) as exc:
        if isinstance(exc, UnsafeURL):
            raise
        raise UnsafeURL("Malformed web URL.") from exc


def canonical_url(value: str) -> str:
    """Normalize host case, standard ports, fragments, and known tracking keys.

    Keep path case, trailing slashes, meaningful query parameters and their order.
    Sorting queries or discarding arbitrary parameters can merge distinct pages.
    """
    url = validate_web_url(value)
    parts = urlsplit(str(url))
    host = url.raw_host.decode("ascii").rstrip(".").lower()
    authority = f"[{host}]" if ":" in host else host
    all_parameters = parse_qsl(parts.query, keep_blank_values=True)
    parameters = [
        (key, val) for key, val in all_parameters
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
    ]
    query = parts.query if len(parameters) == len(all_parameters) else urlencode(parameters)
    return urlunsplit((url.scheme, authority, parts.path or "/", query, ""))
