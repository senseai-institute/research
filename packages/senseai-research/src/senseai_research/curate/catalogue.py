# SPDX-License-Identifier: Apache-2.0
"""The path catalogue: which schema paths to fill for a clade, and how to search for each.

A catalogue entry says what a path means, which units are acceptable, and
the organ and measurement terms the search step combines with the clade's
names. Paths the profile rules consume come first (``senseai-profiles rules
--paths``), then the sense fields the schema offers for every sense the clade
has, then behaviours.

The catalogue is data, not biology: it never contains a value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from senseai_profiles.rules import all_rules


@dataclass(frozen=True, slots=True)
class PathSpec:
    path: str  # e.g. senses.vision.detection_threshold
    description: str
    units: tuple[str, ...]  # accepted unit spellings (pint-parseable or literal)
    organ_terms: tuple[str, ...]
    measure_terms: tuple[str, ...]
    priority: int = 2  # 0 = consumed by a profile rule, 1 = standard field, 2 = nice to have

    @property
    def sense(self) -> str | None:
        p = self.path.split(".")
        return p[1] if p[0] == "senses" else None


# Sense id → organ search terms. A clade's catalogue uses the senses it declares
# (from its existing entry) or the defaults for its order.
SENSE_TERMS: Mapping[str, tuple[str, ...]] = {
    "vision": ("vision", "visual", "retina", "eye", "photoreceptor", "scotopic"),
    "hearing": ("hearing", "auditory", "audiogram", "cochlea", "sound localization", "pinna"),
    "vibrissae": ("vibrissae", "whisker", "sinus hair", "mechanoreceptor"),
    "touch": ("touch", "tactile", "mechanoreceptor", "somatosensory"),
    "echolocation": ("echolocation", "sonar", "biosonar", "ultrasound"),
    "olfaction": ("olfaction", "olfactory", "smell", "odor"),
    "chemotactile": ("chemotactile", "sucker", "chemosensory"),
    "proprioception": ("proprioception", "proprioceptive"),
    "equilibrium": ("vestibular", "equilibrium", "balance"),
}

# Generic field specs, instantiated per sense.
_FIELDS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], int], ...] = (
    (
        "detection_threshold",
        "Smallest stimulus reliably detected (absolute threshold)",
        ("log10 re human", "lux", "cd/m^2", "photons at cornea", "dB SPL", "N", "mN", "um"),
        ("absolute threshold", "detection threshold", "sensitivity", "minimum detectable"),
        1,
    ),
    (
        "range",
        "Operating range of the stimulus dimension (frequency, wavelength, distance)",
        ("Hz", "kHz", "nm", "m", "lux"),
        ("range", "audiogram", "frequency range", "spectral sensitivity", "detection distance"),
        1,
    ),
    (
        "latency_ms",
        "Stimulus-to-response latency of the fastest behaviourally relevant response",
        ("ms", "s"),
        ("latency", "reaction time", "response time", "orienting"),
        1,
    ),
    (
        "bandwidth",
        "Temporal bandwidth (flicker fusion, temporal resolution)",
        ("Hz",),
        (
            "flicker fusion",
            "critical fusion frequency",
            "temporal resolution",
            "temporal modulation",
        ),
        1,
    ),
    (
        "spatial_resolution",
        "Finest spatial discrimination (acuity, minimum audible angle, two-point)",
        ("cycles/deg", "deg", "arcmin", "mm"),
        ("acuity", "spatial resolution", "minimum audible angle", "resolving power", "grating"),
        1,
    ),
    (
        "field_of_view_deg",
        "Visual or sensory field extent",
        ("deg",),
        ("visual field", "field of view", "binocular overlap", "panoramic"),
        2,
    ),
)

# Named measurements per sense that profiles or the schema examples use.
_MEASUREMENTS: Mapping[str, tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]] = {
    "vision": (
        (
            "rod_density_peak",
            "Peak rod photoreceptor density",
            ("cells/mm^2",),
            ("rod density", "photoreceptor density", "rods per"),
        ),
        (
            "cone_density_peak",
            "Peak cone photoreceptor density",
            ("cells/mm^2",),
            ("cone density", "photoreceptor density", "cones per"),
        ),
        (
            "pupil_area_range",
            "Ratio of maximum to minimum pupil area",
            ("ratio",),
            ("pupil area", "pupil size", "pupil dilation range"),
        ),
        (
            "tapetal_gain",
            "Light gain attributed to the tapetum lucidum",
            ("ratio",),
            ("tapetum lucidum", "tapetal reflectance"),
        ),
    ),
    "hearing": (
        (
            "best_frequency_khz",
            "Frequency of lowest threshold",
            ("kHz", "Hz"),
            ("best frequency", "most sensitive frequency"),
        ),
    ),
    "vibrissae": (
        (
            "afferent_unit_types",
            "Number of mechanoreceptive afferent types",
            ("count",),
            ("afferent", "receptor types"),
        ),
        (
            "whisker_count",
            "Number of mystacial vibrissae",
            ("count",),
            ("number of vibrissae", "whisker count"),
        ),
    ),
    "echolocation": (
        (
            "search_call_band",
            "Frequency band of search-phase calls",
            ("Hz", "kHz"),
            ("search call", "call frequency"),
        ),
        (
            "detection_range_small_target",
            "Detection distance for a small target",
            ("m",),
            ("detection range", "target detection"),
        ),
    ),
}

# Behaviours per sense id that commonly carry a latency.
_BEHAVIOURS: Mapping[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "vision": (
        (
            "pounce",
            "Stalk-and-pounce predatory strike",
            ("pounce", "predatory strike", "attack latency", "prey capture"),
        ),
    ),
    "hearing": (
        (
            "acoustic_orienting",
            "Orienting response to a sound",
            ("orienting response", "head turn", "pinna movement"),
        ),
    ),
    "echolocation": (
        (
            "aerial_hawking",
            "Aerial insect capture by echolocation",
            ("terminal buzz", "prey capture", "approach phase"),
        ),
    ),
    "touch": (
        (
            "touch_foraging",
            "Foraging by touch",
            ("foraging", "prey identification", "handling time"),
        ),
    ),
}


def consumed_paths(clade_key: str) -> dict[str, str]:
    """``path → rule`` for library paths any shipped profile rule reads under this clade."""
    out: dict[str, str] = {}
    try:
        from senseai_profiles.library import load_library
        from senseai_profiles.spec import load_specs

        from senseai_research.curate import profiles_root

        root = profiles_root()
        lib = load_library(root / "library")
        specs = load_specs(root / "packages" / "senseai-profiles" / "profiles", lib)
    except Exception:  # library or specs unavailable: no priority boost
        return out
    for spec in specs.values():
        for _s, _f, call in spec.rule_calls():
            for p in call.library_paths.values():
                if p.startswith(clade_key + "."):
                    out[p[len(clade_key) + 1 :]] = call.name
    return out


def rule_paths() -> dict[str, tuple[str, ...]]:
    """Rule name → units accepted on its library arguments (``senseai-profiles rules --paths``)."""
    return {
        name: tuple(u for units in r.info.library_args.values() for u in units)
        for name, r in all_rules().items()
        if r.info.library_args
    }


def build_catalogue(
    clade_key: str, senses: Iterable[str], *, extra_consumed: Mapping[str, str] | None = None
) -> list[PathSpec]:
    """The ordered list of paths to fill for a clade with the given sense ids."""
    consumed = dict(consumed_paths(clade_key))
    consumed.update(extra_consumed or {})
    out: list[PathSpec] = []
    for sense in senses:
        organ = SENSE_TERMS.get(sense, (sense,))
        for fld, desc, units, terms, prio in _FIELDS:
            path = f"senses.{sense}.{fld}"
            out.append(
                PathSpec(
                    path, f"{sense}: {desc}", units, organ, terms, 0 if path in consumed else prio
                )
            )
        for mid, desc, units, terms in _MEASUREMENTS.get(sense, ()):
            path = f"senses.{sense}.measurements.{mid}"
            out.append(
                PathSpec(
                    path, f"{sense}: {desc}", units, organ, terms, 0 if path in consumed else 2
                )
            )
        for name, desc, terms in _BEHAVIOURS.get(sense, ()):
            path = f"behaviours.{name}.latency_ms"
            out.append(
                PathSpec(
                    path,
                    f"{name}: {desc} (latency)",
                    ("ms", "s"),
                    organ,
                    terms,
                    0 if path in consumed else 1,
                )
            )
    out.sort(key=lambda p: (p.priority, p.path))
    # dedupe behaviours shared by several senses
    seen: set[str] = set()
    uniq: list[PathSpec] = []
    for p in out:
        if p.path not in seen:
            seen.add(p.path)
            uniq.append(p)
    return uniq


@dataclass(frozen=True, slots=True)
class Catalogue:
    clade_key: str
    paths: tuple[PathSpec, ...]
    consumed: Mapping[str, str] = field(default_factory=dict)

    def by_path(self) -> dict[str, PathSpec]:
        return {p.path: p for p in self.paths}

    def prompt_text(self) -> str:
        """The catalogue as the model sees it."""
        lines = ["Catalogue of library paths (path | meaning | accepted units):"]
        for p in self.paths:
            lines.append(f"- {p.path} | {p.description} | {', '.join(p.units)}")
        return "\n".join(lines)


def catalogue_for(clade_key: str, senses: Iterable[str]) -> Catalogue:
    paths = build_catalogue(clade_key, senses)
    return Catalogue(clade_key, tuple(paths), consumed_paths(clade_key))


__all__ = [
    "SENSE_TERMS",
    "Catalogue",
    "PathSpec",
    "build_catalogue",
    "catalogue_for",
    "consumed_paths",
    "rule_paths",
]
