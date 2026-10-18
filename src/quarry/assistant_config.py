"""Model configuration and GUI reply preferences; credentials stay local."""

from __future__ import annotations

import json
import locale
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import core, workspace
from .core import QuarryError


CONFIGURE_HINT = "Model not configured. Run `qy ai configure --help` in your terminal."


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    bifrost: bool = False
    thinking: str = "default"
    timeout: int = 30


def _credentials_path() -> Path:
    return workspace._config_path().with_name("assistant-credentials.json")


def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if private:
                os.fchmod(handle.fileno(), 0o600)
            elif path.exists():
                os.fchmod(handle.fileno(), path.stat().st_mode & 0o777)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise QuarryError("Model base URL must be an HTTP(S) URL without whitespace.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise QuarryError("Invalid model base URL.") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0):
        raise QuarryError("Model base URL must be HTTP(S), without credentials, query or fragment.")
    return urlunsplit(parsed._replace(path=parsed.path.rstrip("/")))


def _read_document() -> dict:
    path = workspace._config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        raise QuarryError("Cannot read Quarry configuration. Check config.toml.") from None
    return data


def _read_settings() -> dict:
    settings = _read_document().get("assistant", {})
    if not isinstance(settings, dict):
        raise QuarryError("Invalid [assistant] configuration.")
    return settings


def _config_table_span(lines: list[str], table: str) -> tuple[int, int] | None:
    """Recognize TOML headers with indentation, quoted names or comments.

    Candidate edits are also parsed and compared before writing, so header-like
    text inside a multiline value cannot silently change an unrelated setting.
    """
    wanted: dict = {}
    for part in reversed(table.split(".")):
        wanted = {part: wanted}
    headers = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            headers.append((index, tomllib.loads(line)))
        except ValueError:
            continue
    for position, (index, parsed) in enumerate(headers):
        if parsed == wanted:
            return index, headers[position + 1][0] if position + 1 < len(headers) else len(lines)
    return None


def _locale_language(value: str | None) -> str | None:
    name = re.split(r"[-_.@]", (value or "").strip().lower())[0]
    if not name or name in ("c", "posix"):
        return None
    return "zh" if name in ("zh", "chinese") else "en"


def _macos_language() -> str | None:
    # AppleLanguages is the preferred language; AppleLocale may describe only
    # regional formatting. Read these keys alone, never all user preferences.
    try:
        result = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                                capture_output=True, timeout=2, check=True)
        xml = subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", "--", "-"],
                             input=result.stdout, capture_output=True, timeout=2, check=True)
        languages = plistlib.loads(xml.stdout)
        if isinstance(languages, list) and languages and isinstance(languages[0], str):
            language = _locale_language(languages[0])
            if language:
                return language
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        pass
    try:
        result = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLocale"],
                                capture_output=True, text=True, timeout=2, check=True)
        return _locale_language(result.stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def system_reply_language() -> str:
    """Resolve the server's OS language without changing process-wide locale."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        language = _locale_language(os.environ.get(name))
        if language:
            return language
    if sys.platform == "darwin":
        language = _macos_language()
        if language:
            return language
    try:
        return _locale_language(locale.getlocale()[0]) or "en"
    except (ValueError, TypeError):
        return "en"


def gui_preferences() -> dict:
    """Read the CLI-managed preference and resolve it on the Quarry server."""
    gui = _read_document().get("gui", {})
    settings = gui.get("assistant", {}) if isinstance(gui, dict) else None
    if not isinstance(settings, dict):
        raise QuarryError("Invalid [gui.assistant] configuration.")
    language = settings.get("reply_language", "system")
    if language not in ("zh", "en", "system", "auto"):
        raise QuarryError("Invalid GUI reply language. Use `qy ai language zh|en|system`.")
    language = "system" if language == "auto" else language
    return {"replyLanguage": language, "configured": "reply_language" in settings,
            "resolvedReplyLanguage": system_reply_language() if language == "system" else language}


def set_reply_language(language: str) -> dict:
    """Update the GUI-only preference through CLI; auto is a legacy alias."""
    if not isinstance(language, str) or language not in ("zh", "en", "system", "auto"):
        raise QuarryError("Reply language must be zh, en, or system.")
    language = "system" if language == "auto" else language
    path = workspace._config_path()
    with core.connections_file_lock(path):
        # CLI is also the recovery path for an invalid stored preference.
        document = _read_document()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        span = _config_table_span(lines, "gui.assistant")
        assignment = f"reply_language = {json.dumps(language)}"
        if span:
            start, end = span
            for index in range(start + 1, end):
                if re.match(r"^\s*reply_language\s*=", lines[index]):
                    lines[index] = assignment
                    break
            else:
                lines.insert(end, assignment)
        else:
            lines.extend(([""] if lines else []) + ["[gui.assistant]", assignment])
        text = "\n".join(lines) + "\n"
        try:
            parsed = tomllib.loads(text)
            document.setdefault("gui", {}).setdefault("assistant", {})["reply_language"] = language
            if parsed != document:
                raise ValueError("Unrelated configuration would change")
        except (ValueError, AttributeError, TypeError):
            raise QuarryError("Cannot update GUI reply language. Use a [gui.assistant] table in config.toml.") from None
        _atomic_write(path, text)
        return gui_preferences()


def configure_model(*, base_url: str, model: str, api_key_env: str | None = None,
                    api_key: str | None = None, no_key: bool = False,
                    bifrost: bool = False, thinking: str = "default", timeout: int = 30) -> dict:
    base_url = _base_url(base_url)
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise QuarryError("A model name is required (maximum 200 characters).")
    if thinking not in ("default", "disabled") or type(timeout) is not int or not 1 <= timeout <= 300:
        raise QuarryError("Invalid thinking mode or timeout (expected 1–300 seconds).")
    if sum((api_key_env is not None, api_key is not None, no_key)) != 1:
        raise QuarryError("Choose --api-key-env, --api-key-stdin, or --no-key.")
    if api_key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise QuarryError("Invalid API key environment variable name.")
    if api_key is not None and (not api_key.strip() or len(api_key) > 8192
                                or any(ord(c) < 32 for c in api_key)):
        raise QuarryError("API key must be a nonempty single line.")
    path = workspace._config_path()
    fields = {"base_url": base_url, "model": model.strip(), "auth": "none" if no_key else "env" if api_key_env else "stored",
              "bifrost": bifrost, "thinking": thinking, "timeout": timeout}
    if api_key_env:
        fields["api_key_env"] = api_key_env
    block = ["[assistant]"] + [f"{key} = {json.dumps(value)}" for key, value in fields.items()] + [""]
    # Share the preference writer's lock: concurrent model configuration must
    # not erase a GUI reply preference.
    with core.connections_file_lock(path):
        document = _read_document()  # Refuse to overwrite malformed TOML.
        if not isinstance(document.get("assistant", {}), dict):
            raise QuarryError("Invalid [assistant] configuration.")
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        span = _config_table_span(lines, "assistant")
        if span:
            lines[span[0]:span[1]] = block
        else:
            lines.extend(([""] if lines else []) + block)
        text = "\n".join(lines) + "\n"
        try:
            parsed = tomllib.loads(text)
            updated_settings = parsed.get("assistant", {})
            if not isinstance(updated_settings, dict) or any(updated_settings.get(key) != value for key, value in fields.items()):
                raise ValueError("Model configuration was not updated")
            if ({k: v for k, v in parsed.items() if k != "assistant"}
                    != {k: v for k, v in document.items() if k != "assistant"}):
                raise ValueError("Unrelated configuration would change")
        except ValueError:
            raise QuarryError("Cannot update model configuration. Use an [assistant] table in config.toml.") from None
        # Validate the complete replacement before touching either file.
        if api_key is not None:
            _atomic_write(_credentials_path(), json.dumps({"base_url": base_url, "api_key": api_key}) + "\n", private=True)
        _atomic_write(path, text)
        if api_key is None:
            _credentials_path().unlink(missing_ok=True)
    return config_details()


def load_model_config() -> ModelConfig:
    fields = _read_settings()
    if not fields.get("base_url") or not fields.get("model"):
        raise QuarryError(CONFIGURE_HINT)
    base_url = _base_url(fields["base_url"])
    model = fields["model"]
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise QuarryError("Invalid assistant model name. Run `qy ai configure --help`.")
    auth = fields.get("auth", "env")
    key = ""
    if auth == "env":
        variable = fields.get("api_key_env", "QUARRY_AI_API_KEY")
        key = os.environ.get(variable, "") if isinstance(variable, str) else ""
    elif auth == "stored":
        try:
            saved = json.loads(_credentials_path().read_text(encoding="utf-8"))
            key = saved.get("api_key", "") if saved.get("base_url") == base_url else ""
        except (OSError, ValueError, AttributeError):
            pass
    elif auth != "none":
        raise QuarryError("Invalid assistant authentication mode.")
    if auth != "none" and (not isinstance(key, str) or not key or any(ord(c) < 32 for c in key)):
        raise QuarryError("Model API key is unavailable. Run `qy ai configure --help` in your terminal.")
    timeout = fields.get("timeout", 30)
    thinking = fields.get("thinking", "default")
    if type(timeout) is not int or not 1 <= timeout <= 300 or thinking not in ("default", "disabled"):
        raise QuarryError("Invalid assistant timeout or thinking mode.")
    return ModelConfig(base_url, model, key, fields.get("bifrost") is True, thinking, timeout)


def config_status() -> dict:
    """Public, credential-free readiness probe; never performs a model request."""
    try:
        config = load_model_config()
    except QuarryError:
        return {"configured": False}
    return {"configured": True, "model": config.model}


def config_details() -> dict:
    """CLI settings view: exposes configuration, never the key itself."""
    fields = _read_settings()
    return {**config_status(), **{key: fields[key] for key in (
        "base_url", "model", "auth", "api_key_env", "bifrost", "thinking", "timeout") if key in fields},
        "api_key": "********" if config_status()["configured"] and fields.get("auth") != "none" else None}
