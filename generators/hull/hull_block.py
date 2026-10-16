"""Hull block generator: shell, decks/flats, web frames, bulkheads, inner
bottom, girders, engine seatings, skeg and transom for one x-range of the
hull form.

Local frame = ship frame (blocks are placed at the origin), millimetres.
Plates are thin solids with their real thicknesses, so masses come out of
volume x density. Openings (shaft, rudder stock, thruster tunnel) are cut
through every plate they cross.

This is a structural stand-in at the detail level the viewer and the graph
need (L1/L2 of the plan). Structural design data proper arrives through the
OCX adapter in phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from build123d import (Align, Axis, Box, Compound, Cylinder, Face, Kind, Plane, Polyline, Pos, Rot, Solid,
                       Vector, Wire, extrude, loft, offset)

from generators.common import assembly, part
from generators.hull.hullform import FORM, HullForm

C, MIN, MAX = Align.CENTER, Align.MIN, Align.MAX


@dataclass(frozen=True)
class Opening:
    """Cylindrical cut. axis: 'x' longitudinal, 'y' transverse, 'z' vertical."""
    name: str
    axis: str
    center: tuple[float, float, float]
    radius: float
    span: tuple[float, float] = (-12000.0, 12000.0)   # extent along the axis (keep finite: long cutters hurt boolean tolerance)


@dataclass(frozen=True)
class HullBlockParams:
    name: str
    x0: float
    x1: float
    code_suffix: str = ""
    shell_t: float = 14.0
    deck_t: float = 12.0
    web_t: float = 12.0
    web_depth: float = 450.0
    bulkhead_t: float = 11.0
    webs: tuple[float, ...] = ()
    bulkheads: tuple[float, ...] = ()
    inner_bottom: tuple[float, float, float] | None = None   # (x0, x1, height)
    centre_girder: tuple[float, float] | None = None          # (x0, x1), up to inner bottom
    seatings: tuple[tuple[float, float, float, float], ...] = ()  # (x0, x1, y, top_z) pairs at +/- y
    flats: tuple[tuple[str, float, float, float], ...] = ()   # (name, x0, x1, z)
    skeg: bool = False
    transom: bool = False
    openings: tuple[Opening, ...] = ()
    station_step: float = 1000.0
    ruled: bool = False        # straight between stations; avoids spline overshoot where sections change fast
    form: HullForm = field(default=FORM)


# ------------------------------------------------------------------ helpers
def _offset_inward(pts: list[tuple[float, float]], t: float) -> list[tuple[float, float]]:
    """Offset an open section polyline (port deck -> keel -> starboard deck) by t towards the inside."""
    P = np.array(pts, dtype=float)
    out = []
    for i in range(len(P)):
        a = P[max(i - 1, 0)]
        b = P[min(i + 1, len(P) - 1)]
        d = b - a
        n = np.array([-d[1], d[0]])          # left normal of travel direction
        n /= np.linalg.norm(n) or 1.0
        out.append(tuple(P[i] + n * t))
    return out


def _wire(pts3) -> Wire:
    return Wire(Polyline(*pts3, close=True).edges())


def _inner_open_path(form: HullForm, x: float, t: float) -> list[tuple[float, float]]:
    """Inner skin of the section at station x, from port deck level round the
    keel to starboard deck level: the proper 2D inward offset of the closed
    section (uniform thickness, no fold-over at narrow or sharp sections)."""
    top = form.depth + 3 * t
    inner = _inset(_closed_section(form, x, top), t)
    vs = [(py, pz) for (_, py, pz) in _ordered_points(inner)]
    # order the loop so it runs port top -> bottom -> starboard top
    n = len(vs)
    k_port = max(range(n), key=lambda i: (vs[i][1] > top - 3 * t, vs[i][0]))
    loop = vs[k_port:] + vs[:k_port]
    if loop[1][1] > loop[-1][1]:            # wrong direction: we are on the top edge
        loop = [loop[0]] + list(reversed(loop[1:]))
    path = list(loop)  # the closing edge (stbd top -> port top) is the top line we discard
    # bring both end points down to deck level along their side edges
    def to_deck(p_top, p_next):
        (y0, z0), (y1, z1) = p_top, p_next
        f = (z0 - form.depth) / (z0 - z1)
        return (y0 + f * (y1 - y0), form.depth)
    path[0] = to_deck(path[0], path[1])
    path[-1] = to_deck(path[-1], path[-2])
    return path


def _normal_thickness_inner(form: HullForm, x: float, t: float, dx: float = 150.0) -> list[tuple[float, float]]:
    """Inner skin points at station x for a plate of thickness t measured
    normal to the hull surface. In-section offset d = t * sqrt(1 + s^2), where
    s is the rate at which the outline moves along its in-plane normal per
    unit x (flare / entrance angle); a plain in-plane offset of t would make
    inclined plating (bow, stern) up to ~40 % too thin."""
    P = np.array(form.section_full(x), dtype=float)
    Pa = np.array(form.section_full(x - dx), dtype=float)
    Pb = np.array(form.section_full(x + dx), dtype=float)
    dPdx = (Pb - Pa) / (2 * dx)
    out = []
    centre_z = 0.5 * (P[:, 1].max() + P[:, 1].min())
    for i in range(len(P)):
        a, b = P[max(i - 1, 0)], P[min(i + 1, len(P) - 1)]
        d = b - a
        n = np.array([-d[1], d[0]])
        n /= np.linalg.norm(n) or 1.0
        # inward = towards the centreline / up from the keel
        probe = np.array([0.0, centre_z]) - P[i]
        if np.dot(n, probe) < 0:
            n = -n
        slope = float(np.dot(dPdx[i], n))
        dist = min(t * np.sqrt(1.0 + slope * slope), 3.0 * t)
        out.append(tuple(P[i] + n * dist))
    # keep the deck-edge points at deck level
    out[0] = (out[0][0], form.depth)
    out[-1] = (out[-1][0], form.depth)
    return out


def _section_ring(form: HullForm, x: float, t: float) -> Wire:
    outer = form.section_full(x)
    inner = _normal_thickness_inner(form, x, t)
    loop = outer + list(reversed(inner))
    return _wire([Vector(x, y, z) for (y, z) in loop])


def _section_face(form: HullForm, x: float, inset: float) -> Face:
    """Inside of the hull at station x (closed at deck level), inset from the shell."""
    outer = form.section_full(x)
    inner = _offset_inward(outer, inset)
    if inner[len(inner) // 2][1] < outer[len(outer) // 2][1]:
        inner = _offset_inward(outer, -inset)
    top = form.depth - inset
    inner = [(y, min(z, top)) for (y, z) in inner]
    return Face(_wire([Vector(x, y, z) for (y, z) in inner]))


def _closed_section(form: HullForm, x: float, top: float) -> Face:
    """Outer section outline closed by a straight line at height `top`."""
    pts = form.section_full(x)
    (y0, _), (y1, _) = pts[0], pts[-1]
    pts = [(y0, top)] + pts[1:-1] + [(y1, top)]
    return Face(_wire([Vector(x, y, z) for (y, z) in pts]))


def _nurbs(face: Face) -> Face:
    """Convert to plain B-spline geometry. Offsets of curves are stored as
    offset-curve/-surface types that the STEP writer drops silently, which
    loses those faces on exchange."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from OCP.TopoDS import TopoDS
    conv = BRepBuilderAPI_NurbsConvert(face.wrapped, True)
    return Face(TopoDS.Face(conv.Shape()))


def _inset(face: Face, t: float) -> Face:
    """Planar inward offset; the sign of build123d's offset depends on the wire
    winding, so keep whichever result is smaller."""
    a = offset(face, amount=-t, kind=Kind.INTERSECTION)
    a = a.face() if hasattr(a, "face") else a
    if a.area >= face.area:
        a = offset(face, amount=t, kind=Kind.INTERSECTION)
        a = a.face() if hasattr(a, "face") else a
    return _nurbs(a)


def _ordered_points(face: Face) -> list[tuple[float, float, float]]:
    """Vertices of the face's outer wire in traversal order."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools_WireExplorer
    exp = BRepTools_WireExplorer(face.outer_wire().wrapped)
    pts = []
    while exp.More():
        p = BRep_Tool.Pnt_s(exp.CurrentVertex())
        pts.append((p.X(), p.Y(), p.Z()))
        exp.Next()
    return pts


def _stations(p: HullBlockParams) -> list[float]:
    n = max(2, int(round((p.x1 - p.x0) / p.station_step)) + 1)
    return list(np.linspace(p.x0, p.x1, n))


def _cutter(o: Opening):
    r = o.radius
    length = o.span[1] - o.span[0]
    if o.axis == "x":
        return Pos(o.span[0], o.center[1], o.center[2]) * Rot(0, 90, 0) * Cylinder(r, length, align=(C, C, MIN))
    if o.axis == "y":
        return Pos(o.center[0], o.span[0], o.center[2]) * Rot(-90, 0, 0) * Cylinder(r, length, align=(C, C, MIN))
    return Pos(o.center[0], o.center[1], o.span[0]) * Cylinder(r, length, align=(C, C, MIN))


def _cut(shape, openings):
    for o in openings:
        try:
            c = _cutter(o)
            bb_s, bb_c = shape.bounding_box(), c.bounding_box()
            if bb_s.overlaps(bb_c) if hasattr(bb_s, "overlaps") else True:
                shape = shape - c
        except Exception:  # pragma: no cover - keep the plate if a cut fails
            pass
    return shape


def _plate_xy(points_xy: list[tuple[float, float]], z0: float, t: float):
    """Horizontal plate from a plan outline, thickness t downward from z0."""
    w = _wire([Vector(x, y, z0 - t) for (x, y) in points_xy])
    return extrude(Face(w), amount=t, dir=(0, 0, 1))


def _plate_xz(points_xz: list[tuple[float, float]], y0: float, t: float):
    """Vertical longitudinal plate from an outline in the x-z plane, centred on y0."""
    w = _wire([Vector(x, y0 - t / 2, z) for (x, z) in points_xz])
    return extrude(Face(w), amount=t, dir=(0, 1, 0))


# ------------------------------------------------------------------ generator
def generate(p: HullBlockParams):
    f = p.form
    xs = _stations(p)
    children = []

    shell = loft([Face(_section_ring(f, x, p.shell_t)) for x in xs], ruled=p.ruled)
    shell = _cut(shell, p.openings)
    children.append(part("Shell", "ShellPlating", shell, "paint_hull_shell",
                         "watertight envelope; carries global hull girder loads", thickness=p.shell_t))

    # main deck
    port = [(x, f.halfbreadth_deck(x) - p.shell_t) for x in xs]
    outline = port + [(x, -y) for (x, y) in reversed(port)]
    deck = _cut(_plate_xy(outline, f.depth, p.deck_t), p.openings)
    children.append(part("MainDeck", "DeckPlating", deck, "steel_ah36",
                         "weather deck; closes the hull girder and carries deck loads", thickness=p.deck_t))

    for (nm, fx0, fx1, z) in p.flats:
        fxs = list(np.linspace(fx0, fx1, max(2, int((fx1 - fx0) / 1000) + 1)))
        pp = [(x, f.halfbreadth_at(x, z) - p.shell_t) for x in fxs]
        flat = _cut(_plate_xy(pp + [(x, -y) for (x, y) in reversed(pp)], z, p.deck_t), p.openings)
        children.append(part(nm, "DeckPlating", flat, "steel_ah36",
                             "internal flat supporting machinery", thickness=p.deck_t))

    if p.inner_bottom:
        ix0, ix1, h = p.inner_bottom
        ixs = list(np.linspace(ix0, ix1, max(2, int((ix1 - ix0) / 1000) + 1)))
        pp = [(x, f.halfbreadth_at(x, h) - p.shell_t) for x in ixs]
        ib = _cut(_plate_xy(pp + [(x, -y) for (x, y) in reversed(pp)], h, p.deck_t), p.openings)
        children.append(part("InnerBottom", "InnerBottom", ib, "steel_ah36",
                             "double-bottom tank top; second barrier against grounding damage",
                             thickness=p.deck_t))

    if p.centre_girder:
        gx0, gx1 = p.centre_girder
        top = p.inner_bottom[2] - p.deck_t if p.inner_bottom else 1200.0
        gxs = list(np.linspace(gx0, gx1, 6))
        outline = [(x, f.keel_z(x) + p.shell_t) for x in gxs] + [(gx1, top), (gx0, top)]
        cg = _cut(_plate_xz(outline, 0.0, 14.0), p.openings)
        children.append(part("CentreGirder", "Girder", cg, "steel_ah36",
                             "longitudinal strength member on the centreline of the double bottom",
                             thickness=14.0))

    for i, (sx0, sx1, y, top) in enumerate(p.seatings):
        base = p.inner_bottom[2] if p.inner_bottom else 0.0
        for side, sy in (("P", y), ("S", -y)):
            s = _plate_xz([(sx0, base), (sx1, base), (sx1, top), (sx0, top)], sy, 25.0)
            children.append(part(f"EngineSeating_{side}", "Girder", s, "steel_ah36",
                                 "longitudinal seating girder carrying main engine loads into the double bottom",
                                 thickness=25.0))

    for i, x in enumerate(p.webs):
        outer = _section_face(f, x, p.shell_t)
        try:
            inner = offset(outer, amount=-p.web_depth)
            ring = outer - inner
        except Exception:  # pragma: no cover
            ring = outer
        web = extrude(ring, amount=p.web_t, dir=(1, 0, 0))
        web = _cut(web, p.openings)
        children.append(part(f"WebFrame_{int(round(x)):06d}", "WebFrame", web, "steel_ah36",
                             "transverse ring supporting shell and deck longitudinals", thickness=p.web_t))

    for x in p.bulkheads:
        bh = extrude(_section_face(f, x, p.shell_t), amount=p.bulkhead_t, dir=(1, 0, 0))
        bh = _cut(bh, p.openings)
        children.append(part(f"Bulkhead_{int(round(x)):06d}", "Bulkhead", bh, "steel_ah36",
                             "watertight transverse bulkhead; subdivision and transverse strength",
                             thickness=p.bulkhead_t))

    if p.skeg:
        sx_aft, sx_fwd = 4400.0, 16500.0
        top = [(x, f.keel_z(x) + 200.0) for x in np.linspace(sx_aft, sx_fwd, 9)]
        bottom = [(sx_fwd, max(0.0, f.keel_z(sx_fwd))), (6200.0, 1250.0), (sx_aft, 1450.0)]
        outline = top + bottom
        outer = _plate_xz(outline, 0.0, 900.0)
        # plated, hollow fin: 20 mm side plates and edges
        inner_face = offset(Face(_wire([Vector(x, 0.0, z) for (x, z) in outline])), amount=-20.0)
        inner = extrude(Pos(0, -430.0, 0) * inner_face, amount=860.0, dir=(0, 1, 0))
        sk = _cut(outer - inner, p.openings)
        children.append(part("Skeg", "Skeg", sk, "paint_hull_shell",
                             "centreline fin carrying the stern tube; directional stability",
                             thickness=20.0))

    if p.transom:
        tr = extrude(_section_face(f, p.x0, 0.0), amount=p.shell_t, dir=(1, 0, 0))
        tr = _cut(tr, p.openings)
        children.append(part("Transom", "Transom", tr, "paint_hull_shell",
                             "flat stern plate closing the hull aft", thickness=p.shell_t))

    blk = assembly(p.name, "HullBlock", "prefabricated hull block: shell, decks and internal structure",
                   children, length=p.x1 - p.x0)
    blk.attrs["generator"] = f"hull.hull_block:{p.name}"
    return blk


# ------------------------------------------------------------------ demo blocks
SHAFT_Z = 2000.0
STERN = HullBlockParams(
    name="hull_block_b01_stern", x0=-3000.0, x1=16000.0,
    webs=(2400.0, 4800.0, 7200.0, 9600.0, 12000.0, 14400.0),
    bulkheads=(16000.0 - 11.0,),
    flats=(("SteeringGearFlat", -2600.0, 3600.0, 5800.0),),
    skeg=True, transom=True,
    openings=(
        Opening("shaft", "x", (0.0, 0.0, SHAFT_Z), 300.0, (3500.0, 16500.0)),
        Opening("rudder_stock", "z", (0.0, 0.0, 0.0), 310.0, (3000.0, 7000.0)),
    ),
)
ENGINE_ROOM = HullBlockParams(
    name="hull_block_b02_engine_room", x0=16000.0, x1=30000.0,
    webs=(18000.0, 20400.0, 22800.0, 25200.0, 27600.0),
    bulkheads=(30000.0 - 11.0,),
    inner_bottom=(16000.0, 30000.0, 900.0),
    centre_girder=(16000.0, 30000.0),
    seatings=((19670.0, 24670.0, 800.0, 2375.0),),
)
BOW = HullBlockParams(
    name="hull_block_b10_bow", x0=66000.0, x1=78500.0, station_step=500.0, ruled=True,
    webs=(67200.0, 69300.0, 72600.0, 75600.0),
    bulkheads=(66000.0, 73800.0),
    openings=(Opening("bow_thruster_tunnel", "y", (71000.0, 0.0, 2200.0), 850.0),),
)
