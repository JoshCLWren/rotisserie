#!/usr/bin/env python3
"""Adopter configuration for the Rotisserie factory runtime.

An adopter repository declares its identity, production endpoint, marker
namespace and model providers in `.rotisserie/config.toml`. Every factory
script reads those values here instead of embedding one repository's names.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

CONFIG_ENV = "ROTISSERIE_CONFIG"
CONFIG_RELATIVE_PATH = Path(".rotisserie/config.toml")
FACTORY_ROOT_ENV = "ROTISSERIE_FACTORY_ROOT"


def default_providers_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get(FACTORY_ROOT_ENV) or str(Path(__file__).resolve().parents[2])
    return Path(root) / "defaults" / "providers.toml"
DEFAULT_MARKER_NAMESPACE = "rotisserie-factory"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    credential: str
    endpoint: str
    name: str
    additional_credentials: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    catalog_url: str = ""

    @property
    def credentials(self) -> tuple[str, ...]:
        return (self.credential, *self.additional_credentials)

    def expand(self, template: str, env: Mapping[str, str]) -> str:
        for name in self.additional_credentials:
            template = template.replace("{" + name + "}", env[name])
        return template

    def resolved_endpoint(self, env: Mapping[str, str]) -> str:
        return self.expand(self.endpoint, env)

    def resolved_catalog_url(self, env: Mapping[str, str]) -> str:
        if self.catalog_url:
            return self.expand(self.catalog_url, env)
        return self.resolved_endpoint(env).rstrip("/") + "/models"


@dataclass(frozen=True)
class AdopterConfig:
    owner: str
    name: str
    host: str = "github.com"
    production_url: str = ""
    marker_namespace: str = DEFAULT_MARKER_NAMESPACE
    providers: Mapping[str, ProviderSpec] = field(default_factory=dict)
    extra_secrets: tuple[str, ...] = ()
    path: Path | None = None

    @property
    def repository(self) -> str:
        if not self.owner or not self.name:
            raise ConfigError(
                f"repository is unknown; set [repository] in {CONFIG_RELATIVE_PATH} "
                "or GITHUB_REPOSITORY"
            )
        return f"{self.owner}/{self.name}"

    def secret_names(self) -> tuple[str, ...]:
        names = {name for spec in self.providers.values() for name in spec.credentials}
        names.update(self.extra_secrets)
        return tuple(sorted(names))


def _candidate_paths(environ: Mapping[str, str]) -> list[Path]:
    if explicit := environ.get(CONFIG_ENV):
        return [Path(explicit)]
    roots = [Path(environ[key]) for key in ("GITHUB_WORKSPACE",) if environ.get(key)]
    roots.append(Path.cwd())
    return [root / CONFIG_RELATIVE_PATH for root in roots]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def _provider(source: str, raw: Mapping[str, Any]) -> ProviderSpec:
    try:
        return ProviderSpec(
            provider=str(raw.get("provider", source)),
            credential=str(raw["credential"]),
            endpoint=str(raw["endpoint"]),
            name=str(raw.get("name", source)),
            additional_credentials=tuple(
                str(item) for item in raw.get("additional_credentials", ())
            ),
            catalog_url=str(raw.get("catalog_url", "")),
            headers=tuple(
                (str(key), str(value)) for key, value in dict(raw.get("headers", {})).items()
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"provider {source!r} is missing {exc.args[0]!r}") from exc


def _providers(raw: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, ProviderSpec]:
    section = dict(raw.get("providers", {}))
    mode = section.pop("mode", "extend")
    if mode not in {"extend", "replace"}:
        raise ConfigError("providers.mode must be 'extend' or 'replace'")
    tables: dict[str, Any] = {}
    defaults = default_providers_path(environ)
    if mode == "extend":
        if not defaults.is_file():
            raise ConfigError(f"default providers not found at {defaults}; set {FACTORY_ROOT_ENV}")
        tables.update(_read_toml(defaults).get("providers", {}))
    disabled = set(section.pop("disabled", ()))
    tables.update(section)
    return {
        source: _provider(source, table)
        for source, table in tables.items()
        if source not in disabled
    }


def _repository_from_env(environ: Mapping[str, str]) -> tuple[str, str]:
    owner, _, name = environ.get("GITHUB_REPOSITORY", "").partition("/")
    return owner, name


def parse(
    raw: Mapping[str, Any], environ: Mapping[str, str], path: Path | None = None
) -> AdopterConfig:
    repository = dict(raw.get("repository", {}))
    if repository.get("owner") and repository.get("name"):
        owner, name = str(repository["owner"]), str(repository["name"])
    else:
        owner, name = _repository_from_env(environ)
    return AdopterConfig(
        owner=owner,
        name=name,
        host=str(repository.get("host", "github.com")),
        production_url=str(dict(raw.get("production", {})).get("url", "")),
        marker_namespace=str(
            dict(raw.get("markers", {})).get("namespace", DEFAULT_MARKER_NAMESPACE)
        ),
        providers=_providers(raw, environ),
        extra_secrets=tuple(str(item) for item in dict(raw.get("secrets", {})).get("extra", ())),
        path=path,
    )


def load(environ: Mapping[str, str] | None = None) -> AdopterConfig:
    environ = os.environ if environ is None else environ
    for path in _candidate_paths(environ):
        if path.is_file():
            return parse(_read_toml(path), environ, path)
    if environ.get(CONFIG_ENV):
        raise ConfigError(f"{CONFIG_ENV} points to a missing file: {environ[CONFIG_ENV]}")
    return parse({}, environ)


@cache
def current() -> AdopterConfig:
    return load()


def repository(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    if slug := environ.get("GITHUB_REPOSITORY"):
        return slug
    return (current() if environ is os.environ else load(environ)).repository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "field",
        choices=(
            "repository",
            "owner",
            "name",
            "production-url",
            "marker-namespace",
            "secrets",
            "providers",
            "json",
        ),
    )
    args = parser.parse_args(argv)
    try:
        config = load()
    except ConfigError as exc:
        print(f"rotisserie config: {exc}", file=sys.stderr)
        return 2
    values: dict[str, Any] = {
        "repository": repository(),
        "owner": config.owner,
        "name": config.name,
        "production-url": config.production_url,
        "marker-namespace": config.marker_namespace,
        "secrets": "\n".join(config.secret_names()),
        "providers": "\n".join(sorted(config.providers)),
    }
    if args.field == "json":
        print(
            json.dumps(
                {
                    **values,
                    "secrets": list(config.secret_names()),
                    "providers": sorted(config.providers),
                }
            )
        )
    else:
        print(values[args.field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
