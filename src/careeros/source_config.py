"""Secure, private configurable-source configuration loading."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from careeros.settings import ENV_SOURCES_CONFIG, default_sources_config_path

MAX_CONFIG_BYTES = 64 * 1024
MAX_BOARDS = 8
_BOARD_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}\Z")


class SourceConfigError(ValueError):
    """Raised for missing, unsafe, malformed, or invalid source config."""


@dataclass(frozen=True)
class LeverBoard:
    id: str
    instance: str
    company: str


@dataclass(frozen=True)
class LeverConfig:
    present: bool
    enabled: bool
    boards: tuple[LeverBoard, ...]


@dataclass(frozen=True)
class GreenhouseBoard:
    token: str


@dataclass(frozen=True)
class GreenhouseConfig:
    present: bool
    enabled: bool
    boards: tuple[GreenhouseBoard, ...]


@dataclass(frozen=True)
class WorkableBoard:
    subdomain: str


@dataclass(frozen=True)
class WorkableConfig:
    present: bool
    enabled: bool
    boards: tuple[WorkableBoard, ...]


@dataclass(frozen=True)
class SmartRecruitersCompany:
    identifier: str


@dataclass(frozen=True)
class SmartRecruitersConfig:
    present: bool
    enabled: bool
    companies: tuple[SmartRecruitersCompany, ...]


@dataclass(frozen=True)
class SourceConfig:
    version: int
    lever: LeverConfig
    path: Path | None = None
    explicit: bool = False
    # Keep this after the existing defaulted fields for positional M1
    # compatibility.
    greenhouse: GreenhouseConfig = GreenhouseConfig(False, False, ())
    workable: WorkableConfig = WorkableConfig(False, False, ())
    smartrecruiters: SmartRecruitersConfig = SmartRecruitersConfig(False, False, ())

    @property
    def lever_configured(self) -> bool:
        return self.lever.present

    @property
    def greenhouse_configured(self) -> bool:
        return self.greenhouse.present

    @property
    def workable_configured(self) -> bool:
        return self.workable.present

    @property
    def smartrecruiters_configured(self) -> bool:
        return self.smartrecruiters.present


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise SourceConfigError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def selected_sources_config_path(
    path: str | os.PathLike[str] | None = None,
) -> tuple[Path, bool]:
    """Resolve config precedence; return path and whether it was explicitly selected."""
    if path is not None:
        return Path(path).expanduser().absolute(), True
    value = os.environ.get(ENV_SOURCES_CONFIG)
    if value:
        return Path(value).expanduser().absolute(), True
    return default_sources_config_path(), False


def _read_secure(path: Path, *, required: bool) -> str | None:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise SourceConfigError(
                f"source configuration does not exist: {path}"
            ) from None
        return None
    except OSError as error:
        raise SourceConfigError(
            f"cannot read source configuration {path}: {error}"
        ) from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SourceConfigError(
                "source configuration must be an owned regular file with one link"
            )
        if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise SourceConfigError(
                "source configuration must be owned by the effective user and mode 0600"
            )
        if before.st_size > MAX_CONFIG_BYTES:
            raise SourceConfigError(
                f"source configuration exceeds {MAX_CONFIG_BYTES} bytes"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise SourceConfigError(
                    f"source configuration exceeds {MAX_CONFIG_BYTES} bytes"
                )
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_uid,
            before.st_mode,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_uid,
            after.st_mode,
            after.st_nlink,
        ):
            raise SourceConfigError("source configuration changed while being read")
        try:
            return b"".join(chunks).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise SourceConfigError(
                "source configuration is not valid UTF-8"
            ) from error
    finally:
        os.close(fd)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise SourceConfigError(f"{label} must be a mapping")
    return value


def _string(
    value: object, label: str, *, maximum: int, ascii_only: bool = False
) -> str:
    if not isinstance(value, str):
        raise SourceConfigError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise SourceConfigError(f"{label} has invalid length")
    if ascii_only and not value.isascii():
        raise SourceConfigError(f"{label} must be ASCII")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SourceConfigError(f"{label} contains control characters")
    return value


def _reject_duplicate_nodes(node: yaml.Node) -> None:
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            marker = yaml.serialize(key)
            if marker in keys:
                raise SourceConfigError("duplicate YAML key")
            keys.add(marker)
            _reject_duplicate_nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _reject_duplicate_nodes(child)


def _parse(text: str, path: Path) -> SourceConfig:
    try:
        _reject_duplicate_nodes(yaml.compose(text, Loader=_UniqueLoader))
        raw = yaml.safe_load(text)
    except SourceConfigError:
        raise
    except yaml.YAMLError as error:
        raise SourceConfigError(f"invalid YAML in {path}: {error}") from error
    root = _mapping(raw, "root")
    if set(root) != {"version", "sources"}:
        raise SourceConfigError("root keys must be exactly version and sources")
    version = root["version"]
    if isinstance(version, bool) or version != 1 or not isinstance(version, int):
        raise SourceConfigError("version must be integer 1")
    try:
        sources = _mapping(root["sources"], "sources")
    except TypeError as error:
        raise SourceConfigError("sources must be a mapping") from error
    if set(sources) - {"lever", "greenhouse", "workable", "smartrecruiters"}:
        raise SourceConfigError("sources contains an unknown key")
    lever_config = LeverConfig(False, False, ())
    if "lever" in sources:
        lever = _mapping(sources["lever"], "sources.lever")
        if set(lever) - {"enabled", "boards"} or "enabled" not in lever:
            raise SourceConfigError("Lever keys must be exactly enabled and boards")
        enabled = lever["enabled"]
        if not isinstance(enabled, bool):
            raise SourceConfigError("sources.lever.enabled must be boolean")
        boards_raw = lever.get("boards", [])
        if not isinstance(boards_raw, list):
            raise SourceConfigError("sources.lever.boards must be a list")
        if len(boards_raw) > MAX_BOARDS:
            raise SourceConfigError(f"at most {MAX_BOARDS} Lever boards are allowed")
        boards: list[LeverBoard] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(boards_raw):
            board = _mapping(item, f"board {index}")
            if set(board) != {"id", "instance", "company"}:
                raise SourceConfigError(
                    "Lever board keys must be exactly id, instance, company"
                )
            board_id = _string(board["id"], "board.id", maximum=100, ascii_only=True)
            if not _BOARD_ID.fullmatch(board_id):
                raise SourceConfigError("board.id has invalid format")
            instance = _string(board["instance"], "board.instance", maximum=6)
            if instance not in {"global", "eu"}:
                raise SourceConfigError("board.instance must be global or eu")
            company = _string(board["company"], "board.company", maximum=200)
            if (instance, board_id) in seen:
                raise SourceConfigError("duplicate canonical Lever board")
            seen.add((instance, board_id))
            boards.append(LeverBoard(board_id, instance, company))
        if enabled and not boards:
            raise SourceConfigError("enabled Lever configuration requires boards")
        lever_config = LeverConfig(True, enabled, tuple(boards))

    greenhouse_config = GreenhouseConfig(False, False, ())
    if "greenhouse" in sources:
        greenhouse = _mapping(sources["greenhouse"], "sources.greenhouse")
        if set(greenhouse) - {"enabled", "boards"} or "enabled" not in greenhouse:
            raise SourceConfigError(
                "Greenhouse keys must be exactly enabled and boards"
            )
        enabled = greenhouse["enabled"]
        if not isinstance(enabled, bool):
            raise SourceConfigError("sources.greenhouse.enabled must be boolean")
        boards_raw = greenhouse.get("boards", [])
        if not isinstance(boards_raw, list):
            raise SourceConfigError("sources.greenhouse.boards must be a list")
        if len(boards_raw) > MAX_BOARDS:
            raise SourceConfigError(
                f"at most {MAX_BOARDS} Greenhouse boards are allowed"
            )
        greenhouse_boards: list[GreenhouseBoard] = []
        greenhouse_seen: set[str] = set()
        for index, item in enumerate(boards_raw):
            board = _mapping(item, f"Greenhouse board {index}")
            if set(board) != {"token"}:
                raise SourceConfigError("Greenhouse board keys must be exactly token")
            token = _string(
                board["token"], "Greenhouse board.token", maximum=100, ascii_only=True
            )
            if not _BOARD_ID.fullmatch(token):
                raise SourceConfigError("Greenhouse board.token has invalid format")
            if token in greenhouse_seen:
                raise SourceConfigError("duplicate canonical Greenhouse board")
            greenhouse_seen.add(token)
            greenhouse_boards.append(GreenhouseBoard(token))
        if enabled and not greenhouse_boards:
            raise SourceConfigError("enabled Greenhouse configuration requires boards")
        greenhouse_config = GreenhouseConfig(True, enabled, tuple(greenhouse_boards))
    workable_config = WorkableConfig(False, False, ())
    if "workable" in sources:
        workable = _mapping(sources["workable"], "sources.workable")
        if set(workable) - {"enabled", "boards"} or "enabled" not in workable:
            raise SourceConfigError("Workable keys must be exactly enabled and boards")
        enabled = workable["enabled"]
        if not isinstance(enabled, bool):
            raise SourceConfigError("sources.workable.enabled must be boolean")
        boards_raw = workable.get("boards", [])
        if not isinstance(boards_raw, list):
            raise SourceConfigError("sources.workable.boards must be a list")
        if len(boards_raw) > MAX_BOARDS:
            raise SourceConfigError(f"at most {MAX_BOARDS} Workable boards are allowed")
        workable_boards: list[WorkableBoard] = []
        workable_seen: set[str] = set()
        for index, item in enumerate(boards_raw):
            board = _mapping(item, f"Workable board {index}")
            if set(board) != {"subdomain"}:
                raise SourceConfigError("Workable board keys must be exactly subdomain")
            subdomain = _string(
                board["subdomain"],
                "Workable board.subdomain",
                maximum=100,
                ascii_only=True,
            )
            if not _BOARD_ID.fullmatch(subdomain):
                raise SourceConfigError("Workable board.subdomain has invalid format")
            if subdomain in workable_seen:
                raise SourceConfigError("duplicate canonical Workable board")
            workable_seen.add(subdomain)
            workable_boards.append(WorkableBoard(subdomain))
        if enabled and not workable_boards:
            raise SourceConfigError("enabled Workable configuration requires boards")
        workable_config = WorkableConfig(True, enabled, tuple(workable_boards))
    smartrecruiters_config = SmartRecruitersConfig(False, False, ())
    if "smartrecruiters" in sources:
        smartrecruiters = _mapping(
            sources["smartrecruiters"], "sources.smartrecruiters"
        )
        if (
            set(smartrecruiters) - {"enabled", "companies"}
            or "enabled" not in smartrecruiters
        ):
            raise SourceConfigError(
                "SmartRecruiters keys must be exactly enabled and companies"
            )
        enabled = smartrecruiters["enabled"]
        if not isinstance(enabled, bool):
            raise SourceConfigError("sources.smartrecruiters.enabled must be boolean")
        companies_raw = smartrecruiters.get("companies", [])
        if not isinstance(companies_raw, list):
            raise SourceConfigError("sources.smartrecruiters.companies must be a list")
        if len(companies_raw) > MAX_BOARDS:
            raise SourceConfigError(
                f"at most {MAX_BOARDS} SmartRecruiters companies are allowed"
            )
        companies: list[SmartRecruitersCompany] = []
        seen_companies: set[str] = set()
        for index, item in enumerate(companies_raw):
            company_mapping = _mapping(item, f"SmartRecruiters company {index}")
            if set(company_mapping) != {"identifier"}:
                raise SourceConfigError(
                    "SmartRecruiters company keys must be exactly identifier"
                )
            identifier = _string(
                company_mapping["identifier"],
                "SmartRecruiters company.identifier",
                maximum=100,
                ascii_only=True,
            )
            if not _BOARD_ID.fullmatch(identifier):
                raise SourceConfigError(
                    "SmartRecruiters company.identifier has invalid format"
                )
            if identifier in seen_companies:
                raise SourceConfigError("duplicate canonical SmartRecruiters company")
            seen_companies.add(identifier)
            companies.append(SmartRecruitersCompany(identifier))
        if enabled and not companies:
            raise SourceConfigError(
                "enabled SmartRecruiters configuration requires companies"
            )
        smartrecruiters_config = SmartRecruitersConfig(True, enabled, tuple(companies))
    return SourceConfig(
        1,
        lever_config,
        path,
        False,
        greenhouse_config,
        workable_config,
        smartrecruiters_config,
    )


def load_source_config(path: str | os.PathLike[str] | None = None) -> SourceConfig:
    selected, explicit = selected_sources_config_path(path)
    text = _read_secure(selected, required=explicit)
    if text is None:
        return SourceConfig(
            1,
            LeverConfig(False, False, ()),
            selected,
            False,
            GreenhouseConfig(False, False, ()),
            WorkableConfig(False, False, ()),
            SmartRecruitersConfig(False, False, ()),
        )
    result = _parse(text, selected)
    return SourceConfig(
        result.version,
        result.lever,
        selected,
        explicit,
        result.greenhouse,
        result.workable,
        result.smartrecruiters,
    )


__all__ = [
    "MAX_BOARDS",
    "LeverBoard",
    "LeverConfig",
    "GreenhouseBoard",
    "GreenhouseConfig",
    "WorkableBoard",
    "WorkableConfig",
    "SmartRecruitersCompany",
    "SmartRecruitersConfig",
    "SourceConfig",
    "SourceConfigError",
    "load_source_config",
    "selected_sources_config_path",
]
