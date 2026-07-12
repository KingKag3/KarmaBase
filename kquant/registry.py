"""Strategy registry.

Discovers strategy specs by scanning `strategies/*.md` for an embedded
`# kquant-manifest` YAML block, and builds a runnable engine config from a
manifest + user-chosen variant/instrument/param overrides.

This is the bridge that lets an md file (a human spec) drive a backtest: the
prose is for people, the manifest block is for the machine. Adding a new md with
a manifest makes it appear in the GUI automatically. New *engine families*
(beyond "orb") still require a builder registered in FAMILY_BUILDERS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config as cfgmod

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"
_MANIFEST_RE = re.compile(r"```ya?ml\s*\n(#\s*kquant-manifest.*?)\n```", re.DOTALL)


@dataclass
class Manifest:
    id: str
    family: str
    name: str
    description: str
    variants: list[str]
    instruments: list[str]
    params: dict            # param -> schema (default/choices/min/max/step/label)
    status: str = ""
    path: Path = None
    raw: dict = field(default_factory=dict)

    def defaults(self) -> dict:
        return {k: v.get("default") for k, v in self.params.items()}


def parse_manifest(md_text: str) -> dict | None:
    m = _MANIFEST_RE.search(md_text)
    if not m:
        return None
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) and "family" in data else None


def _to_manifest(data: dict, path: Path) -> Manifest:
    return Manifest(
        id=data.get("id", path.stem),
        family=data["family"],
        name=data.get("name", data.get("id", path.stem)),
        description=data.get("description", ""),
        variants=data.get("variants", ["default"]),
        instruments=data.get("instruments", list(cfgmod.INSTRUMENTS)),
        params=data.get("params", {}),
        status=data.get("status", ""),
        path=path,
        raw=data,
    )


def discover(strategies_dir: Path | str = STRATEGIES_DIR) -> list[Manifest]:
    out = []
    for md in sorted(Path(strategies_dir).glob("*.md")):
        if md.name.startswith("_"):
            continue
        data = parse_manifest(md.read_text(encoding="utf-8"))
        if data:
            out.append(_to_manifest(data, md))
    return out


def get(strategy_id: str, strategies_dir: Path | str = STRATEGIES_DIR) -> Manifest:
    for man in discover(strategies_dir):
        if man.id == strategy_id:
            return man
    raise KeyError(f"No strategy manifest with id={strategy_id!r}")


# ---------------------------------------------------------------------------
# family builders: manifest + choices -> engine config
# ---------------------------------------------------------------------------
def _build_orb(variant: str, instrument: str, overrides: dict):
    return cfgmod.PRESETS[variant](instrument, **overrides)


FAMILY_BUILDERS = {"orb": _build_orb}


def build_config(manifest: Manifest, variant: str, instrument: str, overrides: dict | None = None):
    if manifest.family not in FAMILY_BUILDERS:
        raise NotImplementedError(
            f"No engine builder for family {manifest.family!r}. "
            f"Register one in registry.FAMILY_BUILDERS.")
    clean = {k: v for k, v in (overrides or {}).items() if v is not None}
    return FAMILY_BUILDERS[manifest.family](variant, instrument, clean)
