"""Generator catalog: every library asset is (family, generator, parameter set).

Adding a size or variant = adding a parameter set here. The build regenerates
STEP and USD for every entry; IDs persist through assets/ids.lock.json.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from generators.propulsion import gearbox, main_engine, propeller_cpp, shaft_line


@dataclass(frozen=True)
class Entry:
    family: str          # library folder
    version: str         # asset semantic version; bump when generator output changes
    generate: Callable[[Any], Any]
    params: Any

    @property
    def name(self) -> str:
        return self.params.name

    def params_dict(self) -> dict:
        return asdict(self.params)


CATALOG: list[Entry] = [
    Entry("propeller_cpp", "0.1.0", propeller_cpp.generate, propeller_cpp.PropellerParams()),
    Entry("propeller_cpp", "0.1.0", propeller_cpp.generate,
          propeller_cpp.PropellerParams(name="propeller_cpp_5b", blades=5, expanded_area_ratio=0.62,
                                        palm_diameter=560.0, blade_bolts=6)),
    Entry("shaft_line", "0.1.0", shaft_line.generate, shaft_line.ShaftLineParams()),
    Entry("gearbox", "0.1.0", gearbox.generate, gearbox.GearboxParams()),
    Entry("main_engine", "0.1.0", main_engine.generate, main_engine.MainEngineParams()),
]

# Variant wrappers: one library asset that switches between catalog entries.
VARIANTS = {
    "propeller_cpp": {"set": "bladeCount", "default": "4",
                      "options": {"4": "propeller_cpp_4b", "5": "propeller_cpp_5b"}},
}
