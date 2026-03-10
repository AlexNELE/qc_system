"""
settings.py — Central configuration for the QC System.

Operator-editable settings are stored in an external ``settings.json`` file
that sits next to the EXE (PyInstaller build) or next to this file (dev run).
On first run, if ``settings.json`` does not exist it is created automatically
with the defaults shown below — operators can then edit it with any text editor
and restart the application without rebuilding.

Fields NOT in settings.json are fixed engineering constants (queue sizes,
directory names, JPEG quality, etc.) and must be changed here in source.

FUTURE: Replace flat constants with a Pydantic BaseSettings model that reads
        from a .env file or environment variables for containerised deployments.
"""

# ---------------------------------------------------------------------------
# Stdlib imports — NO third-party dependencies allowed in this file.
# ---------------------------------------------------------------------------
import json
import os
import sys
from pathlib import Path

# ===========================================================================
# 1. Locate the base directory
#    Frozen EXE  → directory that contains the EXE
#    Dev script  → directory that contains this settings.py file
# ===========================================================================
if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle
    _BASE_DIR: Path = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).resolve().parent

# The path to the external operator config file.
# Other modules may import CONFIG_PATH to display it in the UI or logs.
CONFIG_PATH: Path = _BASE_DIR / "settings.json"

# ===========================================================================
# 2. Defaults — these are the values used when settings.json is absent or
#    when a key is missing from the file.
# ===========================================================================
_DEFAULTS: dict = {
    "cameras": [0, 1, 2, 3],
    "expected_count": 160,
    "model_path": "models/yolov8_model.onnx",
    "conf_threshold": 0.5,
    "iou_threshold": 0.45,
    "target_class_id": 0,
    "camera_reconnect_delay": 2.0,
    "camera_reconnect_max": 30.0,
    "save_annotated_images": True,
    "log_level": "DEBUG",
    "auth": {
        "active_directory_enabled": True,
        "login_required": True,
        "no_auth_default_role": "ADMIN",
        "ldap_servers": ["dc1.example.com", "dc2.example.com"],
        "ldap_domain": "example.com",
        "ldap_base_dn": "DC=example,DC=com",
        "ldap_group_role_map": {
            "CN=QC-Admins":      "ADMIN",
            "CN=QC-Supervisors": "SUPERVISOR",
            "CN=QC-Operators":   "OPERATOR",
        },
        "ldap_default_role": "OPERATOR",
        "ldap_use_tls": False,
        "ldap_use_ssl": False,
        "ldap_connect_timeout": 5.0,
    },
}

# ===========================================================================
# 3. Load (or create) settings.json
# ===========================================================================
def _load_config() -> dict:
    """
    Load operator settings from CONFIG_PATH.

    - If the file does not exist, write a default file and return defaults.
    - If the file exists but is malformed JSON, warn to stderr and return defaults.
    - Missing keys in an otherwise valid file are filled with defaults.
    """
    if not CONFIG_PATH.exists():
        # First run: write defaults so the operator has a template to edit.
        try:
            CONFIG_PATH.write_text(
                json.dumps(_DEFAULTS, indent=4),
                encoding="utf-8",
            )
            print(
                f"[settings] settings.json not found — created default config at {CONFIG_PATH}",
                file=sys.stderr,
            )
        except OSError as exc:
            print(
                f"[settings] WARNING: could not write default settings.json ({exc}); using built-in defaults.",
                file=sys.stderr,
            )
        return dict(_DEFAULTS)

    # File exists — attempt to parse it.
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        loaded: dict = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[settings] WARNING: settings.json is malformed ({exc}); falling back to built-in defaults.",
            file=sys.stderr,
        )
        return dict(_DEFAULTS)

    # Merge: start from defaults, overlay whatever keys the operator has set.
    merged = dict(_DEFAULTS)
    merged.update(loaded)

    print(
        f"[settings] Loaded operator config from {CONFIG_PATH}",
        file=sys.stderr,
    )
    return merged


_cfg: dict = _load_config()

# Auth sub-section — merged with defaults above
_auth_cfg: dict = _cfg.get("auth", _DEFAULTS["auth"])

# ===========================================================================
# 4. Expose operator-editable settings as module-level constants
#    All names are IDENTICAL to the pre-existing names so no other file needs
#    to change.
# ===========================================================================

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------
# CAMERAS / CAMERA_SOURCES — both kept in sync; UI and services use CAMERAS.
CAMERA_SOURCES: list[int | str] = [
    int(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else c
    for c in _cfg["cameras"]
]
CAMERAS: list[int | str] = CAMERA_SOURCES

# Derived — always equals len(CAMERAS).  Do NOT set this by hand.
MAX_CAMERAS: int = len(CAMERAS)

# Seconds between reconnect attempts; doubles on each retry, capped at max.
CAMERA_RECONNECT_DELAY: float = float(_cfg["camera_reconnect_delay"])
CAMERA_RECONNECT_MAX: float = float(_cfg["camera_reconnect_max"])

# ---------------------------------------------------------------------------
# Model / Inference
# ---------------------------------------------------------------------------
MODEL_PATH: str = str(_cfg["model_path"])

# Fallback used only when the ONNX model has dynamic input dimensions.
# For static models (all standard YOLOv8 exports) the Detector class reads
# the actual size directly from the model and ignores this value.
MODEL_INPUT_SIZE: tuple[int, int] = (640, 640)

CONF_THRESHOLD: float = float(_cfg["conf_threshold"])
IOU_THRESHOLD: float = float(_cfg["iou_threshold"])

# Class ID in the ONNX model that corresponds to the counted object.
# FUTURE: Extend to a list[int] to count multiple classes simultaneously.
TARGET_CLASS_ID: int = int(_cfg["target_class_id"])

# ---------------------------------------------------------------------------
# Counting / QC
# ---------------------------------------------------------------------------
# FUTURE: Increase EXPECTED_COUNT — edit expected_count in settings.json.
EXPECTED_COUNT: int = int(_cfg["expected_count"])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = str(_cfg["log_level"])

# ---------------------------------------------------------------------------
# Defect image saving — operator-editable subset
# ---------------------------------------------------------------------------
SAVE_ANNOTATED_IMAGES: bool = bool(_cfg["save_annotated_images"])

# ===========================================================================
# 5. Fixed engineering constants — NOT exposed in settings.json.
#    Change these here in source code only; they are not operator-facing.
# ===========================================================================

# Maximum frames held in each camera → inference queue.
# Keep small (2) to prevent unbounded memory growth and latency buildup.
FRAME_QUEUE_SIZE: int = 2

# False = each InferenceService owns its own InferenceSession (Option B, recommended).
# True  = all threads share one session protected by a mutex (Option A).
# FUTURE: Replace with TensorRT EP — change providers list in detector.py
SHARED_ONNX_SESSION: bool = False

# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
USE_TRACKER: bool = True

# Maximum pixels a centroid may travel between frames to be considered
# the same object.  Tune for your conveyor belt speed and frame rate.
TRACKER_MAX_DISTANCE: float = 50.0

# Frames a track may go unseen before it is pruned.
TRACKER_MAX_DISAPPEARED: int = 5

# ---------------------------------------------------------------------------
# Defect image saving — fixed engineering constants
# ---------------------------------------------------------------------------
SAVE_ALL_DEFECT_IMAGES: bool = True

# Root directory for missing-item images (relative to the project root at runtime).
DEFECT_DIR: str = "defects"

# JPEG compression quality (0-100).
JPEG_QUALITY_ORIGINAL: int = 95
JPEG_QUALITY_ANNOTATED: int = 90

# Thread pool size for async missing-item image I/O.
# FUTURE: Increase if disk I/O becomes the throughput bottleneck.
DEFECT_WORKER_THREADS: int = 4

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
# Resolved to an absolute path so the same DB is found regardless of the
# process working directory (important for PyInstaller builds launched via
# desktop shortcut, where CWD can vary).
DB_PATH: str = str(_BASE_DIR / "qc_results.db")

# ---------------------------------------------------------------------------
# Capture image saving
# ---------------------------------------------------------------------------
# Save every captured frame (OK and MISSING) when Capture All is pressed.
SAVE_CAPTURE_IMAGES: bool = True

# Directory where capture images are stored (one sub-folder per batch ID).
CAPTURES_DIR: str = str(_BASE_DIR / "captures")

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
# Root directory for generated PDF reports (relative to the project root).
REPORTS_DIR: str = "reports"

# ---------------------------------------------------------------------------
# Logging — directory (level comes from settings.json)
# ---------------------------------------------------------------------------
LOG_DIR: str = "logs"

# ===========================================================================
# 6. Active Directory / LDAP authentication settings
#    These are IT-configurable via the "auth" section of settings.json.
#    They can also be overridden here in source for deployments that prefer
#    to bake credentials into the build rather than expose them in a file.
#
#    settings.json "auth" section keys (all optional — defaults shown):
#      ldap_servers        : list of DC hostnames/IPs
#      ldap_domain         : FQDN used to build UPN (user@domain)
#      ldap_base_dn        : LDAP search base DN
#      ldap_group_role_map : dict mapping partial group DNs -> role names
#      ldap_default_role   : role when user matches no group ("OPERATOR")
#      ldap_use_tls        : enable STARTTLS on port 389 (bool)
#      ldap_use_ssl        : enable LDAPS on port 636 (bool)
#      ldap_connect_timeout: TCP timeout in seconds (float)
# ===========================================================================

# Ordered list of Active Directory domain controller hostnames / IPs.
# LDAPAuthService tries each in turn; first reachable server wins.
LDAP_SERVERS: list[str] = list(
    _auth_cfg.get("ldap_servers", ["dc1.example.com", "dc2.example.com"])
)

# FQDN of the Active Directory domain — used to build the UPN (user@domain).
LDAP_DOMAIN: str = str(_auth_cfg.get("ldap_domain", "example.com"))

# LDAP search base — the OU / container to search for user objects.
LDAP_BASE_DN: str = str(_auth_cfg.get("ldap_base_dn", "DC=example,DC=com"))

# Mapping from AD group DN fragments to role name strings.
# Matching is case-insensitive substring — a partial DN like "CN=QC-Admins"
# matches "CN=QC-Admins,OU=Groups,DC=example,DC=com".
# Role strings must match Role enum names exactly: OPERATOR, SUPERVISOR, ADMIN.
LDAP_GROUP_ROLE_MAP: dict[str, str] = dict(
    _auth_cfg.get(
        "ldap_group_role_map",
        {
            "CN=QC-Admins":      "ADMIN",
            "CN=QC-Supervisors": "SUPERVISOR",
            "CN=QC-Operators":   "OPERATOR",
        },
    )
)

# Default role granted when the user authenticates successfully but belongs
# to none of the mapped AD groups.  Set to "OPERATOR" for least privilege.
LDAP_DEFAULT_ROLE: str = str(_auth_cfg.get("ldap_default_role", "OPERATOR"))

# Enable STARTTLS negotiation after connecting on plain port 389.
# Recommended for LAN deployments where LDAPS is not configured.
# Mutually exclusive with LDAP_USE_SSL — only one may be True.
LDAP_USE_TLS: bool = bool(_auth_cfg.get("ldap_use_tls", False))

# Enable LDAPS (TLS from the start on port 636).
# Set to True in production environments. Requires a valid CA certificate.
LDAP_USE_SSL: bool = bool(_auth_cfg.get("ldap_use_ssl", False))

# Path to the CA certificate bundle used to validate the AD server's TLS cert.
# Set to None to use the system default CA store.
LDAP_CA_CERT_FILE: str | None = _auth_cfg.get("ldap_ca_cert_file", None)  # type: ignore[assignment]

# TCP connect timeout in seconds for each LDAP server probe / bind attempt.
LDAP_CONNECT_TIMEOUT: float = float(_auth_cfg.get("ldap_connect_timeout", 5.0))

# ===========================================================================
# 7. User authentication cache database
#    Stores password hashes, display names, roles, and last-login info for
#    offline fallback.  Separate from the QC results database.
# ===========================================================================

USER_CACHE_DB_PATH: str = str(_BASE_DIR / "user_cache.db")

# ===========================================================================
# 8. Authentication mode — operator-editable via settings.json
# ===========================================================================

# Set to False to disable all authentication and start the application
# directly without any login dialog.  When False, a local ADMIN session is
# created automatically using AUTH_NO_AUTH_DEFAULT_ROLE.
AUTH_AD_ENABLED: bool = bool(_auth_cfg.get("active_directory_enabled", True))

# When False the application starts immediately with an automatic OPERATOR
# session — no login is required.  The Login button in the header remains
# available so administrators can authenticate for elevated access.
AUTH_LOGIN_REQUIRED: bool = bool(_auth_cfg.get("login_required", True))

# Role granted to the auto-created session when AUTH_AD_ENABLED is False.
# Must match a Role enum name exactly: OPERATOR, SUPERVISOR, or ADMIN.
AUTH_NO_AUTH_DEFAULT_ROLE: str = str(_auth_cfg.get("no_auth_default_role", "ADMIN"))
