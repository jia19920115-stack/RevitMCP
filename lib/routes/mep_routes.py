# RevitMCP: MEP routes (linked model reading, connectors, fittings, connected pipe runs)
# -*- coding: UTF-8 -*-
"""
Routes added for plumbing/drainage modeling:

  /mep/linked_elements     read walls/floors/columns/rooms/shafts INSIDE Revit links,
                           returned in HOST project coordinates (link transform applied)
  /mep/connectors          list the piping connectors of fixtures, fittings and pipes
  /mep/place_pipe_fittings place a fitting family (e.g. a trap / 存水彎) onto an open
                           connector, oriented, sized and connected
  /mep/create_pipes        pipe runs with: slope_percent, start_connect (connect to a
                           fixture / fitting / pipe end), end_connect (tee into a pipe or
                           connect to a connector). Elbows and tees come from the PIPE
                           TYPE's routing preferences, so SP pipes get SP fittings and WP
                           pipes get WP fittings.

All lengths are millimetres. One transaction per call (one Ctrl+Z), one
sub-transaction per item. Uses the helpers from modeling_routes.
"""

import math

from pyrevit import script, DB
from System.Collections.Generic import List

from routes.revit_compat import get_element_id_text, get_element_id_value, make_element_id
from routes.json_safety import sanitize_for_json
from routes.modeling_routes import (
    MM_PER_FOOT,
    MIN_CURVE_LENGTH_MM,
    _text,
    _name,
    _coerce_bool,
    _to_float,
    _mm_to_ft,
    _ft_to_mm,
    _point_from,
    _error,
    _set_param,
    _all_levels,
    _find_level,
    _symbols_in_category,
    _symbol_label,
    _find_family_symbol,
    _find_system_type,
    _ensure_symbol_active,
    _run_creation,
    _build_response,
    _prepare,
    _merged,
    _check_document,
    _drop_collinear,
)

MEP_ROUTES_VERSION = "2026-10-07-mep-v9e"
# v9: a run may start directly on the outlet of these fittings: the first 45-deg elbow
# sits on the fitting with no pipe between, and only this pair may overlap.
DIRECT_START_FAMILY_KEYS = (u"單口彎頭",)
DIRECT_START_TRIAL_MM = 500.0
RISER_AXIS_Z = 0.9              # |axis.Z| above this = vertical main (riser)
BEND_SPLIT_ABOVE_DEG = 50.0     # corners turning more than this get two 45-deg bends
MIN_CHAMFER_MM = 40.0
SQUARE_BRANCH_TOLERANCE_DEG = 10.0   # branch within 90 +/- this of the main = ordinary tee
OVERLAP_SEARCH_MARGIN_MM = 100.0   # how far around the new fittings to look for neighbours
SIZE_TOLERANCE_MM = 0.5
TRANSITION_STUB_MM = 80.0       # short pipe at the connector's own size before the transition
TRANSITION_ROOM_MM = 120.0      # straight length the transition fitting and the next pipe need
TEE_MIN_CLEARANCE_MM = 100.0
TEE_CLEARANCE_FACTOR = 1.5      # x main diameter
MIN_TEE_CLEARANCE_OVERRIDE_MM = 50.0   # floor for the per-run tee_clearance_mm option
MIN_PIPE_BETWEEN_FITTINGS_MM = 5.0   # straight pipe that must remain between two fittings
TINY_FT = 1e-6
CONNECT_TOLERANCE_MM = 5.0
DEFAULT_END_SNAP_MM = 500.0
END_AS_ELBOW_MM = 30.0


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _xyz_mm(point, level_elevation_ft=None, digits=1):
    data = {
        "x_mm": round(point.X * MM_PER_FOOT, digits),
        "y_mm": round(point.Y * MM_PER_FOOT, digits),
        "z_mm_abs": round(point.Z * MM_PER_FOOT, digits),
    }
    if level_elevation_ft is not None:
        data["z_mm"] = round((point.Z - level_elevation_ft) * MM_PER_FOOT, digits)
    return data


def _dir_dict(vector):
    return {"x": round(vector.X, 4), "y": round(vector.Y, 4), "z": round(vector.Z, 4)}


def _element_by_id(doc, raw_id, label):
    if raw_id is None or _text(raw_id) == u"":
        raise ValueError(u"{}: 'element_id' is required.".format(label))
    try:
        element = doc.GetElement(make_element_id(DB, raw_id))
    except Exception:
        element = None
    if element is None:
        raise ValueError(u"{}: element {} not found in the active document.".format(label, _text(raw_id)))
    return element


def _element_level(doc, element):
    for attr in ("ReferenceLevel", "LevelId"):
        try:
            value = getattr(element, attr, None)
            if value is None:
                continue
            if isinstance(value, DB.Level):
                return value
            level = doc.GetElement(value)
            if isinstance(level, DB.Level):
                return level
        except Exception:
            pass
    return None


def _symbol_by_family(doc, bic, family_name, type_name, label):
    """Like _find_family_symbol, but type_name may be omitted when the family has one type."""
    if _text(type_name):
        return _find_family_symbol(doc, bic, type_name, family_name, label)
    wanted_family = _text(family_name)
    if not wanted_family:
        raise ValueError(u"{}: give family_name (and type_name if the family has several types).".format(label))
    matches = []
    for symbol in _symbols_in_category(doc, bic):
        try:
            if _name(symbol.Family) == wanted_family:
                matches.append(symbol)
        except Exception:
            pass
    if not matches:
        available = sorted(set(_name(s.Family) for s in _symbols_in_category(doc, bic)))
        raise ValueError(u"{}: family '{}' not loaded. Available: {}".format(
            label, wanted_family, u"; ".join(available[:80])))
    if len(matches) > 1:
        raise ValueError(u"{}: family '{}' has several types, pass type_name: {}".format(
            label, wanted_family, u"; ".join(_name(s) for s in matches)))
    return matches[0]


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

def _all_connectors(element):
    manager = None
    try:
        mep_model = getattr(element, "MEPModel", None)
        if mep_model is not None and mep_model.ConnectorManager is not None:
            manager = mep_model.ConnectorManager
    except Exception:
        manager = None
    if manager is None:
        try:
            manager = element.ConnectorManager
        except Exception:
            manager = None
    if manager is None:
        return []
    out = []
    try:
        for connector in manager.Connectors:
            try:
                if connector.ConnectorType == DB.ConnectorType.Logical:
                    continue
            except Exception:
                pass
            out.append(connector)
    except Exception:
        pass
    return out


def _piping_connectors(element):
    out = []
    for connector in _all_connectors(element):
        try:
            if connector.Domain == DB.Domain.DomainPiping:
                out.append(connector)
        except Exception:
            pass
    return out


def _connector_dir(connector):
    try:
        return connector.CoordinateSystem.BasisZ
    except Exception:
        return None


def _connector_classification(connector):
    try:
        return _text(connector.PipeSystemType)
    except Exception:
        return u""


def _connector_diameter_mm(connector):
    try:
        if connector.Shape == DB.ConnectorProfileType.Round:
            return round(connector.Radius * 2.0 * MM_PER_FOOT, 1)
    except Exception:
        pass
    return None


def _connected_owner_ids(connector):
    ids = []
    try:
        own = get_element_id_text(connector.Owner.Id)
        for ref in connector.AllRefs:
            try:
                if ref.ConnectorType == DB.ConnectorType.Logical:
                    continue
                owner_id = get_element_id_text(ref.Owner.Id)
                if owner_id != own and owner_id not in ids:
                    ids.append(owner_id)
            except Exception:
                pass
    except Exception:
        pass
    return ids


def _connector_info(connector, level_elevation_ft=None):
    info = {"connector_id": connector.Id}
    try:
        info.update(_xyz_mm(connector.Origin, level_elevation_ft))
    except Exception:
        pass
    direction = _connector_dir(connector)
    if direction is not None:
        info["direction"] = _dir_dict(direction)
    info["system_classification"] = _connector_classification(connector)
    diameter = _connector_diameter_mm(connector)
    if diameter is not None:
        info["diameter_mm"] = diameter
    try:
        info["is_connected"] = bool(connector.IsConnected)
    except Exception:
        pass
    connected = _connected_owner_ids(connector)
    if connected:
        info["connected_to"] = connected
    try:
        info["flow_direction"] = _text(connector.Direction)
    except Exception:
        pass
    return info


def _classification_matches(connector, classification):
    if not classification:
        return True
    value = _connector_classification(connector).lower()
    if value in (u"", u"fitting", u"global", u"undefinedsystemclassification"):
        return True
    return value == _text(classification).lower()


def _pick_connector(element, label, connector_id=None, near_point=None, classification=None):
    connectors = _piping_connectors(element)
    if not connectors:
        raise ValueError(u"{}: element {} has no piping connectors.".format(label, get_element_id_text(element.Id)))
    if connector_id is not None and _text(connector_id) != u"":
        for connector in connectors:
            if _text(connector.Id) == _text(connector_id):
                return connector
        raise ValueError(u"{}: connector_id {} not found on element {} (has: {}).".format(
            label, _text(connector_id), get_element_id_text(element.Id),
            u", ".join(_text(c.Id) for c in connectors)))

    def free(c):
        try:
            return not c.IsConnected
        except Exception:
            return True

    pool = [c for c in connectors if free(c) and _classification_matches(c, classification)]
    if not pool:
        pool = [c for c in connectors if free(c)]
    if not pool:
        raise ValueError(u"{}: every piping connector on element {} is already connected.".format(
            label, get_element_id_text(element.Id)))
    if near_point is not None and len(pool) > 1:
        pool.sort(key=lambda c: c.Origin.DistanceTo(near_point))
    return pool[0]


def _nearest_connector(element, point):
    best = None
    for connector in _piping_connectors(element):
        try:
            distance = connector.Origin.DistanceTo(point)
        except Exception:
            continue
        if best is None or distance < best[0]:
            best = (distance, connector)
    return best[1] if best else None


def _is_pipe(element):
    try:
        return isinstance(element, DB.Plumbing.Pipe)
    except Exception:
        return False


def _join(doc, pipe_connector, other_connector):
    """Connect a pipe end to another connector. Pipe-to-pipe at an angle gets an elbow."""
    owner = other_connector.Owner
    if _is_pipe(owner):
        d1 = _connector_dir(pipe_connector)
        d2 = _connector_dir(other_connector)
        if d1 is not None and d2 is not None and abs(d1.DotProduct(d2)) < 0.9999:
            fitting = doc.Create.NewElbowFitting(other_connector, pipe_connector)
            return {"mode": "elbow", "fitting_id": get_element_id_text(fitting.Id)}
        try:
            fitting = doc.Create.NewUnionFitting(other_connector, pipe_connector)
            return {"mode": "union", "fitting_id": get_element_id_text(fitting.Id)}
        except Exception:
            pass
    pipe_connector.ConnectTo(other_connector)
    return {"mode": "connected"}


# ---------------------------------------------------------------------------
# Linked models
# ---------------------------------------------------------------------------

_LINK_CATEGORY_KEYS = (
    ("walls", "OST_Walls"),
    ("floors", "OST_Floors"),
    ("structural_columns", "OST_StructuralColumns"),
    ("columns", "OST_Columns"),
    ("beams", "OST_StructuralFraming"),
    ("doors", "OST_Doors"),
    ("windows", "OST_Windows"),
    ("rooms", "OST_Rooms"),
    ("shaft_openings", "OST_ShaftOpening"),
    ("plumbing_fixtures", "OST_PlumbingFixtures"),
    ("generic_models", "OST_GenericModel"),
)
_DEFAULT_LINK_CATEGORIES = ("walls", "floors", "structural_columns", "columns", "doors", "rooms", "shaft_openings")


def _link_category(key):
    wanted = _text(key).lower()
    for k, ost in _LINK_CATEGORY_KEYS:
        if k == wanted:
            bic = getattr(DB.BuiltInCategory, ost, None)
            if bic is not None:
                return k, bic
    raise ValueError(u"Unknown category '{}'. Use: {}".format(key, u", ".join(k for k, _ in _LINK_CATEGORY_KEYS)))


def _transformed_box(element, transform):
    try:
        box = element.get_BoundingBox(None)
    except Exception:
        box = None
    if box is None:
        return None
    xs, ys, zs = [], [], []
    for x in (box.Min.X, box.Max.X):
        for y in (box.Min.Y, box.Max.Y):
            for z in (box.Min.Z, box.Max.Z):
                p = transform.OfPoint(DB.XYZ(x, y, z))
                xs.append(p.X)
                ys.append(p.Y)
                zs.append(p.Z)
    return (DB.XYZ(min(xs), min(ys), min(zs)), DB.XYZ(max(xs), max(ys), max(zs)))


def _curve_points(curve, transform, level_ft):
    out = []
    try:
        if isinstance(curve, DB.Line):
            pts = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        else:
            pts = list(curve.Tessellate())
        for p in pts:
            out.append(_xyz_mm(transform.OfPoint(p), level_ft))
    except Exception:
        pass
    return out


def _loops_from_curve_arrays(curve_array_array, transform, level_ft):
    loops = []
    try:
        for curve_array in curve_array_array:
            loop = []
            for curve in curve_array:
                pts = _curve_points(curve, transform, level_ft)
                if pts:
                    loop.append(pts[0])
            if loop:
                loops.append(loop)
    except Exception:
        pass
    return loops


def _describe_linked(link_doc, element, key, transform, level_ft):
    data = {
        "element_id": get_element_id_text(element.Id),
        "category": key,
    }
    try:
        type_element = link_doc.GetElement(element.GetTypeId())
        if type_element is not None:
            data["type_name"] = _name(type_element)
    except Exception:
        pass
    level = _element_level(link_doc, element)
    if level is not None:
        data["link_level"] = _name(level)
    box = _transformed_box(element, transform)
    if box is not None:
        data["bbox_min"] = _xyz_mm(box[0], level_ft)
        data["bbox_max"] = _xyz_mm(box[1], level_ft)

    if key == "walls":
        try:
            curve = element.Location.Curve
            data["location_line"] = _curve_points(curve, transform, level_ft)
        except Exception:
            pass
        try:
            data["thickness_mm"] = round(element.Width * MM_PER_FOOT, 1)
        except Exception:
            pass
    elif key in ("structural_columns", "columns", "doors", "windows", "plumbing_fixtures", "generic_models"):
        try:
            point = element.Location.Point
            data["location_point"] = _xyz_mm(transform.OfPoint(point), level_ft)
        except Exception:
            try:
                data["location_line"] = _curve_points(element.Location.Curve, transform, level_ft)
            except Exception:
                pass
        if key in ("doors", "windows"):
            try:
                data["host_wall_id"] = get_element_id_text(element.Host.Id)
            except Exception:
                pass
    elif key == "rooms":
        for attr in ("Number", "Name"):
            try:
                data[attr.lower()] = _text(getattr(element, attr))
            except Exception:
                pass
        try:
            loops = []
            options = DB.SpatialElementBoundaryOptions()
            for segment_list in element.GetBoundarySegments(options):
                loop = []
                for segment in segment_list:
                    pts = _curve_points(segment.GetCurve(), transform, level_ft)
                    if pts:
                        loop.append(pts[0])
                if loop:
                    loops.append(loop)
            data["boundary_loops"] = loops
        except Exception:
            pass
    elif key == "floors":
        try:
            sketch = link_doc.GetElement(element.SketchId)
            data["boundary_loops"] = _loops_from_curve_arrays(sketch.Profile, transform, level_ft)
        except Exception:
            pass
    elif key == "shaft_openings":
        try:
            loop = []
            for curve in element.BoundaryCurves:
                pts = _curve_points(curve, transform, level_ft)
                if pts:
                    loop.append(pts[0])
            data["boundary_loops"] = [loop]
        except Exception:
            pass
    return data


# ---------------------------------------------------------------------------
# Fitting placement helpers
# ---------------------------------------------------------------------------

_DIAMETER_PARAM_NAMES = (u"公稱直徑", u"Nominal Diameter", u"直徑", u"Diameter", u"管徑", u"Size")
_RADIUS_PARAM_NAMES = (u"公稱半徑", u"Nominal Radius", u"半徑", u"Radius")


def _set_fitting_size(instance, diameter_ft):
    for names, value in ((_DIAMETER_PARAM_NAMES, diameter_ft), (_RADIUS_PARAM_NAMES, diameter_ft / 2.0)):
        for name in names:
            try:
                param = instance.LookupParameter(name)
            except Exception:
                param = None
            if param is None or param.IsReadOnly or param.StorageType != DB.StorageType.Double:
                continue
            try:
                param.Set(value)
                return u"parameter '{}'".format(name)
            except Exception:
                pass
    for connector in _piping_connectors(instance):
        try:
            connector.Radius = diameter_ft / 2.0
            return u"connector radius"
        except Exception:
            pass
    return None


def _rotate(doc, element_id, origin, axis, angle):
    if abs(angle) < 1e-9:
        return
    line = DB.Line.CreateBound(origin, origin.Add(axis.Normalize()))
    DB.ElementTransformUtils.RotateElement(doc, element_id, line, angle)


def _any_perpendicular(vector):
    trial = DB.XYZ.BasisX if abs(vector.DotProduct(DB.XYZ.BasisX)) < 0.9 else DB.XYZ.BasisY
    return vector.CrossProduct(trial).Normalize()


def _connector_by_id(element, connector_id):
    for connector in _piping_connectors(element):
        if connector.Id == connector_id:
            return connector
    return None


def _inlet_geometry_score(candidate, connectors):
    """How much the fitting 'leaves' along its outlet if `candidate` is the inlet.

    For a P-trap the outlet sits out ahead along its own direction from the inlet
    (vector inlet->outlet is parallel to the outlet direction, score ~1). Taking the
    wrong connector as inlet puts the outlet directly below/beside it (score ~0).
    """
    best = 0.0
    for other in connectors:
        if other.Id == candidate.Id:
            continue
        vec = other.Origin.Subtract(candidate.Origin)
        out_dir = _connector_dir(other)
        if vec.GetLength() < 1e-9 or out_dir is None:
            continue
        best = max(best, abs(vec.Normalize().DotProduct(out_dir)))
    return best


def _guess_fitting_inlet(connectors, wanted_in):
    """Pick the inlet connector of a fitting from its own geometry.

    The old rule (connector already facing the target) depended on how the family
    happened to be inserted, so the same trap came out right one time and rotated
    90 degrees the next. Geometry is independent of insertion orientation.
    """
    scored = sorted(((_inlet_geometry_score(c, connectors), c) for c in connectors),
                    key=lambda pair: pair[0], reverse=True)
    if len(scored) >= 2 and scored[0][0] - scored[1][0] > 0.2:
        return scored[0][1], "geometry"
    # Symmetric fittings (elbows etc.): either end works, keep the old rule.
    return max(connectors, key=lambda c: _connector_dir(c).DotProduct(wanted_in)), "facing_target"


def _needs_transition(a_mm, b_mm):
    return a_mm is not None and b_mm is not None and abs(a_mm - b_mm) > SIZE_TOLERANCE_MM


def _pipe_diameter_mm(pipe):
    try:
        return round(pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).AsDouble() * MM_PER_FOOT, 1)
    except Exception:
        return None


def _check_tee_clearance(main, point, label, override_mm=None):
    """Refuse a tee that would land on or against a fitting already on the main pipe.

    BreakCurve + NewTeeFitting next to an existing fitting does not fail: Revit joins
    the branch to the neighbouring piece's end instead and the main run comes apart.
    """
    curve = main.Location.Curve
    main_d = _pipe_diameter_mm(main) or 0.0
    need_mm = max(TEE_MIN_CLEARANCE_MM, TEE_CLEARANCE_FACTOR * main_d)
    if override_mm is not None:
        try:
            need_mm = max(MIN_TEE_CLEARANCE_OVERRIDE_MM, float(override_mm))
        except Exception:
            pass
    for k in (0, 1):
        end = curve.GetEndPoint(k)
        distance_mm = end.DistanceTo(point) * MM_PER_FOOT
        if distance_mm >= need_mm:
            continue
        end_c = _nearest_connector(main, end)
        if end_c is None or not end_c.IsConnected:
            continue  # open pipe end: joined with an elbow/union as before
        owners = [i for i in _connected_owner_ids(end_c) if i != get_element_id_text(main.Id)]
        raise ValueError(
            u"{}: branch point is {} mm from fitting {} on pipe {}; a tee needs at least {} mm of "
            u"straight pipe there. Move the branch point further along the main.".format(
                label, round(distance_mm, 1), u", ".join(owners) or u"?",
                get_element_id_text(main.Id), round(need_mm, 1)))


def _overlap_problem(pipe, planned_dir=None):
    """None if the pipe still separates the fittings at its ends, else a short reason.

    Fittings trim the pipes they join. When two fittings need more length than the pipe
    between them has, Revit gives no warning: the pipe is left reversed, near zero length
    or deleted, and the fittings overlap.
    """
    try:
        if pipe is None or not pipe.IsValidObject:
            return u"the pipe was consumed by its fittings"
        curve = pipe.Location.Curve
    except Exception:
        return u"the pipe was consumed by its fittings"
    length_mm = curve.Length * MM_PER_FOOT
    if length_mm < MIN_PIPE_BETWEEN_FITTINGS_MM:
        return u"only {} mm of pipe is left between the fittings".format(round(length_mm, 1))
    if planned_dir is not None:
        actual = curve.GetEndPoint(1).Subtract(curve.GetEndPoint(0)).Normalize()
        if actual.DotProduct(planned_dir) < 0:
            return u"the fittings at its two ends overlap (pipe reversed)"
    return None


def _overlapping_fittings(doc, new_ids):
    """Pairs (new fitting id, other fitting id) whose SOLIDS intersect.

    `new_ids` are element ids (text or ElementId) of fittings just created. Each is tested
    against every pipe fitting whose bounding box is near it, including fittings that were
    already in the model (traps, tees of other runs).
    """
    news = []
    for raw in new_ids:
        try:
            element_id = raw if isinstance(raw, DB.ElementId) else make_element_id(DB, raw)
            element = doc.GetElement(element_id)
        except Exception:
            element = None
        if element is not None and element.IsValidObject:
            news.append(element)
    if not news:
        return []
    margin = _mm_to_ft(OVERLAP_SEARCH_MARGIN_MM)
    pairs = set()
    for element in news:
        box = element.get_BoundingBox(None)
        if box is None:
            continue
        outline = DB.Outline(DB.XYZ(box.Min.X - margin, box.Min.Y - margin, box.Min.Z - margin),
                             DB.XYZ(box.Max.X + margin, box.Max.Y + margin, box.Max.Z + margin))
        near = [i for i in DB.FilteredElementCollector(doc)
                .OfCategory(DB.BuiltInCategory.OST_PipeFitting)
                .WherePasses(DB.BoundingBoxIntersectsFilter(outline))
                .ToElementIds() if i != element.Id]
        if not near:
            continue
        hits = DB.FilteredElementCollector(doc, List[DB.ElementId](near)) \
            .WherePasses(DB.ElementIntersectsElementFilter(element)).ToElementIds()
        for hit in hits:
            a = get_element_id_text(element.Id)
            b = get_element_id_text(hit)
            pairs.add((a, b) if a < b else (b, a))
    return sorted(pairs)


def _describe_overlaps(doc, pairs):
    parts = []
    for a, b in pairs:
        names = []
        for raw in (a, b):
            try:
                names.append(u"{} ({})".format(raw, _name(doc.GetElement(make_element_id(DB, raw)).Symbol.Family)))
            except Exception:
                names.append(raw)
        parts.append(u" x ".join(names))
    return u"; ".join(parts)


def _is_direct_start_fitting(element):
    try:
        if element.Category is None or get_element_id_value(element.Category.Id) != \
                int(DB.BuiltInCategory.OST_PipeFitting):
            return False
        family = _name(element.Symbol.Family)
    except Exception:
        return False
    return any(key in family for key in DIRECT_START_FAMILY_KEYS)


def _elbow_leg_ft(doc, system_type, pipe_type, level, diameter_ft, corner, dir_in, dir_out):
    """Centre-to-end length of the elbow the routing preferences make at this corner.

    Builds two trial pipes meeting at `corner`, lets Revit make the elbow, measures the
    distance from the corner to the elbow end facing the incoming pipe, then rolls back.
    """
    trial_ft = _mm_to_ft(DIRECT_START_TRIAL_MM)
    own = None
    sub = None
    if doc.IsModifiable:
        sub = DB.SubTransaction(doc)
        sub.Start()
    else:
        own = DB.Transaction(doc, "RevitMCP elbow trial")
        own.Start()
    try:
        a_start = corner.Subtract(dir_in.Multiply(trial_ft))
        pipe_a = DB.Plumbing.Pipe.Create(doc, system_type.Id, pipe_type.Id, level.Id, a_start, corner)
        pipe_b = DB.Plumbing.Pipe.Create(doc, system_type.Id, pipe_type.Id, level.Id, corner,
                                         corner.Add(dir_out.Multiply(trial_ft)))
        if diameter_ft:
            _set_param(pipe_a, DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM, diameter_ft)
            _set_param(pipe_b, DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM, diameter_ft)
        doc.Regenerate()
        elbow = doc.Create.NewElbowFitting(_nearest_connector(pipe_a, corner), _nearest_connector(pipe_b, corner))
        doc.Regenerate()
        best = None
        for c in _piping_connectors(elbow):
            dist = c.Origin.DistanceTo(a_start)
            if best is None or dist < best[0]:
                best = (dist, c.Origin.DistanceTo(corner))
        return best[1]
    finally:
        if sub is not None:
            sub.RollBack()
        else:
            own.RollBack()


def _branch_angle_deg(main, branch_connector):
    """Plan-independent angle between the main pipe axis and the branch pipe axis (0..90)."""
    try:
        axis = main.Location.Curve.Direction.Normalize()
        branch = _connector_dir(branch_connector)
        dot = abs(max(-1.0, min(1.0, axis.DotProduct(branch))))
        return math.degrees(math.acos(dot))
    except Exception:
        return 90.0


def _junction_symbol(doc, main, family_name, branch_connector, label):
    """(symbol or None, how). Explicit family wins; otherwise <pipe type>_斜T for angled branches."""
    if _text(family_name):
        symbol = _symbol_by_family(doc, DB.BuiltInCategory.OST_PipeFitting, family_name, None, label)
        return symbol, u"junction_family"
    try:
        vertical = abs(main.Location.Curve.Direction.Normalize().Z) > RISER_AXIS_Z
    except Exception:
        vertical = False
    if vertical:
        # branches into a riser use the system's sanitary tee (順T), whatever the routing preferences say
        tee_name = u"{}_順T".format(_name(main.PipeType))
        for symbol in _symbols_in_category(doc, DB.BuiltInCategory.OST_PipeFitting):
            try:
                if _name(symbol.Family) == tee_name:
                    return symbol, u"riser_sanitary_tee"
            except Exception:
                pass
        raise ValueError(u"{}: branches into a riser need the family '{}' (or pass junction_family).".format(
            label, tee_name))
    angle = _branch_angle_deg(main, branch_connector)
    if abs(angle - 90.0) <= SQUARE_BRANCH_TOLERANCE_DEG:
        return None, u"routing_preferences"
    wye_name = u"{}_斜T".format(_name(main.PipeType))
    for symbol in _symbols_in_category(doc, DB.BuiltInCategory.OST_PipeFitting):
        try:
            if _name(symbol.Family) == wye_name:
                return symbol, u"auto_wye ({} deg branch)".format(round(angle, 1))
        except Exception:
            pass
    raise ValueError(u"{}: the branch meets the main at {} deg; that needs a wye family named '{}' "
                     u"(or pass junction_family).".format(label, round(angle, 1), wye_name))


def _new_tee_with(doc, main, symbol, c_a, c_b, branch_c):
    """NewTeeFitting, temporarily making `symbol` the first junction rule of the main's pipe type."""
    if symbol is None:
        return doc.Create.NewTeeFitting(c_a, c_b, branch_c)
    _ensure_symbol_active(doc, symbol)
    manager = main.PipeType.RoutingPreferenceManager
    group = DB.RoutingPreferenceRuleGroupType.Junctions
    old_type = manager.PreferredJunctionType
    manager.AddRule(group, DB.RoutingPreferenceRule(symbol.Id, u"RevitMCP temporary junction"), 0)
    try:
        manager.PreferredJunctionType = DB.PreferredJunctionType.Tee
        return doc.Create.NewTeeFitting(c_a, c_b, branch_c)
    finally:
        try:
            manager.RemoveRule(group, 0)
        except Exception:
            pass
        try:
            manager.PreferredJunctionType = old_type
        except Exception:
            pass


def _chamfer_corners(points, chamfer_ft, label):
    """Cut every corner sharper than BEND_SPLIT_ABOVE_DEG into two half bends (90 -> 45 + 45)."""
    if len(points) < 3:
        return points, 0
    out = [points[0]]
    count = 0
    last = len(points) - 1
    for i in range(1, last):
        a, p, b = points[i - 1], points[i], points[i + 1]
        d1 = p.Subtract(a)
        d2 = b.Subtract(p)
        len1 = d1.GetLength()
        len2 = d2.GetLength()
        d1 = d1.Normalize()
        d2 = d2.Normalize()
        turn = math.degrees(math.acos(max(-1.0, min(1.0, d1.DotProduct(d2)))))
        if turn <= BEND_SPLIT_ABOVE_DEG:
            out.append(p)
            continue
        # a segment shared by two chamfered corners may give at most ~half of itself to each
        share1 = 0.45 if i - 1 > 0 else 0.8
        share2 = 0.45 if i + 1 < last else 0.8
        cut = min(chamfer_ft, len1 * share1, len2 * share2)
        if cut * MM_PER_FOOT < MIN_CHAMFER_MM:
            raise ValueError(u"{}: corner #{} has segments too short for two 45-degree bends "
                             u"({} / {} mm). Lengthen them.".format(
                                 label, i, round(len1 * MM_PER_FOOT, 1), round(len2 * MM_PER_FOOT, 1)))
        out.append(p.Subtract(d1.Multiply(cut)))
        out.append(p.Add(d2.Multiply(cut)))
        count += 1
    out.append(points[-1])
    return out, count


def _run_connector_pair(connectors):
    """(run_a, run_b, branch) of a 3-connector junction: the run pair points in opposite directions."""
    best = None
    for i in range(len(connectors)):
        for j in range(i + 1, len(connectors)):
            dot = _connector_dir(connectors[i]).DotProduct(_connector_dir(connectors[j]))
            if best is None or dot < best[0]:
                best = (dot, i, j)
    _, i, j = best
    branch = [c for k, c in enumerate(connectors) if k not in (i, j)][0]
    return connectors[i], connectors[j], branch


def _closest_on_line(origin, direction, other_origin, other_direction):
    """Point on line (origin, direction) closest to line (other_origin, other_direction)."""
    w0 = origin.Subtract(other_origin)
    a = direction.DotProduct(direction)
    b = direction.DotProduct(other_direction)
    c = other_direction.DotProduct(other_direction)
    d = direction.DotProduct(w0)
    e = other_direction.DotProduct(w0)
    denom = a * c - b * b
    if abs(denom) < 1e-12:
        return origin
    s = (b * e - c * d) / denom
    return origin.Add(direction.Multiply(s))


def _place_wye(doc, main, symbol, joint, branch_dir, label):
    """Place `symbol` on `main` at `joint` for a branch arriving along `branch_dir`.

    Returns (instance, run connector on the branch side, other run connector, branch connector).
    The wye's branch leans toward one run end; that end is turned to face where the branch
    comes from, so the branch joins with the flow.
    """
    _ensure_symbol_active(doc, symbol)
    axis = main.Location.Curve.Direction.Normalize()
    toward_branch = branch_dir.Negate()
    side = axis if toward_branch.DotProduct(axis) > 0 else axis.Negate()
    instance = doc.Create.NewFamilyInstance(joint, symbol, DB.Structure.StructuralType.NonStructural)
    doc.Regenerate()

    main_d = None
    try:
        main_d = main.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).AsDouble()
    except Exception:
        pass
    connectors = _piping_connectors(instance)
    if len(connectors) != 3:
        raise ValueError(u"{}: junction family has {} connectors (need 3).".format(label, len(connectors)))
    run_a, run_b, branch = _run_connector_pair(connectors)
    if main_d:
        for run in (run_a, run_b):  # size by a RUN connector (sizing by the branch made 50/80 swaps)
            try:
                run.Radius = main_d / 2.0
                break
            except Exception:
                continue
        doc.Regenerate()

    ids = {}
    run_a, run_b, branch = _run_connector_pair(_piping_connectors(instance))
    lean = run_a if _connector_dir(branch).DotProduct(_connector_dir(run_a)) > 0 else run_b
    ids["lean"], ids["branch"] = lean.Id, branch.Id

    # 1) the leaning run end faces the side the branch comes from
    current = _connector_dir(lean)
    dot = max(-1.0, min(1.0, current.DotProduct(side)))
    if dot < 0.99999:
        rot_axis = current.CrossProduct(side)
        if rot_axis.GetLength() < 1e-9:
            rot_axis = _any_perpendicular(current)
        _rotate(doc, instance.Id, lean.Origin, rot_axis.Normalize(), math.acos(dot))
        doc.Regenerate()
    # 2) spin about the run axis so the branch points back along the branch pipe
    lean = _connector_by_id(instance, ids["lean"])
    branch = _connector_by_id(instance, ids["branch"])
    have = _connector_dir(branch)
    have = have.Subtract(side.Multiply(have.DotProduct(side)))
    want = toward_branch.Subtract(side.Multiply(toward_branch.DotProduct(side)))
    if have.GetLength() > 1e-6 and want.GetLength() > 1e-6:
        have = have.Normalize()
        want = want.Normalize()
        angle = math.atan2(side.DotProduct(have.CrossProduct(want)), have.DotProduct(want))
        if abs(angle) > 1e-9:
            _rotate(doc, instance.Id, lean.Origin, side, angle)
            doc.Regenerate()
    # 3) move so the wye's virtual intersection sits on the joint (run line = main line)
    lean = _connector_by_id(instance, ids["lean"])
    branch = _connector_by_id(instance, ids["branch"])
    virtual = _closest_on_line(lean.Origin, side, branch.Origin, _connector_dir(branch))
    delta = joint.Subtract(virtual)
    if delta.GetLength() > TINY_FT:
        DB.ElementTransformUtils.MoveElement(doc, instance.Id, delta)
        doc.Regenerate()
    lean = _connector_by_id(instance, ids["lean"])
    branch = _connector_by_id(instance, ids["branch"])
    other = [c for c in _piping_connectors(instance) if c.Id not in (ids["lean"], ids["branch"])][0]
    return instance, lean, other, branch


def _splice_into_main(doc, main, joint, lean, other, label):
    """Cut `main` at `joint`, trim both pieces onto the wye's run connectors and connect them."""
    new_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, main.Id, joint)
    doc.Regenerate()
    piece = doc.GetElement(new_id)
    side = _connector_dir(lean)
    for pipe in (main, piece):
        curve = pipe.Location.Curve
        p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
        far, near_index = (p0, 1) if p0.DistanceTo(joint) > p1.DistanceTo(joint) else (p1, 0)
        target = lean if far.Subtract(joint).DotProduct(side) > 0 else other
        if far.DistanceTo(target.Origin) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
            raise ValueError(u"{}: the wye does not fit on main pipe {}; move the branch point.".format(
                label, get_element_id_text(pipe.Id)))
        if near_index == 1:
            pipe.Location.Curve = DB.Line.CreateBound(far, target.Origin)
        else:
            pipe.Location.Curve = DB.Line.CreateBound(target.Origin, far)
        doc.Regenerate()
        _nearest_connector(pipe, target.Origin).ConnectTo(target)
    return piece


def _with_transition_stub(points, at_start, label, stub_mm=None):
    """Insert a short collinear point so the end segment can carry the other size."""
    if at_start:
        a, b = points[0], points[1]
    else:
        a, b = points[-1], points[-2]
    if stub_mm is None:
        stub_mm = TRANSITION_STUB_MM
    seg_mm = a.DistanceTo(b) * MM_PER_FOOT
    need_mm = stub_mm + TRANSITION_ROOM_MM
    if seg_mm < need_mm:
        raise ValueError(
            u"{}: the {} segment is {} mm; changing size there needs at least {} mm of straight pipe "
            u"(stub + transition). Lengthen that segment.".format(
                label, u"first" if at_start else u"last", round(seg_mm, 1), need_mm))
    stub = a.Add(b.Subtract(a).Normalize().Multiply(_mm_to_ft(stub_mm)))
    if at_start:
        return [points[0], stub] + list(points[1:])
    return list(points[:-1]) + [stub, points[-1]]


def _vector_from(raw, label):
    if not isinstance(raw, dict):
        raise ValueError(u"{} must be {{x, y, z?}}.".format(label))
    x = _to_float(raw.get("x"), label + u".x", 0.0)
    y = _to_float(raw.get("y"), label + u".y", 0.0)
    z = _to_float(raw.get("z"), label + u".z", 0.0)
    vector = DB.XYZ(x, y, z)
    if vector.GetLength() < 1e-9:
        raise ValueError(u"{} must not be a zero vector.".format(label))
    return vector.Normalize()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_routes(api):

    # ---------------------------------------------------- linked elements
    @api.route('/mep/linked_elements', methods=['POST'])
    def handle_linked_elements(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload = request.data if hasattr(request, "data") else {}
            if not isinstance(payload, dict):
                payload = {}
            name_filter = _text(payload.get("link_name_contains")).lower()
            raw_categories = payload.get("categories") or list(_DEFAULT_LINK_CATEGORIES)
            if not isinstance(raw_categories, (list, tuple)):
                raw_categories = [raw_categories]
            categories = [_link_category(c) for c in raw_categories]
            try:
                limit = max(1, min(3000, int(payload.get("limit") or 500)))
            except Exception:
                limit = 500

            host_level = None
            z_bottom = z_top = None
            if _text(payload.get("host_level_name")):
                host_level = _find_level(doc, payload.get("host_level_name"), "host_level_name")
                levels = _all_levels(doc)
                z_bottom = host_level.Elevation
                above = [lv for lv in levels if lv.Elevation > host_level.Elevation + TINY_FT]
                z_top = above[0].Elevation if above else host_level.Elevation + _mm_to_ft(4000.0)
            level_ft = host_level.Elevation if host_level is not None else None
            tol = _mm_to_ft(1.0)

            links_out = []
            elements_out = []
            truncated = False
            for link in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements():
                link_name = _name(link)
                if name_filter and name_filter not in link_name.lower():
                    continue
                link_doc = link.GetLinkDocument()
                if link_doc is None:
                    links_out.append({"link_instance_id": get_element_id_text(link.Id), "name": link_name,
                                      "loaded": False})
                    continue
                transform = link.GetTotalTransform()
                links_out.append({
                    "link_instance_id": get_element_id_text(link.Id),
                    "name": link_name,
                    "loaded": True,
                    "link_document": _text(link_doc.Title),
                    "origin_mm": _xyz_mm(transform.Origin),
                    "rotation_deg": round(math.degrees(math.atan2(transform.BasisX.Y, transform.BasisX.X)), 3),
                })
                for key, bic in categories:
                    collector = (DB.FilteredElementCollector(link_doc)
                                 .OfCategory(bic).WhereElementIsNotElementType())
                    for element in collector:
                        if len(elements_out) >= limit:
                            truncated = True
                            break
                        if z_bottom is not None:
                            box = _transformed_box(element, transform)
                            if box is None:
                                continue
                            if box[1].Z < z_bottom - tol or box[0].Z > z_top - tol:
                                continue
                        try:
                            data = _describe_linked(link_doc, element, key, transform, level_ft)
                        except Exception as describe_error:
                            data = {"element_id": get_element_id_text(element.Id), "category": key,
                                    "error": _text(describe_error)}
                        data["link_instance_id"] = get_element_id_text(link.Id)
                        elements_out.append(data)

            if not links_out:
                return _error(u"No Revit links found{}.".format(
                    u" matching '{}'".format(name_filter) if name_filter else u""))
            return sanitize_for_json({
                "status": "success",
                "route_version": MEP_ROUTES_VERSION,
                "units": "mm, host project internal coordinates; z_mm is relative to host_level_name when given",
                "host_level": _name(host_level) if host_level is not None else None,
                "links": links_out,
                "count": len(elements_out),
                "truncated": truncated,
                "elements": elements_out,
            })
        except Exception as e:
            route_logger.error("Error in /mep/linked_elements: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ---------------------------------------------------------- connectors
    @api.route('/mep/connectors', methods=['POST'])
    def handle_connectors(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload = request.data if hasattr(request, "data") else {}
            if not isinstance(payload, dict):
                payload = {}
            raw_ids = payload.get("element_ids") or []
            if not isinstance(raw_ids, (list, tuple)):
                raw_ids = [raw_ids]
            if not raw_ids:
                raise ValueError(u"'element_ids' is required.")
            out = []
            for raw_id in raw_ids[:200]:
                try:
                    element = _element_by_id(doc, raw_id, u"element")
                except Exception as lookup_error:
                    out.append({"element_id": _text(raw_id), "error": _text(lookup_error)})
                    continue
                level = _element_level(doc, element)
                level_ft = level.Elevation if level is not None else None
                entry = {
                    "element_id": get_element_id_text(element.Id),
                    "category": _text(element.Category.Name) if element.Category else u"",
                    "level": _name(level) if level is not None else None,
                    "connectors": [_connector_info(c, level_ft) for c in _piping_connectors(element)],
                }
                try:
                    entry["type"] = _symbol_label(element.Symbol)
                except Exception:
                    try:
                        entry["type"] = _name(doc.GetElement(element.GetTypeId()))
                    except Exception:
                        pass
                try:
                    system = element.MEPSystem
                    if system is not None:
                        entry["system"] = _name(system)
                except Exception:
                    pass
                out.append(entry)
            return sanitize_for_json({
                "status": "success",
                "route_version": MEP_ROUTES_VERSION,
                "units": "mm; z_mm is relative to the element's level, z_mm_abs to the project origin. "
                         "direction points OUT of the element.",
                "elements": out,
            })
        except Exception as e:
            route_logger.error("Error in /mep/connectors: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ------------------------------------------------- place pipe fittings
    @api.route('/mep/place_pipe_fittings', methods=['POST'])
    def handle_place_pipe_fittings(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "fittings")

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"fittings[{}] must be an object.".format(index))
                label = u"fittings[{}]".format(index)
                if item.get("move_element_id") is not None:
                    # utility mode: move an existing element by delta_mm {x, y, z}
                    moved = _element_by_id(doc, item.get("move_element_id"), label)
                    delta_spec = item.get("delta_mm") or {}
                    dx = _to_float(delta_spec.get("x"), "delta_mm.x") or 0.0
                    dy = _to_float(delta_spec.get("y"), "delta_mm.y") or 0.0
                    dz = _to_float(delta_spec.get("z"), "delta_mm.z") or 0.0
                    if not is_dry_run:
                        DB.ElementTransformUtils.MoveElement(
                            doc, moved.Id, DB.XYZ(_mm_to_ft(dx), _mm_to_ft(dy), _mm_to_ft(dz)))
                    return {"index": index, "element_id": get_element_id_text(moved.Id),
                            "moved": not is_dry_run, "delta_mm": {"x": dx, "y": dy, "z": dz}}
                symbol = _symbol_by_family(
                    doc, DB.BuiltInCategory.OST_PipeFitting,
                    _merged(payload, item, "family_name"), _merged(payload, item, "type_name"), label)
                target_spec = item.get("connect_to")
                if not isinstance(target_spec, dict):
                    raise ValueError(u"{}: 'connect_to' {{element_id, connector_id?}} is required.".format(label))
                target_element = _element_by_id(doc, target_spec.get("element_id"), label)
                target = _pick_connector(target_element, label, target_spec.get("connector_id"))
                target_dir = _connector_dir(target)
                if target_dir is None:
                    raise ValueError(u"{}: target connector has no direction.".format(label))
                diameter_mm = _to_float(_merged(payload, item, "diameter_mm"), "diameter_mm")
                if diameter_mm is None:
                    diameter_mm = _connector_diameter_mm(target)
                outlet_dir = None
                if item.get("outlet_direction") is not None or payload.get("outlet_direction") is not None:
                    outlet_dir = _vector_from(_merged(payload, item, "outlet_direction"), label + u".outlet_direction")

                summary = {
                    "index": index,
                    "type": _symbol_label(symbol),
                    "target_element_id": get_element_id_text(target_element.Id),
                    "target_connector_id": target.Id,
                    "at": _xyz_mm(target.Origin),
                    "diameter_mm": diameter_mm,
                }
                if is_dry_run:
                    return summary

                _ensure_symbol_active(doc, symbol)
                instance = doc.Create.NewFamilyInstance(
                    target.Origin, symbol, DB.Structure.StructuralType.NonStructural)
                doc.Regenerate()
                notes = []
                if diameter_mm:
                    how = _set_fitting_size(instance, _mm_to_ft(diameter_mm))
                    if how is None:
                        notes.append(u"Could not set the fitting size; it keeps the family default.")
                    else:
                        summary["sized_by"] = how
                    doc.Regenerate()

                connectors = _piping_connectors(instance)
                if len(connectors) < 2:
                    raise ValueError(u"{}: fitting family has {} piping connectors (need 2+).".format(
                        label, len(connectors)))
                wanted_in = target_dir.Negate()
                inlet_id = item.get("inlet_connector_id")
                if inlet_id is not None and _text(inlet_id) != u"":
                    inlet = _connector_by_id(instance, int(inlet_id))
                    if inlet is None:
                        raise ValueError(u"{}: inlet_connector_id {} not on the fitting (has {}).".format(
                            label, inlet_id, u", ".join(_text(c.Id) for c in connectors)))
                    summary["inlet_selected_by"] = "inlet_connector_id"
                else:
                    inlet, how = _guess_fitting_inlet(connectors, wanted_in)
                    summary["inlet_selected_by"] = how
                inlet_cid = inlet.Id

                # A fitting family that cannot reach the target size (e.g. a trap whose smallest
                # size is 50) would be joined to a different-size connector. Report it.
                inlet_d = _connector_diameter_mm(inlet)
                target_d = _connector_diameter_mm(target)
                if _needs_transition(inlet_d, target_d):
                    summary["size_mismatch"] = {"fitting_inlet_mm": inlet_d, "target_mm": target_d}
                    notes.append(u"Fitting inlet is {} mm but the target is {} mm (the family has no {} mm size). "
                                 u"Run the pipe to this fitting at {} mm instead; create_pipes adds the "
                                 u"transition at the fixture.".format(inlet_d, target_d, target_d, inlet_d))

                # 1) turn the inlet to face the target connector
                current = _connector_dir(inlet)
                dot = max(-1.0, min(1.0, current.DotProduct(wanted_in)))
                if dot < 0.99999:
                    axis = current.CrossProduct(wanted_in)
                    if axis.GetLength() < 1e-9:
                        axis = _any_perpendicular(current)
                    _rotate(doc, instance.Id, inlet.Origin, axis, math.acos(dot))
                    doc.Regenerate()

                # 2) spin about the connection axis so the outlet points where asked
                if outlet_dir is not None:
                    inlet = _connector_by_id(instance, inlet_cid)
                    outlets = [c for c in _piping_connectors(instance) if c.Id != inlet_cid]
                    axis = wanted_in
                    spin_id = item.get("spin_connector_id")
                    if spin_id is not None and _text(spin_id) != u"":
                        spin_c = _connector_by_id(instance, int(spin_id))
                        if spin_c is None:
                            raise ValueError(u"{}: spin_connector_id {} not on the fitting.".format(label, spin_id))
                    else:
                        # the connector leaning furthest off the axis (a wye's branch, a trap's outlet)
                        def off_axis(c):
                            v = _connector_dir(c)
                            return v.Subtract(axis.Multiply(v.DotProduct(axis))).GetLength()
                        spin_c = max(outlets, key=off_axis)
                    summary["spun_connector_id"] = spin_c.Id
                    out_vec = _connector_dir(spin_c)
                    a = out_vec.Subtract(axis.Multiply(out_vec.DotProduct(axis)))
                    b = outlet_dir.Subtract(axis.Multiply(outlet_dir.DotProduct(axis)))
                    if a.GetLength() > 1e-6 and b.GetLength() > 1e-6:
                        a = a.Normalize()
                        b = b.Normalize()
                        angle = math.atan2(axis.DotProduct(a.CrossProduct(b)), a.DotProduct(b))
                        _rotate(doc, instance.Id, inlet.Origin, axis, angle)
                        doc.Regenerate()
                    else:
                        notes.append(u"outlet_direction is parallel to the connection axis; not applied.")

                # 3) move the inlet onto the target and connect
                inlet = _connector_by_id(instance, inlet_cid)
                delta = target.Origin.Subtract(inlet.Origin)
                if delta.GetLength() > TINY_FT:
                    DB.ElementTransformUtils.MoveElement(doc, instance.Id, delta)
                    doc.Regenerate()
                inlet = _connector_by_id(instance, inlet_cid)
                try:
                    inlet.ConnectTo(target)
                    summary["connected"] = True
                except Exception as connect_error:
                    summary["connected"] = False
                    notes.append(u"Placed but not connected: {}".format(_text(connect_error)))

                doc.Regenerate()
                clash = _overlapping_fittings(doc, [instance.Id])
                if clash:
                    raise ValueError(u"{}: the fitting would overlap other fittings, nothing was placed: {}.".format(
                        label, _describe_overlaps(doc, clash)))

                level = _element_level(doc, target_element)
                level_ft = level.Elevation if level is not None else None
                summary["element_id"] = get_element_id_text(instance.Id)
                summary["inlet"] = _connector_info(inlet, level_ft)
                summary["outlets"] = [_connector_info(c, level_ft)
                                      for c in _piping_connectors(instance) if c.Id != inlet_cid]
                if notes:
                    summary["notes"] = notes
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Place Pipe Fittings", items, create_one, dry_run)
            return _build_response("pipe fittings", created, failed, warnings, outcome, dry_run,
                                   extra={"route_version": MEP_ROUTES_VERSION})
        except Exception as e:
            route_logger.error("Error in /mep/place_pipe_fittings: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ------------------------------------------- pipes (slope/connect/tee)
    @api.route('/mep/create_pipes', methods=['POST'])
    def handle_create_pipes(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "pipes")
            connect_default = _coerce_bool(payload.get("connect_fittings"), default=True)
            run_pipes = {}     # index -> list of live Pipe elements (real run)
            run_points = {}    # index -> planned points (dry run)
            split_pieces = {}  # original pipe id text -> extra pieces created by tees in this call

            def tee_candidates(spec, label):
                """[(line, pipe_or_None, run_index_or_None)] the branch may tee into."""
                out = []
                if spec.get("pipe_id") is not None:
                    pipe = _element_by_id(doc, spec.get("pipe_id"), label + u".end_connect")
                    if not _is_pipe(pipe):
                        raise ValueError(u"{}: end_connect.pipe_id {} is not a pipe.".format(label, spec.get("pipe_id")))
                    out.append((pipe.Location.Curve, pipe, None))
                    for piece in split_pieces.get(get_element_id_text(pipe.Id), []):
                        out.append((piece.Location.Curve, piece, None))
                elif spec.get("run_index") is not None:
                    k = int(spec.get("run_index"))
                    if k in run_pipes:
                        for pipe in run_pipes[k]:
                            out.append((pipe.Location.Curve, pipe, k))
                    elif k in run_points:
                        pts = run_points[k]
                        for i in range(len(pts) - 1):
                            out.append((DB.Line.CreateBound(pts[i], pts[i + 1]), None, k))
                    else:
                        raise ValueError(u"{}: run_index {} was not created earlier in this call.".format(label, k))
                return out

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"pipes[{}] must be an object.".format(index))
                label = u"pipes[{}]".format(index)
                level = _find_level(doc, _merged(payload, item, "level_name"))
                offset_mm = _to_float(_merged(payload, item, "offset_mm"), "offset_mm", 0.0)
                diameter_mm = _to_float(_merged(payload, item, "diameter_mm"), "diameter_mm")
                slope_pct = _to_float(_merged(payload, item, "slope_percent"), "slope_percent")
                pipe_type = _find_system_type(doc, DB.Plumbing.PipeType, _merged(payload, item, "type_name"), u"pipe")
                system_type = _find_system_type(doc, DB.Plumbing.PipingSystemType,
                                                _merged(payload, item, "system_type_name"), u"piping system")
                classification = _text(system_type.SystemClassification)
                connect = _coerce_bool(item.get("connect_fittings"), default=connect_default)
                notes = []

                raw_points = item.get("points")
                if raw_points is None:
                    raw_points = [p for p in (item.get("start"), item.get("end")) if p is not None]
                if not isinstance(raw_points, (list, tuple)):
                    raise ValueError(u"{}: 'points' must be a list.".format(label))

                # ---- start connector
                start_c = None
                start_spec = item.get("start_connect")
                if start_spec is not None:
                    if not isinstance(start_spec, dict):
                        raise ValueError(u"{}: start_connect must be {{element_id, connector_id?}}.".format(label))
                    start_el = _element_by_id(doc, start_spec.get("element_id"), label + u".start_connect")
                    hint = None
                    if raw_points:
                        try:
                            hint = _point_from(raw_points[0], label, level.Elevation)
                        except Exception:
                            hint = None
                    start_c = _pick_connector(start_el, label + u".start_connect",
                                              start_spec.get("connector_id"), hint, classification)

                # ---- points (slope fills in missing z)
                points = []
                explicit_flags = []
                prev = None
                if start_c is not None:
                    prev = start_c.Origin
                    points.append(prev)
                    explicit_flags.append(True)
                for i, raw in enumerate(raw_points):
                    plabel = u"{}.points[{}]".format(label, i)
                    xy = _point_from(raw, plabel, 0.0)
                    explicit_z = None
                    if isinstance(raw, dict):
                        explicit_z = raw.get("z_mm")
                    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
                        explicit_z = raw[2]
                    if explicit_z is not None and _text(explicit_z) != u"":
                        z = level.Elevation + _mm_to_ft(_to_float(explicit_z, plabel + u".z_mm"))
                    elif slope_pct is not None and prev is not None:
                        horizontal = math.hypot(xy.X - prev.X, xy.Y - prev.Y)
                        z = prev.Z - horizontal * slope_pct / 100.0
                    else:
                        z = level.Elevation + _mm_to_ft(offset_mm)
                    point = DB.XYZ(xy.X, xy.Y, z)
                    points.append(point)
                    explicit_flags.append(explicit_z is not None and _text(explicit_z) != u"")
                    prev = point
                if len(points) < 2:
                    raise ValueError(u"{} needs at least 2 points (start_connect counts as one).".format(label))

                # ---- direct start on a 單口彎頭 outlet: first elbow sits on the fitting, no pipe
                direct = None
                if start_c is not None:
                    wanted_direct = item.get("direct_start")
                    if wanted_direct is None:
                        wanted_direct = _is_direct_start_fitting(start_c.Owner)
                    if _coerce_bool(wanted_direct, default=False):
                        if len(points) < 3:
                            raise ValueError(u"{}: direct_start needs the corner point and at least one more "
                                             u"point after it.".format(label))
                        dir0 = _connector_dir(start_c)
                        seg0 = points[1].Subtract(points[0]).Normalize()
                        if dir0 is None or dir0.DotProduct(seg0) < 0.99:
                            raise ValueError(u"{}: direct_start: the first point must lie along the start "
                                             u"connector's direction {}.".format(label, _dir_dict(dir0)))
                        dir1 = points[2].Subtract(points[1]).Normalize()
                        turn = math.degrees(math.acos(max(-1.0, min(1.0, dir0.DotProduct(dir1)))))
                        if turn > BEND_SPLIT_ABOVE_DEG:
                            raise ValueError(u"{}: direct_start corner turns {} deg; only one 45-deg elbow can sit "
                                             u"on the fitting.".format(label, round(turn, 1)))
                        start_dia_mm = _connector_diameter_mm(start_c)
                        if _needs_transition(start_dia_mm, diameter_mm or start_dia_mm):
                            raise ValueError(u"{}: direct_start needs the same size as the fitting ({} mm).".format(
                                label, start_dia_mm))
                        leg = _elbow_leg_ft(doc, system_type, pipe_type, level,
                                            _mm_to_ft(diameter_mm or start_dia_mm), points[1], dir0, dir1)
                        points[1] = points[0].Add(dir0.Multiply(leg))
                        for k in range(2, len(points)):
                            if explicit_flags[k]:
                                continue
                            if slope_pct is not None:
                                horizontal = math.hypot(points[k].X - points[k - 1].X, points[k].Y - points[k - 1].Y)
                                points[k] = DB.XYZ(points[k].X, points[k].Y,
                                                   points[k - 1].Z - horizontal * slope_pct / 100.0)
                        direct = {"fitting": start_c.Owner, "connector": start_c, "leg_ft": leg, "dir": dir0}
                        notes.append(u"direct_start: the first elbow sits on {} with no pipe between "
                                     u"(elbow leg {} mm).".format(get_element_id_text(start_c.Owner.Id), _ft_to_mm(leg)))

                joint_families = item.get("joint_families") or {}
                joint_swaps = []   # (xyz, symbol)
                if joint_families:
                    if not isinstance(joint_families, dict):
                        raise ValueError(u"{}: joint_families must be {{point_index: family_name}}.".format(label))
                    offset = 1 if start_c is not None else 0
                    for raw_key, family in joint_families.items():
                        k = int(raw_key) + offset
                        if k <= 0 or k >= len(points) - 1:
                            raise ValueError(u"{}: joint_families key {} is not an inner corner.".format(label, raw_key))
                        joint_swaps.append((points[k], _symbol_by_family(
                            doc, DB.BuiltInCategory.OST_PipeFitting, family, None, label + u".joint_families")))

                # ---- end: tee into a pipe, or connect to a connector
                end_mode = None
                end_target = None
                end_spec = item.get("end_connect")
                if end_spec is not None:
                    if not isinstance(end_spec, dict):
                        raise ValueError(u"{}: end_connect must be an object.".format(label))
                    snap_ft = _mm_to_ft(_to_float(end_spec.get("snap_tolerance_mm"), "snap_tolerance_mm",
                                                  DEFAULT_END_SNAP_MM))
                    if end_spec.get("pipe_id") is not None or end_spec.get("run_index") is not None:
                        best = None
                        for line, pipe, run_k in tee_candidates(end_spec, label):
                            projected = line.Project(points[-1])
                            if projected is None:
                                continue
                            if best is None or projected.Distance < best[0]:
                                best = (projected.Distance, projected.XYZPoint, pipe, run_k, line)
                        if best is None or best[0] > snap_ft:
                            raise ValueError(u"{}: last point is {} mm from the target pipe (snap {} mm).".format(
                                label, _ft_to_mm(best[0]) if best else u"?", _ft_to_mm(snap_ft)))
                        if best[2] is not None:
                            _check_tee_clearance(best[2], best[1], label, _merged(payload, item, "tee_clearance_mm"))
                        if best[0] * MM_PER_FOOT > 1.0:
                            notes.append(u"Last point moved {} mm onto the target pipe centreline.".format(
                                _ft_to_mm(best[0])))
                        points[-1] = best[1]
                        end_mode = "tee"
                        end_target = best
                    elif end_spec.get("element_id") is not None:
                        end_el = _element_by_id(doc, end_spec.get("element_id"), label + u".end_connect")
                        end_c = _pick_connector(end_el, label + u".end_connect", end_spec.get("connector_id"),
                                                points[-1], classification)
                        distance = end_c.Origin.DistanceTo(points[-1])
                        if distance > snap_ft:
                            raise ValueError(u"{}: last point is {} mm from the target connector (snap {} mm).".format(
                                label, _ft_to_mm(distance), _ft_to_mm(snap_ft)))
                        points[-1] = end_c.Origin
                        end_mode = "connect"
                        end_target = end_c
                    else:
                        raise ValueError(u"{}: end_connect needs pipe_id, run_index or element_id.".format(label))

                for i in range(len(points) - 1):
                    if points[i].DistanceTo(points[i + 1]) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                        raise ValueError(u"{} has two consecutive identical points (#{}).".format(label, i))
                points = _drop_collinear(points)

                if diameter_mm is None and start_c is not None:
                    diameter_mm = _connector_diameter_mm(start_c)
                    if diameter_mm:
                        notes.append(u"diameter_mm taken from the start connector.")

                # ---- drainage: no square bends, every corner becomes two 45-degree bends
                bends_45 = _coerce_bool(_merged(payload, item, "bends_45"),
                                        default=(classification == u"Sanitary"))
                bend_splits = 0
                if bends_45:
                    chamfer_mm = _to_float(_merged(payload, item, "chamfer_mm"), "chamfer_mm",
                                           max(100.0, 2.0 * (diameter_mm or 50.0)))
                    points, bend_splits = _chamfer_corners(points, _mm_to_ft(chamfer_mm), label)

                # ---- size changes at the connected ends: stub at the connector size + transition
                transitions = []
                start_dia = _connector_diameter_mm(start_c) if start_c is not None else None
                end_dia = _connector_diameter_mm(end_target) if end_mode == "connect" else None
                start_stub = _needs_transition(start_dia, diameter_mm)
                end_stub = _needs_transition(end_dia, diameter_mm)
                stub_mm = _to_float(_merged(payload, item, "transition_stub_mm"), "transition_stub_mm",
                                    TRANSITION_STUB_MM)
                if start_stub:
                    points = _with_transition_stub(points, True, label, stub_mm)
                    transitions.append({"at": "start", "from_mm": start_dia, "to_mm": diameter_mm})
                if end_stub:
                    points = _with_transition_stub(points, False, label, stub_mm)
                    transitions.append({"at": "end", "from_mm": diameter_mm, "to_mm": end_dia})
                seg_dia = [diameter_mm] * (len(points) - 1)
                transition_joints = set()
                if start_stub:
                    seg_dia[0] = start_dia
                    transition_joints.add(0)
                if end_stub:
                    seg_dia[-1] = end_dia
                    transition_joints.add(len(points) - 3)

                total_ft = 0.0
                for seg_i in range(len(points) - 1):
                    total_ft += points[seg_i].DistanceTo(points[seg_i + 1])
                summary = {
                    "index": index,
                    "pipe_type": _name(pipe_type),
                    "system_type": _name(system_type),
                    "level": _name(level),
                    "segment_count": len(points) - 1,
                    "total_length_mm": _ft_to_mm(total_ft),
                    "points": [_xyz_mm(p, level.Elevation) for p in points],
                }
                if diameter_mm is not None:
                    summary["diameter_mm"] = diameter_mm
                if slope_pct is not None:
                    summary["slope_percent"] = slope_pct
                if start_c is not None:
                    summary["start_connect"] = {"element_id": get_element_id_text(start_c.Owner.Id),
                                                "connector_id": start_c.Id}
                if end_mode:
                    summary["end_mode"] = end_mode
                if transitions:
                    summary["transitions"] = transitions
                if bend_splits:
                    summary["corners_split_into_45"] = bend_splits
                if direct is not None:
                    summary["direct_start"] = {"fitting_id": get_element_id_text(direct["fitting"].Id),
                                               "elbow_leg_mm": _ft_to_mm(direct["leg_ft"])}
                if is_dry_run:
                    run_points[index] = points
                    if notes:
                        summary["notes"] = notes
                    return summary

                wye = None
                if end_mode == "tee" and end_target[2] is not None:
                    main_for_wye = end_target[2]
                    branch_dir = points[-1].Subtract(points[-2]).Normalize()
                    try:
                        main_axis = main_for_wye.Location.Curve.Direction.Normalize()
                        branch_angle = math.degrees(math.acos(min(1.0, abs(main_axis.DotProduct(branch_dir)))))
                    except Exception:
                        branch_angle = 90.0
                    explicit = _merged(payload, item, "junction_family")
                    if _text(explicit) or abs(branch_angle - 90.0) > SQUARE_BRANCH_TOLERANCE_DEG:
                        _check_tee_clearance(main_for_wye, points[-1], label, _merged(payload, item, "tee_clearance_mm"))
                        if _text(explicit):
                            wye_symbol = _symbol_by_family(doc, DB.BuiltInCategory.OST_PipeFitting, explicit, None, label)
                        else:
                            wye_symbol = None
                            wanted = u"{}_斜T".format(_name(main_for_wye.PipeType))
                            for symbol in _symbols_in_category(doc, DB.BuiltInCategory.OST_PipeFitting):
                                try:
                                    if _name(symbol.Family) == wanted:
                                        wye_symbol = symbol
                                        break
                                except Exception:
                                    pass
                            if wye_symbol is None:
                                raise ValueError(u"{}: branch meets the main at {} deg; family '{}' is needed "
                                                 u"(or pass junction_family).".format(label, round(branch_angle, 1), wanted))
                        wye_inst, w_lean, w_other, w_branch = _place_wye(
                            doc, main_for_wye, wye_symbol, points[-1], branch_dir, label)
                        joint_point = points[-1]
                        points[-1] = w_branch.Origin
                        if points[-1].DistanceTo(points[-2]) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                            raise ValueError(u"{}: last segment is too short to reach the wye.".format(label))
                        wye = (wye_inst, w_lean, w_other, w_branch, joint_point, round(branch_angle, 1))
                        end_mode = "wye"

                pipes = []
                for i in range(len(points) - 1):
                    a = points[i]
                    if i == 0 and direct is not None:
                        # temporary pipe, deleted once the elbow exists; long enough not to be consumed
                        a = points[1].Subtract(direct["dir"].Multiply(direct["leg_ft"] + _mm_to_ft(150.0)))
                    pipe = DB.Plumbing.Pipe.Create(doc, system_type.Id, pipe_type.Id, level.Id,
                                                   a, points[i + 1])
                    if seg_dia[i] is not None:
                        _set_param(pipe, DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM, _mm_to_ft(seg_dia[i]))
                    pipes.append(pipe)
                doc.Regenerate()

                fitting_ids = []
                errors = []
                if connect and len(pipes) > 1:
                    for i in range(len(pipes) - 1):
                        joint = points[i + 1]
                        c1 = _nearest_connector(pipes[i], joint)
                        c2 = _nearest_connector(pipes[i + 1], joint)
                        kind = u"transition" if i in transition_joints else u"elbow"
                        try:
                            if i in transition_joints:
                                fitting = doc.Create.NewTransitionFitting(c1, c2)
                            else:
                                fitting = doc.Create.NewElbowFitting(c1, c2)
                            fitting_ids.append(get_element_id_text(fitting.Id))
                        except Exception as fitting_error:
                            errors.append(u"{} at joint {}: {}".format(kind, i + 1, _text(fitting_error)))

                for xyz, symbol in joint_swaps:
                    swapped = False
                    for fid in fitting_ids:
                        fitting = doc.GetElement(make_element_id(DB, fid))
                        try:
                            at = fitting.Location.Point
                        except Exception:
                            continue
                        if at.DistanceTo(xyz) * MM_PER_FOOT < 60.0:
                            _ensure_symbol_active(doc, symbol)
                            fitting.ChangeTypeId(symbol.Id)
                            swapped = True
                            break
                    if not swapped:
                        errors.append(u"joint_families: no elbow found at {}".format(_xyz_mm(xyz, level.Elevation)))
                if joint_swaps:
                    doc.Regenerate()

                direct_elbow_id = None
                if direct is not None:
                    if not fitting_ids:
                        raise ValueError(u"{}: direct_start: no elbow was made at the first corner.".format(label))
                    direct_elbow_id = fitting_ids[0]
                    elbow = doc.GetElement(make_element_id(DB, direct_elbow_id))
                    doc.Delete(pipes[0].Id)
                    doc.Regenerate()
                    target = direct["connector"]
                    open_c = None
                    for c in _piping_connectors(elbow):
                        if not c.IsConnected and (open_c is None or
                                                  c.Origin.DistanceTo(target.Origin) < open_c.Origin.DistanceTo(target.Origin)):
                            open_c = c
                    if open_c is None:
                        raise ValueError(u"{}: direct_start: the elbow has no open end.".format(label))
                    shift = target.Origin.Subtract(open_c.Origin)
                    if shift.GetLength() * MM_PER_FOOT > 0.01:
                        DB.ElementTransformUtils.MoveElement(doc, elbow.Id, shift)
                        doc.Regenerate()
                        open_c = [c for c in _piping_connectors(elbow) if not c.IsConnected][0]
                    open_c.ConnectTo(target)
                    pipes = pipes[1:]
                    points = points[1:]
                    summary["start_result"] = {"mode": "direct_elbow", "fitting_id": direct_elbow_id,
                                               "on_fitting": get_element_id_text(direct["fitting"].Id)}
                elif start_c is not None:
                    try:
                        result = _join(doc, _nearest_connector(pipes[0], points[0]), start_c)
                        summary["start_result"] = result
                        if result.get("fitting_id"):
                            fitting_ids.append(result["fitting_id"])
                    except Exception as start_error:
                        errors.append(u"start connect: {}".format(_text(start_error)))

                broken_piece = None
                if end_mode == "wye":
                    wye_inst, w_lean, w_other, w_branch, joint_point, branch_angle = wye
                    main = end_target[2]
                    broken_piece = _splice_into_main(doc, main, joint_point, w_lean, w_other, label)
                    w_branch = _connector_by_id(wye_inst, w_branch.Id)
                    _nearest_connector(pipes[-1], w_branch.Origin).ConnectTo(w_branch)
                    fitting_ids.append(get_element_id_text(wye_inst.Id))
                    summary["end_mode"] = "wye"
                    summary["end_result"] = {"mode": "wye", "fitting_id": get_element_id_text(wye_inst.Id),
                                             "junction": _symbol_label(wye_inst.Symbol),
                                             "branch_angle_deg": branch_angle,
                                             "main_pipe_split_into": [get_element_id_text(main.Id),
                                                                      get_element_id_text(broken_piece.Id)]}
                elif end_mode == "connect":
                    try:
                        result = _join(doc, _nearest_connector(pipes[-1], points[-1]), end_target)
                        summary["end_result"] = result
                        if result.get("fitting_id"):
                            fitting_ids.append(result["fitting_id"])
                    except Exception as end_error:
                        errors.append(u"end connect: {}".format(_text(end_error)))
                elif end_mode == "tee":
                    main = end_target[2]
                    joint = points[-1]
                    _check_tee_clearance(main, joint, label, _merged(payload, item, "tee_clearance_mm"))
                    branch_c = _nearest_connector(pipes[-1], joint)
                    try:
                        main_curve = main.Location.Curve
                        near_end = None
                        for k in (0, 1):
                            if main_curve.GetEndPoint(k).DistanceTo(joint) * MM_PER_FOOT < END_AS_ELBOW_MM:
                                near_end = main_curve.GetEndPoint(k)
                        if near_end is not None:
                            main_c = _nearest_connector(main, near_end)
                            result = _join(doc, branch_c, main_c)
                            summary["end_result"] = result
                            if result.get("fitting_id"):
                                fitting_ids.append(result["fitting_id"])
                        else:
                            new_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, main.Id, joint)
                            doc.Regenerate()
                            broken_piece = doc.GetElement(new_id)
                            c_a = _nearest_connector(main, joint)
                            c_b = _nearest_connector(broken_piece, joint)
                            symbol, how = _junction_symbol(doc, main, _merged(payload, item, "junction_family"),
                                                           branch_c, label)
                            try:
                                tee = _new_tee_with(doc, main, symbol, c_a, c_b, branch_c)
                            except Exception:
                                # A wye leans its branch toward one run end; try the other way round.
                                tee = _new_tee_with(doc, main, symbol, c_b, c_a, branch_c)
                            fitting_ids.append(get_element_id_text(tee.Id))
                            summary["end_result"] = {"mode": "tee", "fitting_id": get_element_id_text(tee.Id),
                                                     "junction": _symbol_label(tee.Symbol),
                                                     "junction_selected_by": how,
                                                     "main_pipe_split_into": [get_element_id_text(main.Id),
                                                                              get_element_id_text(new_id)]}
                    except Exception as tee_error:
                        # Roll the whole run back: by now the main pipe may already be cut in two.
                        raise ValueError(u"{}: tee could not be made, nothing was created: {}".format(
                            label, _text(tee_error)))

                doc.Regenerate()
                overlaps = []
                for i, pipe in enumerate(pipes):
                    planned = points[i + 1].Subtract(points[i]).Normalize()
                    problem = _overlap_problem(pipe, planned)
                    if problem:
                        overlaps.append(u"segment {} ({} -> {}): {}".format(
                            i, _xyz_mm(points[i], level.Elevation), _xyz_mm(points[i + 1], level.Elevation), problem))
                if end_mode in ("tee", "wye"):
                    for piece in (end_target[2], broken_piece):
                        if piece is None:
                            continue
                        problem = _overlap_problem(piece)
                        if problem:
                            overlaps.append(u"main pipe {} at the tee: {}".format(
                                get_element_id_text(piece.Id) if piece.IsValidObject else u"?", problem))
                if overlaps:
                    raise ValueError(u"{}: fittings would overlap, nothing was created. {}. Lengthen those "
                                     u"segments (or move the bend / branch point).".format(label, u"; ".join(overlaps)))

                clash = _overlapping_fittings(doc, fitting_ids)
                if direct_elbow_id is not None:
                    allowed = set([direct_elbow_id, get_element_id_text(direct["fitting"].Id)])
                    clash = [pair for pair in clash if set(pair) != allowed]
                if clash:
                    raise ValueError(u"{}: fitting bodies would overlap, nothing was created: {}. Leave more "
                                     u"straight pipe between them (move the bend, branch or trap).".format(
                                         label, _describe_overlaps(doc, clash)))

                summary["pipe_ids"] = [get_element_id_text(p.Id) for p in pipes]
                summary["fitting_ids"] = fitting_ids
                if errors:
                    summary["connection_errors"] = errors
                if notes:
                    summary["notes"] = notes
                run_pipes[index] = pipes
                if broken_piece is not None:
                    if end_target[3] is not None and end_target[3] in run_pipes:
                        run_pipes[end_target[3]].append(broken_piece)
                    elif end_spec.get("pipe_id") is not None:
                        split_pieces.setdefault(_text(end_spec.get("pipe_id")), []).append(broken_piece)
                return summary

            def create_one_traced(index, item, is_dry_run):
                try:
                    return create_one(index, item, is_dry_run)
                except Exception as unexpected:
                    if _text(unexpected).startswith(u"pipes[") or _text(unexpected).startswith(u"'"):
                        raise
                    import traceback
                    raise ValueError(u"{} | {}".format(_text(unexpected), _text(traceback.format_exc())[-1500:]))

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Pipes", items, create_one_traced, dry_run)
            return _build_response("pipe runs", created, failed, warnings, outcome, dry_run,
                                   extra={"route_version": MEP_ROUTES_VERSION})
        except Exception as e:
            route_logger.error("Error in /mep/create_pipes: {}".format(e), exc_info=True)
            return _error(_text(e))
