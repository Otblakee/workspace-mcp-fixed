"""
Enhanced Log Formatter for Google Workspace MCP

Provides visually appealing log formatting with emojis and consistent styling
to match the safe_print output format.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys


class EnhancedLogFormatter(logging.Formatter):
    """Custom log formatter that adds ASCII prefixes and visual enhancements to log messages."""

    # Color codes for terminals that support ANSI colors
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
        "RESET": "\033[0m",  # Reset
    }

    def __init__(self, use_colors: bool = True, *args, **kwargs):
        """
        Initialize the emoji log formatter.

        Args:
            use_colors: Whether to use ANSI color codes (default: True)
        """
        super().__init__(*args, **kwargs)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record with ASCII prefixes and enhanced styling."""
        # Get the appropriate ASCII prefix for the service
        service_prefix = self._get_ascii_prefix(record.name, record.levelname)

        # Format the message with enhanced styling
        formatted_msg = self._enhance_message(record.getMessage())

        # Build the formatted log entry
        if self.use_colors:
            color = self.COLORS.get(record.levelname, "")
            reset = self.COLORS["RESET"]
            return f"{service_prefix} {color}{formatted_msg}{reset}"
        else:
            return f"{service_prefix} {formatted_msg}"

    def _get_ascii_prefix(self, logger_name: str, level_name: str) -> str:
        """Get ASCII-safe prefix for Windows compatibility."""
        # ASCII-safe prefixes for different services
        ascii_prefixes = {
            "core.tool_tier_loader": "[TOOLS]",
            "core.tool_registry": "[REGISTRY]",
            "auth.scopes": "[AUTH]",
            "core.utils": "[UTILS]",
            "auth.google_auth": "[OAUTH]",
            "auth.credential_store": "[CREDS]",
            "gcalendar.calendar_tools": "[CALENDAR]",
            "gdrive.drive_tools": "[DRIVE]",
            "gmail.gmail_tools": "[GMAIL]",
            "gdocs.docs_tools": "[DOCS]",
            "gsheets.sheets_tools": "[SHEETS]",
            "gchat.chat_tools": "[CHAT]",
            "gforms.forms_tools": "[FORMS]",
            "gslides.slides_tools": "[SLIDES]",
            "gtasks.tasks_tools": "[TASKS]",
            "gsearch.search_tools": "[SEARCH]",
        }

        return ascii_prefixes.get(logger_name, f"[{level_name}]")

    def _enhance_message(self, message: str) -> str:
        """Enhance the log message with better formatting."""
        # Handle common patterns for better visual appeal

        # Tool tier loading messages
        if "resolved to" in message and "tools across" in message:
            # Extract numbers and service names for better formatting
            pattern = (
                r"Tier '(\w+)' resolved to (\d+) tools across (\d+) services: (.+)"
            )
            match = re.search(pattern, message)
            if match:
                tier, tool_count, service_count, services = match.groups()
                return f"Tool tier '{tier}' loaded: {tool_count} tools across {service_count} services [{services}]"

        # Configuration loading messages
        if "Loaded tool tiers configuration from" in message:
            path = message.split("from ")[-1]
            return f"Configuration loaded from {path}"

        # Tool filtering messages
        if "Tool tier filtering" in message:
            pattern = r"removed (\d+) tools, (\d+) enabled"
            match = re.search(pattern, message)
            if match:
                removed, enabled = match.groups()
                return f"Tool filtering complete: {enabled} tools enabled ({removed} filtered out)"

        # Enabled tools messages
        if "Enabled tools set for scope management" in message:
            tools = message.split(": ")[-1]
            return f"Scope management configured for tools: {tools}"

        # Credentials directory messages
        if "Credentials directory permissions check passed" in message:
            path = message.split(": ")[-1]
            return f"Credentials directory verified: {path}"

        # If no specific pattern matches, return the original message
        return message


def setup_enhanced_logging(
    log_level: int = logging.INFO, use_colors: bool = True
) -> None:
    """
    Set up enhanced logging with ASCII prefix formatter for the entire application.

    Args:
        log_level: The logging level to use (default: INFO)
        use_colors: Whether to use ANSI colors (default: True)
    """
    # Create the enhanced formatter
    formatter = EnhancedLogFormatter(use_colors=use_colors)

    # Get the root logger
    root_logger = logging.getLogger()

    # Update existing console handlers
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream.name in [
            "<stderr>",
            "<stdout>",
        ]:
            handler.setFormatter(formatter)

    # If no console handler exists, create one
    console_handlers = [
        h
        for h in root_logger.handlers
        if isinstance(h, logging.StreamHandler)
        and h.stream.name in ["<stderr>", "<stdout>"]
    ]

    if not console_handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)


class QueryStringFilter(logging.Filter):
    """Strip query strings from uvicorn access-log lines.

    uvicorn formats access records as ``'%s - "%s %s HTTP/%s" %d'`` with the
    full request target as the third argument, so ``/oauth2callback?code=…``
    and ``/authorize?…state=…`` would land in the retained log stream on
    every sign-in. The path is kept; everything after ``?`` is dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            from core.redaction import strip_query_string

            args = list(args)
            args[2] = strip_query_string(args[2])
            record.args = tuple(args)
        return True


FILE_LOGGING_ENV = "WORKSPACE_MCP_FILE_LOGGING"
FILE_LOG_MAX_BYTES = 10 * 1024 * 1024
FILE_LOG_BACKUP_COUNT = 3


def configure_file_logging(logger_name: str = None) -> bool:
    """
    Configure file logging based on stateless mode setting.

    In stateless mode, file logging is completely disabled to avoid filesystem writes.
    In normal mode, sets up detailed file logging to 'mcp_server_debug.log'.

    Args:
        logger_name: Optional name for the logger (defaults to root logger)

    Returns:
        bool: True if file logging was configured, False if skipped (stateless mode)
    """
    # Check if stateless mode is enabled
    stateless_mode = (
        os.getenv("WORKSPACE_MCP_STATELESS_MODE", "false").lower() == "true"
    )

    if stateless_mode:
        logger = logging.getLogger(logger_name)
        logger.debug("File logging disabled in stateless mode")
        return False

    # Explicit opt-out. The debug file captures DEBUG-level output, which
    # includes Gmail/Drive query strings, identities and Google API error
    # bodies. On a hosted deployment (Render) stdout is already retained by
    # the platform, so the on-container file is pure liability: set
    # WORKSPACE_MCP_FILE_LOGGING=false there. Local stdio development keeps
    # the file by default.
    if os.getenv(FILE_LOGGING_ENV, "true").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        logging.getLogger(logger_name).debug(
            "File logging disabled via %s", FILE_LOGGING_ENV
        )
        return False

    # Configure file logging for normal mode
    try:
        target_logger = logging.getLogger(logger_name)
        log_file_dir = os.path.dirname(os.path.abspath(__file__))
        # Go up one level since we're in core/ subdirectory
        log_file_dir = os.path.dirname(log_file_dir)
        log_file_path = os.path.join(log_file_dir, "mcp_server_debug.log")

        # Rotate so an always-on DEBUG log can never fill the filesystem:
        # 10 MiB x 3 backups, ~40 MiB worst case.
        file_handler = RotatingFileHandler(
            log_file_path,
            mode="a",
            maxBytes=FILE_LOG_MAX_BYTES,
            backupCount=FILE_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)

        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(process)d - %(threadName)s "
            "[%(module)s.%(funcName)s:%(lineno)d] - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        target_logger.addHandler(file_handler)

        logger = logging.getLogger(logger_name)
        logger.debug(f"Detailed file logging configured to: {log_file_path}")
        return True

    except Exception as e:
        sys.stderr.write(
            f"CRITICAL: Failed to set up file logging to '{log_file_path}': {e}\n"
        )
        return False
