"""Generator catalog: every library asset is (family, generator, parameter set).

Adding a size or variant = adding a parameter set here. The build regenerates
STEP and USD for every entry; IDs persist through assets/ids.lock.json.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from generators.hull import hull_block
from generators.maneuvering import rudder, steering_gear, tunnel_thruster
from generators.propulsion import gearbox, main_engine, propeller_cpp, shaft_line

# Bow thruster position in the ship frame (mm); the tunnel is trimmed to the
# hull here, so arrangement.yaml must place it at the same point.
BOW_THRUSTER_AT = (71000.0, 0.0, 2200.0)


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
    Entry("hull_block", "0.1.0", hull_block.generate, hull_block.STERN),
    Entry("hull_block", "0.1.0", hull_block.generate, hull_block.ENGINE_ROOM),
    Entry("hull_block", "0.1.0", hull_block.generate, hull_block.BOW),
    Entry("rudder", "0.1.0", rudder.generate, rudder.RudderParams()),
    Entry("steering_gear", "0.1.0", steering_gear.generate, steering_gear.SteeringGearParams()),
    Entry("tunnel_thruster", "0.1.0", tunnel_thruster.generate,
          tunnel_thruster.TunnelThrusterParams(tunnel_length=9000.0, ship_x=BOW_THRUSTER_AT[0],
                                               ship_z=BOW_THRUSTER_AT[2])),
]

# Variant wrappers: one library asset that switches between catalog entries.
VARIANTS = {
    "propeller_cpp": {"set": "bladeCount", "default": "4",
                      "options": {"4": "propeller_cpp_4b", "5": "propeller_cpp_5b"}},
}
