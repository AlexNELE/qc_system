"""
auth/ldap_service.py — Active Directory / LDAP authentication service.

Integration library: ldap3 (pure-Python, no native GSSAPI required).

Authentication flow
-------------------
1.  Attempt to connect to each server in ``LDAP_SERVERS`` (failover list).
2.  Perform a simple bind with ``username@LDAP_DOMAIN`` (UPN format) and
    the supplied password.  This validates the credential against AD.
3.  After a successful bind, search for the user object to retrieve:
      - ``displayName``
      - ``mail``
      - ``memberOf`` (list of group DNs the user belongs to)
4.  Walk ``memberOf`` and match against ``LDAP_GROUP_ROLE_MAP`` (DN suffix
    matching so the full OU path need not be exact).
5.  Return a ``UserSession`` with the highest matching role.
6.  If no group matches, fall back to ``LDAP_DEFAULT_ROLE``.

Offline / fallback flow
-----------------------
If every LDAP server is unreachable (``ldap3.core.exceptions.LDAPException``
or ``socket.timeout``), the service raises ``LDAPUnavailableError`` so the
caller (LoginDialog) can attempt to authenticate from the local SQLite cache
(UserCacheDB).  Credentials are NOT verified offline — the cache only grants
access if the user has a row with a cached_password_hash that matches.

Connection security
-------------------
``LDAP_USE_TLS = True``  → ``ldap3.Tls`` + ``START_TLS`` after connect
``LDAP_USE_SSL = True``  → LDAPS on port 636 (mutually exclusive with START_TLS)
Both off → plain LDAP on port 389 (dev/LAN-only, never for production)

Group → Role mapping example (settings.py)::

    LDAP_GROUP_ROLE_MAP = {
        "CN=QC-Admins,OU=Groups":       "ADMIN",
        "CN=QC-Supervisors,OU=Groups":  "SUPERVISOR",
        "CN=QC-Operators,OU=Groups":    "OPERATOR",
    }

The matching is a case-insensitive substring check so partial DNs work.

FUTURE: Replace simple bind with SASL/GSSAPI (Kerberos) for environments
        that enforce NTLMv2/Kerberos-only authentication.
FUTURE: Cache a short-lived LDAP connection pool to avoid reconnect overhead
        on every login for high-throughput deployments.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

import settings
from auth.permissions import Role, UserSession

logger = logging.getLogger("auth.ldap")


# ---------------------------------------------------------------------------
# Custom exceptions — callers must handle these explicitly; no bare excepts.
# ---------------------------------------------------------------------------

class LDAPAuthError(Exception):
    """
    Raised when the user's credentials are rejected by Active Directory.

    The ``message`` attribute is safe to display in the UI.
    """


class LDAPUnavailableError(Exception):
    """
    Raised when no LDAP server could be reached (network or config error).

    LoginDialog catches this to trigger the offline-cache fallback.
    """


class LDAPConfigError(Exception):
    """Raised when the LDAP settings are structurally invalid."""


# ---------------------------------------------------------------------------
# LDAPAuthService
# ---------------------------------------------------------------------------

class LDAPAuthService:
    """
    Stateless Active Directory authentication helper.

    Every public method is safe to call from any thread (no shared mutable
    state; each call opens its own ldap3 Connection).

    Parameters
    ----------
    servers:
        List of AD server hostnames or IPs.  Tried in order; first
        reachable server wins.  Defaults to ``settings.LDAP_SERVERS``.
    domain:
        NetBIOS or FQDN domain used to build the UPN
        (``user@domain``).  Defaults to ``settings.LDAP_DOMAIN``.
    base_dn:
        Search base for user lookups.  Defaults to
        ``settings.LDAP_BASE_DN``.
    group_role_map:
        Dict mapping AD group DN substrings to role name strings.
        Defaults to ``settings.LDAP_GROUP_ROLE_MAP``.
    default_role:
        Role granted when no group matches.  Defaults to
        ``settings.LDAP_DEFAULT_ROLE``.
    use_tls:
        Enable STARTTLS after connect.  Defaults to ``settings.LDAP_USE_TLS``.
    use_ssl:
        Use LDAPS (port 636).  Mutually exclusive with use_tls.
        Defaults to ``settings.LDAP_USE_SSL``.
    connect_timeout:
        Per-server TCP connect timeout in seconds.
        Defaults to ``settings.LDAP_CONNECT_TIMEOUT``.
    """

    def __init__(
        self,
        servers: Optional[list[str]] = None,
        domain: Optional[str] = None,
        base_dn: Optional[str] = None,
        group_role_map: Optional[dict[str, str]] = None,
        default_role: Optional[str] = None,
        use_tls: Optional[bool] = None,
        use_ssl: Optional[bool] = None,
        connect_timeout: Optional[float] = None,
    ) -> None:
        self._servers         = servers         or settings.LDAP_SERVERS
        self._domain          = domain          or settings.LDAP_DOMAIN
        self._base_dn         = base_dn         or settings.LDAP_BASE_DN
        self._group_role_map  = group_role_map  or settings.LDAP_GROUP_ROLE_MAP
        self._default_role    = default_role    or settings.LDAP_DEFAULT_ROLE
        self._use_tls         = use_tls         if use_tls is not None else settings.LDAP_USE_TLS
        self._use_ssl         = use_ssl         if use_ssl is not None else settings.LDAP_USE_SSL
        self._connect_timeout = connect_timeout or settings.LDAP_CONNECT_TIMEOUT

        if not self._servers:
            raise LDAPConfigError("LDAP_SERVERS must contain at least one server address.")
        if not self._domain:
            raise LDAPConfigError("LDAP_DOMAIN must be set.")
        if not self._base_dn:
            raise LDAPConfigError("LDAP_BASE_DN must be set.")

        logger.info(
            "LDAPAuthService initialised | servers=%s domain=%s base_dn=%s tls=%s ssl=%s",
            self._servers, self._domain, self._base_dn, self._use_tls, self._use_ssl,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> UserSession:
        """
        Authenticate a user against Active Directory.

        Parameters
        ----------
        username:
            SAMAccountName (e.g. ``jsmith``) — NOT the UPN, NOT email.
            The UPN is constructed as ``username@domain`` internally.
        password:
            Plain-text password (transmitted only over the TLS/LDAPS channel).

        Returns
        -------
        UserSession
            Populated with AD attributes and resolved role.

        Raises
        ------
        LDAPAuthError
            Bad credentials or account locked/disabled.
        LDAPUnavailableError
            No LDAP server reachable (caller should try offline cache).
        LDAPConfigError
            Settings are structurally invalid.
        """
        # Guard against trivially empty credentials
        if not username or not username.strip():
            raise LDAPAuthError("Username must not be empty.")
        if not password:
            raise LDAPAuthError("Password must not be empty.")

        username = username.strip().lower()
        upn      = f"{username}@{self._domain}"

        conn = self._connect_and_bind(upn, password)
        try:
            display_name, email, groups = self._fetch_user_attributes(conn, username)
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001 — unbind errors are non-critical
                pass

        role = self._resolve_role(groups)

        session = UserSession(
            username          = username,
            display_name      = display_name or username,
            role              = role,
            authenticated_via = "ldap",
            email             = email or "",
        )
        logger.info(
            "LDAP authentication successful | user=%s role=%s",
            username, role.name,
        )
        return session

    def is_server_reachable(self) -> bool:
        """
        Quick TCP probe of the first LDAP server to test connectivity.

        Returns True if at least one server responds on the expected port.
        Does NOT perform a bind — used by LoginDialog to decide whether to
        show the 'Offline mode' indicator before the user presses Login.
        """
        port = 636 if self._use_ssl else 389
        for server in self._servers:
            try:
                with socket.create_connection(
                    (server, port), timeout=self._connect_timeout
                ):
                    logger.debug("LDAP probe OK | server=%s port=%d", server, port)
                    return True
            except OSError:
                logger.debug("LDAP probe failed | server=%s port=%d", server, port)
        return False

    # ------------------------------------------------------------------
    # Private — connection
    # ------------------------------------------------------------------

    def _connect_and_bind(self, upn: str, password: str):
        """
        Try each configured server in order, return a bound Connection.

        Raises LDAPUnavailableError if no server is reachable.
        Raises LDAPAuthError if the bind is rejected (bad credentials).
        """
        import ldap3
        import ldap3.core.exceptions as ldap_exc

        port        = 636 if self._use_ssl else 389
        last_error: Optional[Exception] = None

        for server_host in self._servers:
            logger.debug("Trying LDAP server %s:%d", server_host, port)
            try:
                tls_obj = None
                if self._use_tls or self._use_ssl:
                    import ssl as _ssl
                    tls_obj = ldap3.Tls(
                        validate        = _ssl.CERT_REQUIRED,
                        version         = _ssl.PROTOCOL_TLS_CLIENT,
                        ca_certs_file   = getattr(settings, "LDAP_CA_CERT_FILE", None),
                    )

                server = ldap3.Server(
                    server_host,
                    port          = port,
                    use_ssl       = self._use_ssl,
                    tls           = tls_obj,
                    connect_timeout = self._connect_timeout,
                    get_info      = ldap3.NONE,  # no anonymous info query needed
                )

                conn = ldap3.Connection(
                    server,
                    user       = upn,
                    password   = password,
                    authentication = ldap3.SIMPLE,
                    raise_exceptions = True,
                    receive_timeout  = self._connect_timeout,
                )

                if self._use_tls and not self._use_ssl:
                    conn.start_tls()

                conn.bind()

                if not conn.bound:
                    # Bind returned False without raising — treat as auth failure
                    raise LDAPAuthError(
                        f"Authentication failed for user '{upn}'. "
                        "Check your username and password."
                    )

                logger.debug("LDAP bind successful | server=%s upn=%s", server_host, upn)
                return conn

            except ldap_exc.LDAPInvalidCredentialsResult as exc:
                logger.warning("LDAP invalid credentials | upn=%s server=%s", upn, server_host)
                raise LDAPAuthError(
                    "Invalid username or password. "
                    "Your account may also be locked — contact your administrator."
                ) from exc

            except ldap_exc.LDAPSocketOpenError as exc:
                logger.warning("LDAP socket error | server=%s: %s", server_host, exc)
                last_error = exc
                continue  # try next server

            except ldap_exc.LDAPException as exc:
                logger.warning("LDAP error | server=%s: %s", server_host, exc)
                last_error = exc
                continue

            except OSError as exc:
                logger.warning("Network error reaching %s: %s", server_host, exc)
                last_error = exc
                continue

        # All servers exhausted
        raise LDAPUnavailableError(
            f"No LDAP server could be reached. "
            f"Last error: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Private — user attribute retrieval
    # ------------------------------------------------------------------

    def _fetch_user_attributes(
        self,
        conn,
        username: str,
    ) -> tuple[str, str, list[str]]:
        """
        Search AD for the user's displayName, mail, and memberOf groups.

        Returns
        -------
        (display_name, email, group_dn_list)
        """
        import ldap3

        search_filter = f"(sAMAccountName={ldap3.utils.conv.escape_filter_chars(username)})"

        conn.search(
            search_base   = self._base_dn,
            search_filter = search_filter,
            search_scope  = ldap3.SUBTREE,
            attributes    = ["displayName", "mail", "memberOf"],
        )

        if not conn.entries:
            logger.warning(
                "LDAP search returned no entries for user=%s base=%s",
                username, self._base_dn,
            )
            return username, "", []

        entry        = conn.entries[0]
        display_name = str(entry.displayName.value) if entry.displayName else username
        email        = str(entry.mail.value) if entry.mail else ""
        groups: list[str] = []

        if entry.memberOf:
            raw = entry.memberOf.value
            if isinstance(raw, list):
                groups = [str(g) for g in raw]
            elif raw:
                groups = [str(raw)]

        logger.debug(
            "LDAP attributes fetched | user=%s display=%s groups_count=%d",
            username, display_name, len(groups),
        )
        return display_name, email, groups

    # ------------------------------------------------------------------
    # Private — role resolution
    # ------------------------------------------------------------------

    def _resolve_role(self, group_dns: list[str]) -> Role:
        """
        Map the user's AD group memberships to the highest matching Role.

        Matching is a case-insensitive substring check so a partial DN
        like ``"CN=QC-Admins"`` will match
        ``"CN=QC-Admins,OU=Groups,DC=example,DC=com"``.

        If no group matches, returns the configured default role.
        """
        best_role = self._str_to_role(self._default_role)

        for group_dn_fragment, role_name in self._group_role_map.items():
            fragment_lower = group_dn_fragment.lower()
            for user_group in group_dns:
                if fragment_lower in user_group.lower():
                    candidate = self._str_to_role(role_name)
                    if candidate > best_role:
                        best_role = candidate
                    logger.debug(
                        "Group match | fragment=%r -> role=%s",
                        group_dn_fragment, candidate.name,
                    )

        logger.info("Resolved role: %s", best_role.name)
        return best_role

    @staticmethod
    def _str_to_role(role_name: str) -> Role:
        """
        Convert a role name string (e.g. ``"ADMIN"``) to a Role enum value.

        Falls back to Role.OPERATOR if the string is unrecognised.
        """
        try:
            return Role[role_name.upper()]
        except KeyError:
            logger.warning("Unrecognised role name %r — defaulting to OPERATOR", role_name)
            return Role.OPERATOR
