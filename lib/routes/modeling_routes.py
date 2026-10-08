# RevitMCP: Modeling (element creation) HTTP routes
# -*- coding: UTF-8 -*-
"""
Routes that CREATE new model elements: structural/architectural columns,
walls, structural beams, floors, doors, windows, pipes (plumbing / fire
protection runs with elbows) and MEP fixtures/equipment, plus a helper that
lists the levels and types available for modeling.

Conventions
- All lengths in the payload are millimetres (x_mm, y_mm, height_mm, ...).
  They are converted to Revit internal feet here.
- Each call runs in ONE transaction, so a single Ctrl+Z in Revit undoes the
  whole batch. Each item runs in its own SubTransaction, so one bad item is
  rolled back and reported without losing the others.
- Revit warnings (overlaps, joins, ...) are swallowed and returned in the
  response instead of popping up dialogs that would block the route.
- dry_run=true validates levels/types/geometry without creating anything.
- Errors are returned with HTTP 200 and status="error" so the MCP side always
  sees the full details.
"""

import math

from pyrevit import script, DB
from System.Collections.Generic import List

from routes.json_safety import sanitize_for_json
from routes.revit_compat import get_element_id_text, get_element_id_value, make_element_id


MM_PER_FOOT = 304.8
MAX_ITEMS_HARD_LIMIT = 1000
DEFAULT_MAX_ITEMS = 200
MIN_CURVE_LENGTH_MM = 1.0

try:
    TEXT_TYPES = (basestring,)
except NameError:
    TEXT_TYPES = (str,)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _text(value):
    if value is None:
        return u""
    try:
        return u"{}".format(value).strip()
    except Exception:
        try:
            return unicode(value).strip()  # noqa: F821 (IronPython 2)
        except Exception:
            return u""


def _name(element):
    """
    Element name that is safe on Revit 2025+ (.NET 8). For element types, read the
    built-in type-name parameter first; never use DB.Element.Name.__get__.
    """
    if element is None:
        return u""
    try:
        is_type = isinstance(element, DB.ElementType)
    except Exception:
        is_type = False
    if is_type:
        for bip_name in ("SYMBOL_NAME_PARAM", "ALL_MODEL_TYPE_NAME"):
            try:
                bip = getattr(DB.BuiltInParameter, bip_name, None)
                if bip is None:
                    continue
                param = element.get_Parameter(bip)
                if param is not None:
                    value = param.AsString()
                    if value:
                        return _text(value)
            except Exception:
                pass
    try:
        value = getattr(element, "Name", None)
        if value:
            return _text(value)
    except Exception:
        pass
    return u""

def _coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in (u"1", u"true", u"yes", u"y", u"on"):
        return True
    if text in (u"0", u"false", u"no", u"n", u"off"):
        return False
    return default


def _to_float(value, field_name, default=None, required=False):
    if value is None or (isinstance(value, TEXT_TYPES) and not _text(value)):
        if required:
            raise ValueError(u"'{}' is required.".format(field_name))
        return default
    try:
        return float(value)
    except Exception:
        raise ValueError(u"'{}' must be a number (got {}).".format(field_name, _text(value)))


def _mm_to_ft(value_mm):
    return float(value_mm) / MM_PER_FOOT


def _ft_to_mm(value_ft):
    return round(float(value_ft) * MM_PER_FOOT, 3)


def _point_from(raw, field_name, z_ft=0.0):
    """Accept {"x_mm":..,"y_mm":..} / {"x":..,"y":..} / [x, y]."""
    if isinstance(raw, dict):
        x = raw.get("x_mm", raw.get("x"))
        y = raw.get("y_mm", raw.get("y"))
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        x, y = raw[0], raw[1]
    else:
        raise ValueError(u"'{}' must be {{x_mm, y_mm}} or [x_mm, y_mm].".format(field_name))
    x_mm = _to_float(x, field_name + u".x_mm", required=True)
    y_mm = _to_float(y, field_name + u".y_mm", required=True)
    return DB.XYZ(_mm_to_ft(x_mm), _mm_to_ft(y_mm), z_ft)


def _items_from(payload, key):
    items = payload.get(key)
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if not isinstance(items, (list, tuple)):
        raise ValueError(u"'{}' must be a list.".format(key))
    return list(items)


def _max_items(payload):
    try:
        value = int(payload.get("max_items", DEFAULT_MAX_ITEMS))
    except Exception:
        value = DEFAULT_MAX_ITEMS
    return max(1, min(MAX_ITEMS_HARD_LIMIT, value))


def _error(message, **extra):
    data = {"status": "error", "message": message}
    data.update(extra)
    return sanitize_for_json(data)


def _set_param(element, built_in_param, value):
    """Set a built-in parameter if it exists and is writable. Returns True on success."""
    try:
        param = element.get_Parameter(built_in_param)
    except Exception:
        param = None
    if param is None or param.IsReadOnly:
        return False
    try:
        param.Set(value)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lookups (levels and types)
# ---------------------------------------------------------------------------

def _all_levels(doc):
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    levels.sort(key=lambda lv: lv.Elevation)
    return levels


def _level_names(doc):
    return [_name(lv) for lv in _all_levels(doc)]


def _find_level(doc, name, field_name="level_name"):
    wanted = _text(name)
    if not wanted:
        raise ValueError(u"'{}' is required. Available levels: {}".format(
            field_name, u", ".join(_level_names(doc))))
    levels = _all_levels(doc)
    for lv in levels:
        if _name(lv) == wanted:
            return lv
    for lv in levels:
        if _name(lv).lower() == wanted.lower():
            return lv
    raise ValueError(u"Level '{}' not found. Available levels: {}".format(
        wanted, u", ".join(_level_names(doc))))


def _symbols_in_category(doc, built_in_category):
    return list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.FamilySymbol)
        .OfCategory(built_in_category)
        .ToElements()
    )


def _symbol_label(symbol):
    family_name = u""
    try:
        family_name = _name(symbol.Family)
    except Exception:
        pass
    return u"{} : {}".format(family_name, _name(symbol))


def _find_family_symbol(doc, built_in_category, type_name, family_name=None, category_label=u"type"):
    symbols = _symbols_in_category(doc, built_in_category)
    available = [_symbol_label(s) for s in symbols]
    if not symbols:
        raise ValueError(u"No {} families are loaded in this project. Load a family first.".format(category_label))

    wanted_type = _text(type_name)
    wanted_family = _text(family_name)

    # Allow "Family : Type" in type_name.
    if wanted_type and not wanted_family and u":" in wanted_type:
        parts = wanted_type.split(u":", 1)
        wanted_family, wanted_type = parts[0].strip(), parts[1].strip()

    if not wanted_type:
        raise ValueError(u"'type_name' is required. Available {} types: {}".format(
            category_label, u"; ".join(available[:60])))

    matches = []
    for symbol in symbols:
        if _name(symbol) != wanted_type:
            continue
        if wanted_family:
            try:
                if _name(symbol.Family) != wanted_family:
                    continue
            except Exception:
                continue
        matches.append(symbol)

    if not matches:
        raise ValueError(u"{} type '{}'{} not found. Available: {}".format(
            category_label, wanted_type,
            u" in family '{}'".format(wanted_family) if wanted_family else u"",
            u"; ".join(available[:60])))
    if len(matches) > 1:
        raise ValueError(u"Type name '{}' exists in several families; pass family_name. Candidates: {}".format(
            wanted_type, u"; ".join(_symbol_label(s) for s in matches)))
    return matches[0]


def _find_system_type(doc, type_class, type_name, category_label):
    types = list(DB.FilteredElementCollector(doc).OfClass(type_class).ToElements())
    available = [_name(t) for t in types]
    wanted = _text(type_name)
    if not wanted:
        raise ValueError(u"'type_name' is required. Available {} types: {}".format(
            category_label, u"; ".join(available[:80])))
    for t in types:
        if _name(t) == wanted:
            return t
    for t in types:
        if _name(t).lower() == wanted.lower():
            return t
    raise ValueError(u"{} type '{}' not found. Available: {}".format(
        category_label, wanted, u"; ".join(available[:80])))


def _ensure_symbol_active(doc, symbol):
    if not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()


# ---------------------------------------------------------------------------
# Transaction runner (one transaction, one sub-transaction per item)
# ---------------------------------------------------------------------------

class _WarningCollector(DB.IFailuresPreprocessor):
    """Deletes Revit warnings so no dialog blocks the route; records their text."""

    def __init__(self):
        self.warnings = []

    def PreprocessFailures(self, failures_accessor):
        try:
            for failure in failures_accessor.GetFailureMessages():
                if failure.GetSeverity() == DB.FailureSeverity.Warning:
                    try:
                        self.warnings.append(_text(failure.GetDescriptionText()))
                    except Exception:
                        pass
                    failures_accessor.DeleteWarning(failure)
        except Exception:
            pass
        return DB.FailureProcessingResult.Continue


def _run_creation(doc, transaction_name, items, create_one, dry_run):
    """
    create_one(index, item, dry_run) -> dict summary of what was (or would be) created.
    It must raise ValueError for bad input.
    """
    created = []
    failed = []
    collector = _WarningCollector()

    if dry_run:
        for index, item in enumerate(items):
            try:
                created.append(create_one(index, item, True))
            except Exception as item_error:
                failed.append({"index": index, "error": _text(item_error)})
        return created, failed, [], "dry_run"

    transaction = DB.Transaction(doc, transaction_name)
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(collector)
    options.SetClearAfterRollback(True)
    transaction.SetFailureHandlingOptions(options)
    transaction.Start()
    try:
        for index, item in enumerate(items):
            sub = DB.SubTransaction(doc)
            sub.Start()
            try:
                summary = create_one(index, item, False)
                sub.Commit()
                created.append(summary)
            except Exception as item_error:
                try:
                    sub.RollBack()
                except Exception:
                    pass
                failed.append({"index": index, "error": _text(item_error)})

        if not created:
            transaction.RollBack()
            return created, failed, collector.warnings, "rolled_back"

        commit_status = transaction.Commit()
        if commit_status != DB.TransactionStatus.Committed:
            return [], failed + [{"index": None, "error": u"Revit refused to commit the transaction ({}).".format(
                _text(commit_status))}], collector.warnings, "rolled_back"
        return created, failed, collector.warnings, "committed"
    except Exception:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass
        raise


def _build_response(kind, created, failed, warnings, outcome, dry_run, extra=None):
    if outcome == "dry_run":
        status = "dry_run" if not failed else ("dry_run_with_errors" if created else "error")
        message = u"Dry run: {} {} would be created, {} invalid.".format(len(created), kind, len(failed))
    elif created and not failed:
        status = "success"
        message = u"Created {} {}.".format(len(created), kind)
    elif created and failed:
        status = "partial_success"
        message = u"Created {} {}; {} failed.".format(len(created), kind, len(failed))
    else:
        status = "error"
        message = u"No {} were created.".format(kind)

    data = {
        "status": status,
        "message": message,
        "dry_run": dry_run,
        "created_count": len(created) if outcome != "dry_run" else 0,
        "created": created,
        "failed": failed,
        "warnings": warnings[:50],
        "warning_count": len(warnings),
    }
    if outcome == "committed":
        data["undo_hint"] = u"All elements from this call were created in one transaction; one Ctrl+Z in Revit undoes them."
    if extra:
        data.update(extra)
    return sanitize_for_json(data)


def _prepare(request, list_key):
    payload = request.data if hasattr(request, "data") else {}
    if payload is None or not isinstance(payload, dict):
        raise ValueError(u"Invalid JSON payload.")
    items = _items_from(payload, list_key)
    if not items:
        raise ValueError(u"'{}' must contain at least one item.".format(list_key))
    limit = _max_items(payload)
    if len(items) > limit:
        raise ValueError(u"Refusing to create {} items because max_items is {}.".format(len(items), limit))
    dry_run = _coerce_bool(payload.get("dry_run"), default=False)
    return payload, items, dry_run


def _merged(payload, item, key, default=None):
    """Per-item value overrides the call-level value."""
    if isinstance(item, dict) and item.get(key) is not None:
        return item.get(key)
    if payload.get(key) is not None:
        return payload.get(key)
    return default


def _check_document(doc):
    if doc is None:
        raise ValueError(u"No active Revit document.")
    if doc.IsReadOnly:
        raise ValueError(u"The active document is read-only.")
    if doc.IsFamilyDocument:
        raise ValueError(u"The active document is a family document; open a project.")


# ---------------------------------------------------------------------------
# Wall hosting (doors, windows, wall-hosted fixtures)
# ---------------------------------------------------------------------------

DEFAULT_SNAP_TOLERANCE_MM = 300.0


def _wall_curve(wall):
    try:
        location = wall.Location
        if isinstance(location, DB.LocationCurve):
            return location.Curve
    except Exception:
        pass
    return None


def _is_curtain_wall(wall):
    try:
        return wall.WallType.Kind == DB.WallKind.Curtain
    except Exception:
        return False


def _wall_by_id(doc, raw_id):
    try:
        element = doc.GetElement(make_element_id(DB, raw_id))
    except Exception:
        element = None
    if element is None or not isinstance(element, DB.Wall):
        raise ValueError(u"wall_id {} is not an existing wall.".format(_text(raw_id)))
    return element


def _nearest_wall(doc, point, z_check_ft, tolerance_ft):
    """Closest non-curtain wall (in plan) whose height range contains z_check_ft."""
    best = None
    for wall in DB.FilteredElementCollector(doc).OfClass(DB.Wall).ToElements():
        if _is_curtain_wall(wall):
            continue
        curve = _wall_curve(wall)
        if curve is None:
            continue
        try:
            bbox = wall.get_BoundingBox(None)
            if bbox is not None and not (bbox.Min.Z - 1e-3 <= z_check_ft <= bbox.Max.Z + 1e-3):
                continue
        except Exception:
            pass
        flat = DB.XYZ(point.X, point.Y, curve.GetEndPoint(0).Z)
        projection = curve.Project(flat)
        if projection is None:
            continue
        distance = projection.Distance
        if distance <= tolerance_ft and (best is None or distance < best[0]):
            best = (distance, wall, projection.XYZPoint)
    return best


def _distance_along(curve, point_on_curve):
    try:
        if isinstance(curve, DB.Line):
            start = curve.GetEndPoint(0)
            flat = DB.XYZ(point_on_curve.X, point_on_curve.Y, start.Z)
            return _ft_to_mm(start.DistanceTo(flat))
    except Exception:
        pass
    return None


def _resolve_wall_host(doc, payload, item, label, z_above_level_mm):
    """
    Returns (wall, location XYZ at the level elevation, level, distance_along_mm, snap_distance_mm).
    Mode A: item has wall_id + distance_mm (distance from the wall's start point).
    Mode B: item has x_mm/y_mm; the nearest wall within snap_tolerance_mm is used.
    """
    wall_id = item.get("wall_id") if isinstance(item, dict) else None
    level_name = _merged(payload, item, "level_name")

    if wall_id is not None and _text(wall_id):
        wall = _wall_by_id(doc, wall_id)
        if _is_curtain_wall(wall):
            raise ValueError(u"{}: curtain walls are not supported as hosts.".format(label))
        curve = _wall_curve(wall)
        if curve is None:
            raise ValueError(u"{}: wall {} has no location line.".format(label, _text(wall_id)))
        distance_mm = _to_float(item.get("distance_mm"), label + u".distance_mm", required=True)
        length_mm = _ft_to_mm(curve.Length)
        if distance_mm <= 0 or distance_mm >= length_mm:
            raise ValueError(u"{}: distance_mm must be between 0 and the wall length ({} mm).".format(
                label, length_mm))
        on_wall = curve.Evaluate(distance_mm / length_mm, True)
        level = _find_level(doc, level_name) if level_name else doc.GetElement(wall.LevelId)
        location = DB.XYZ(on_wall.X, on_wall.Y, level.Elevation)
        return wall, location, level, distance_mm, 0.0

    level = _find_level(doc, level_name)
    point = _point_from(item.get("point", item) if isinstance(item, dict) else item, label, level.Elevation)
    tolerance_mm = _to_float(_merged(payload, item, "snap_tolerance_mm"), "snap_tolerance_mm",
                             DEFAULT_SNAP_TOLERANCE_MM)
    z_check = level.Elevation + _mm_to_ft(max(z_above_level_mm, 1.0))
    found = _nearest_wall(doc, point, z_check, _mm_to_ft(tolerance_mm))
    if found is None:
        raise ValueError(u"{}: no wall found within {} mm of ({}, {}) on level '{}'. "
                         u"Use wall_id + distance_mm, or raise snap_tolerance_mm.".format(
                             label, tolerance_mm, _ft_to_mm(point.X), _ft_to_mm(point.Y), _name(level)))
    distance_ft, wall, on_wall = found
    location = DB.XYZ(on_wall.X, on_wall.Y, level.Elevation)
    return wall, location, level, _distance_along(_wall_curve(wall), on_wall), _ft_to_mm(distance_ft)


def _wall_face_candidates(doc):
    """(wall, transform to host, link instance or None) for host walls and walls inside loaded links."""
    for wall in DB.FilteredElementCollector(doc).OfClass(DB.Wall).ToElements():
        yield wall, None, None
    for link in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements():
        try:
            link_doc = link.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            continue
        transform = link.GetTotalTransform()
        for wall in DB.FilteredElementCollector(link_doc).OfClass(DB.Wall).ToElements():
            yield wall, transform, link


def _wall_face_host(doc, point, z_ft, tolerance_ft, label):
    """Find the wall side face nearest to `point` (plan) and return what NewFamilyInstance needs.

    Works for walls in the host model and in Revit links (the face reference is converted with
    CreateLinkReference). The face is the side of the wall the point is on.
    """
    best = None
    for wall, transform, link in _wall_face_candidates(doc):
        if _is_curtain_wall(wall):
            continue
        curve = _wall_curve(wall)
        if curve is None or not isinstance(curve, DB.Line):
            continue
        a = curve.GetEndPoint(0)
        b = curve.GetEndPoint(1)
        if transform is not None:
            a = transform.OfPoint(a)
            b = transform.OfPoint(b)
        try:
            bbox = wall.get_BoundingBox(None)
            if bbox is not None:
                lo = bbox.Min.Z + (transform.Origin.Z if transform is not None else 0.0)
                hi = bbox.Max.Z + (transform.Origin.Z if transform is not None else 0.0)
                if not (lo - 1e-3 <= z_ft <= hi + 1e-3):
                    continue
        except Exception:
            pass
        a2 = DB.XYZ(a.X, a.Y, 0.0)
        b2 = DB.XYZ(b.X, b.Y, 0.0)
        p2 = DB.XYZ(point.X, point.Y, 0.0)
        ab = b2.Subtract(a2)
        length = ab.GetLength()
        if length < 1e-6:
            continue
        t = max(0.0, min(1.0, p2.Subtract(a2).DotProduct(ab) / (length * length)))
        on_line = a2.Add(ab.Multiply(t))
        distance = on_line.DistanceTo(p2)
        if distance > tolerance_ft:
            continue
        if best is None or distance < best[0]:
            best = (distance, wall, transform, link, on_line)
    if best is None:
        raise ValueError(u"{}: no wall (host or linked) within {} mm of ({}, {}) at that height.".format(
            label, _ft_to_mm(tolerance_ft), _ft_to_mm(point.X), _ft_to_mm(point.Y)))

    distance, wall, transform, link, on_line = best
    normal = wall.Orientation
    if transform is not None:
        normal = transform.OfVector(normal)
    normal = DB.XYZ(normal.X, normal.Y, 0.0).Normalize()
    side = DB.XYZ(point.X, point.Y, 0.0).Subtract(on_line).DotProduct(normal)
    if abs(side) < 1e-6:
        raise ValueError(u"{}: the point is on the wall centreline; move it to the side the fixture "
                         u"should hang on.".format(label))
    if side > 0:
        shell = DB.ShellLayerType.Exterior
    else:
        shell = DB.ShellLayerType.Interior
        normal = normal.Negate()
    refs = list(DB.HostObjectUtils.GetSideFaces(wall, shell))
    if not refs:
        raise ValueError(u"{}: wall {} has no side face on that side.".format(label, get_element_id_text(wall.Id)))
    reference = refs[0]
    if link is not None:
        reference = reference.CreateLinkReference(link)
    half_width = wall.Width / 2.0
    face_point = on_line.Add(normal.Multiply(half_width))
    location = DB.XYZ(face_point.X, face_point.Y, z_ft)
    return {
        "reference": reference,
        "location": location,
        "normal": normal,
        # family X along the wall, chosen so family Y (= Z x X) points up
        "ref_dir": DB.XYZ.BasisZ.CrossProduct(normal),
        "info": {
            "wall_id": get_element_id_text(wall.Id),
            "link_instance_id": get_element_id_text(link.Id) if link is not None else None,
            "side": "exterior" if shell == DB.ShellLayerType.Exterior else "interior",
            "snap_distance_mm": _ft_to_mm(distance),
            "face_normal": {"x": round(normal.X, 4), "y": round(normal.Y, 4)},
        },
    }


def _apply_flips(instance, item):
    flipped = []
    try:
        if _coerce_bool(item.get("flip_facing") if isinstance(item, dict) else None) and instance.CanFlipFacing:
            instance.flipFacing()
            flipped.append("facing")
    except Exception:
        pass
    try:
        if _coerce_bool(item.get("flip_hand") if isinstance(item, dict) else None) and instance.CanFlipHand:
            instance.flipHand()
            flipped.append("hand")
    except Exception:
        pass
    return flipped


def _set_first_param(element, built_in_names, value):
    for name in built_in_names:
        bip = getattr(DB.BuiltInParameter, name, None)
        if bip is not None and _set_param(element, bip, value):
            return name
    return None


OFFSET_PARAM_NAMES = ("INSTANCE_FREE_HOST_OFFSET_PARAM", "INSTANCE_ELEVATION_PARAM")


# ---------------------------------------------------------------------------
# MEP lookups
# ---------------------------------------------------------------------------

_FIXTURE_CATEGORY_KEYS = (
    ("plumbing_fixtures", "OST_PlumbingFixtures"),
    ("plumbing_equipment", "OST_PlumbingEquipment"),
    ("sprinklers", "OST_Sprinklers"),
    ("mechanical_equipment", "OST_MechanicalEquipment"),
    ("lighting_fixtures", "OST_LightingFixtures"),
    ("electrical_fixtures", "OST_ElectricalFixtures"),
    ("electrical_equipment", "OST_ElectricalEquipment"),
    ("fire_alarm_devices", "OST_FireAlarmDevices"),
)


def _fixture_categories():
    out = []
    for key, ost in _FIXTURE_CATEGORY_KEYS:
        bic = getattr(DB.BuiltInCategory, ost, None)
        if bic is not None:
            out.append((key, bic))
    return out


def _fixture_category(key):
    wanted = _text(key).lower()
    for k, bic in _fixture_categories():
        if k == wanted:
            return bic
    raise ValueError(u"category must be one of: {}".format(u", ".join(k for k, _ in _fixture_categories())))


def _placement_type_text(symbol):
    try:
        return _text(symbol.Family.FamilyPlacementType)
    except Exception:
        return u""


def _point3_from(raw, label, level_elevation_ft, default_offset_mm):
    """Plan point plus optional z_mm (centre height above the level)."""
    if isinstance(raw, dict):
        z_mm = _to_float(raw.get("z_mm"), label + u".z_mm", default_offset_mm)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
        z_mm = _to_float(raw[2], label + u".z_mm", default_offset_mm)
    else:
        z_mm = default_offset_mm
    return _point_from(raw, label, level_elevation_ft + _mm_to_ft(z_mm))


def _drop_collinear(points):
    if len(points) < 3:
        return points
    kept = [points[0]]
    for i in range(1, len(points) - 1):
        a = (points[i] - kept[-1])
        b = (points[i + 1] - points[i])
        if a.GetLength() < 1e-9 or b.GetLength() < 1e-9:
            continue
        if abs(a.Normalize().DotProduct(b.Normalize())) > 0.99999:
            continue
        kept.append(points[i])
    kept.append(points[-1])
    return kept


def _connector_near(element, point):
    best = None
    try:
        connectors = element.ConnectorManager.Connectors
    except Exception:
        return None
    for connector in connectors:
        try:
            distance = connector.Origin.DistanceTo(point)
        except Exception:
            continue
        if best is None or distance < best[0]:
            best = (distance, connector)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Trimming against existing structure (floors inside columns/beams,
# walls stopping at column faces)
# ---------------------------------------------------------------------------

SQFT_TO_M2 = 0.09290304
FLOOR_PROBE_DEPTH_MM = 50.0     # probe slab from (top - 50mm) ...
FLOOR_PROBE_TOP_GAP_MM = 10.0   # ... to (top - 10mm)
MIN_WALL_PIECE_MM = 50.0


def _structure_categories(include_beams):
    names = ["OST_StructuralColumns", "OST_Columns"]
    if include_beams:
        names.append("OST_StructuralFraming")
    cats = List[DB.BuiltInCategory]()
    for name in names:
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is not None:
            cats.Add(bic)
    return cats


def _elements_in_outline(doc, outline, include_beams):
    category_filter = DB.ElementMulticategoryFilter(_structure_categories(include_beams))
    bbox_filter = DB.BoundingBoxIntersectsFilter(outline)
    return list(
        DB.FilteredElementCollector(doc)
        .WhereElementIsNotElementType()
        .WherePasses(category_filter)
        .WherePasses(bbox_filter)
        .ToElements()
    )


def _element_solids(element):
    options = DB.Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    solids = []
    try:
        geometry = element.get_Geometry(options)
    except Exception:
        geometry = None
    if geometry is None:
        return solids
    for obj in geometry:
        if isinstance(obj, DB.Solid):
            if obj.Volume > 1e-9:
                solids.append(obj)
        elif isinstance(obj, DB.GeometryInstance):
            try:
                for sub in obj.GetInstanceGeometry():
                    if isinstance(sub, DB.Solid) and sub.Volume > 1e-9:
                        solids.append(sub)
            except Exception:
                pass
    return solids


def _curve_loop(curves, counter_clockwise):
    loop = DB.CurveLoop()
    for curve in curves:
        loop.Append(curve)
    try:
        if loop.IsCounterclockwise(DB.XYZ.BasisZ) != counter_clockwise:
            loop.Flip()
    except Exception:
        pass
    return loop


def _trimmed_floor_loops(doc, outer_curves, opening_curves_list, level, offset_mm):
    """
    Subtract column and beam solids from the floor outline.
    Returns (list of pieces, info). Each piece is (List[CurveLoop] at level elevation, area_m2).
    """
    top_z = level.Elevation + _mm_to_ft(offset_mm)
    probe_bottom = top_z - _mm_to_ft(FLOOR_PROBE_DEPTH_MM)
    probe_height = _mm_to_ft(FLOOR_PROBE_DEPTH_MM - FLOOR_PROBE_TOP_GAP_MM)
    move_down = DB.Transform.CreateTranslation(DB.XYZ(0, 0, probe_bottom - level.Elevation))

    loops = List[DB.CurveLoop]()
    loops.Add(DB.CurveLoop.CreateViaTransform(_curve_loop(outer_curves, True), move_down))
    for opening_curves in opening_curves_list:
        loops.Add(DB.CurveLoop.CreateViaTransform(_curve_loop(opening_curves, False), move_down))
    probe = DB.GeometryCreationUtilities.CreateExtrusionGeometry(loops, DB.XYZ.BasisZ, probe_height)

    box = probe.GetBoundingBox()
    t = box.Transform
    lo, hi = t.OfPoint(box.Min), t.OfPoint(box.Max)
    outline = DB.Outline(DB.XYZ(min(lo.X, hi.X), min(lo.Y, hi.Y), min(lo.Z, hi.Z)),
                         DB.XYZ(max(lo.X, hi.X), max(lo.Y, hi.Y), max(lo.Z, hi.Z)))

    result = probe
    cut_ids = []
    cut_errors = []
    for element in _elements_in_outline(doc, outline, include_beams=True):
        cut_this = False
        for solid in _element_solids(element):
            try:
                result = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                    result, solid, DB.BooleanOperationsType.Difference)
                cut_this = True
            except Exception as boolean_error:
                cut_errors.append(u"{}: {}".format(get_element_id_text(element.Id), _text(boolean_error)))
        if cut_this:
            cut_ids.append(get_element_id_text(element.Id))

    if result is None or result.Volume < 1e-9:
        raise ValueError(u"Nothing is left of the floor after removing columns and beams.")

    move_up = DB.Transform.CreateTranslation(DB.XYZ(0, 0, level.Elevation - (probe_bottom + probe_height)))
    pieces = []
    for face in result.Faces:
        if not isinstance(face, DB.PlanarFace):
            continue
        if face.FaceNormal.Z < 0.999:
            continue
        piece_loops = List[DB.CurveLoop]()
        for loop in face.GetEdgesAsCurveLoops():
            piece_loops.Add(DB.CurveLoop.CreateViaTransform(loop, move_up))
        pieces.append((piece_loops, round(face.Area * SQFT_TO_M2, 3)))

    if not pieces:
        raise ValueError(u"Could not find the top face of the trimmed floor shape.")
    info = {"cut_by_element_ids": cut_ids}
    if cut_errors:
        info["cut_errors"] = cut_errors
    return pieces, info


def _wall_pieces_between_columns(doc, start, end, z_mid):
    """
    Split the wall line start->end (plan) where it passes through columns.
    Returns (list of (start, end) at the original z, list of column ids).
    """
    base_z = start.Z
    a = DB.XYZ(start.X, start.Y, z_mid)
    b = DB.XYZ(end.X, end.Y, z_mid)
    probe = DB.Line.CreateBound(a, b)
    length = probe.Length
    direction = (b - a).Normalize()

    pad = 1.0  # ft
    outline = DB.Outline(DB.XYZ(min(a.X, b.X) - pad, min(a.Y, b.Y) - pad, z_mid - pad),
                         DB.XYZ(max(a.X, b.X) + pad, max(a.Y, b.Y) + pad, z_mid + pad))

    options = DB.SolidCurveIntersectionOptions()
    options.ResultType = DB.SolidCurveIntersectionMode.CurveSegmentsInside

    intervals = []
    column_ids = []
    for column in _elements_in_outline(doc, outline, include_beams=False):
        hit = False
        for solid in _element_solids(column):
            try:
                intersection = solid.IntersectWithCurve(probe, options)
            except Exception:
                continue
            for i in range(intersection.SegmentCount):
                segment = intersection.GetCurveSegment(i)
                t0 = (segment.GetEndPoint(0) - a).DotProduct(direction)
                t1 = (segment.GetEndPoint(1) - a).DotProduct(direction)
                lo, hi = max(0.0, min(t0, t1)), min(length, max(t0, t1))
                if hi - lo > 1e-6:
                    intervals.append((lo, hi))
                    hit = True
        if hit:
            column_ids.append(get_element_id_text(column.Id))

    intervals.sort()
    merged = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    pieces = []
    cursor = 0.0
    min_piece = _mm_to_ft(MIN_WALL_PIECE_MM)
    for lo, hi in merged + [(length, length)]:
        if lo - cursor >= min_piece:
            p0 = start + direction * cursor
            p1 = start + direction * lo
            pieces.append((DB.XYZ(p0.X, p0.Y, base_z), DB.XYZ(p1.X, p1.Y, base_z)))
        cursor = max(cursor, hi)
    return pieces, column_ids


WALL_PROBE_HALF_THICKNESS_MM = 5.0


def _elements_above_wall(doc, outline, base_z):
    cats = List[DB.BuiltInCategory]()
    for name in ("OST_StructuralFraming", "OST_Floors"):
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is not None:
            cats.Add(bic)
    elements = (
        DB.FilteredElementCollector(doc)
        .WhereElementIsNotElementType()
        .WherePasses(DB.ElementMulticategoryFilter(cats))
        .WherePasses(DB.BoundingBoxIntersectsFilter(outline))
        .ToElements()
    )
    out = []
    for element in elements:
        try:
            box = element.get_BoundingBox(None)
            # Ignore the slab/beams of the floor the wall stands on.
            if box is not None and box.Max.Z <= base_z + _mm_to_ft(1.0):
                continue
        except Exception:
            pass
        out.append(element)
    return out


def _wall_profiles_under_structure(doc, p0, p1, base_z, top_z):
    """
    Profile(s) for a wall on the line p0->p1 (plan) from base_z up to top_z, cut back
    so the top follows the underside of beams and floors above the wall centreline.
    Returns (profiles, cut_ids). profiles is None when nothing above cuts the wall,
    otherwise a list of lists of curves in the vertical plane of the centreline.
    """
    a = DB.XYZ(p0.X, p0.Y, base_z)
    b = DB.XYZ(p1.X, p1.Y, base_z)
    direction = (b - a).Normalize()
    normal = DB.XYZ.BasisZ.CrossProduct(direction).Normalize()
    half = _mm_to_ft(WALL_PROBE_HALF_THICKNESS_MM)

    c1 = a + normal * half
    c2 = b + normal * half
    c3 = b - normal * half
    c4 = a - normal * half
    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(c1, c2))
    loop.Append(DB.Line.CreateBound(c2, c3))
    loop.Append(DB.Line.CreateBound(c3, c4))
    loop.Append(DB.Line.CreateBound(c4, c1))
    loops = List[DB.CurveLoop]()
    loops.Add(loop)
    probe = DB.GeometryCreationUtilities.CreateExtrusionGeometry(loops, DB.XYZ.BasisZ, top_z - base_z)

    pad = _mm_to_ft(20.0)
    outline = DB.Outline(
        DB.XYZ(min(c1.X, c2.X, c3.X, c4.X) - pad, min(c1.Y, c2.Y, c3.Y, c4.Y) - pad, base_z),
        DB.XYZ(max(c1.X, c2.X, c3.X, c4.X) + pad, max(c1.Y, c2.Y, c3.Y, c4.Y) + pad, top_z + pad))

    result = probe
    cut_ids = []
    for element in _elements_above_wall(doc, outline, base_z):
        cut_this = False
        for solid in _element_solids(element):
            try:
                result = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                    result, solid, DB.BooleanOperationsType.Difference)
                cut_this = True
            except Exception:
                pass
        if cut_this:
            cut_ids.append(get_element_id_text(element.Id))

    debug = {
        "probe_volume_m3": round(probe.Volume * 0.0283168466, 6),
        "result_volume_m3": round(result.Volume * 0.0283168466, 6) if result is not None else 0.0,
    }
    if not cut_ids:
        return None, [], debug
    if result is None or result.Volume < 1e-9:
        raise ValueError(u"Nothing is left of the wall after removing the beams/floors above it.")
    if abs(result.Volume - probe.Volume) < 1e-6:
        return None, cut_ids, debug

    back_to_centre = DB.Transform.CreateTranslation(normal * (-half))
    profiles = []
    for face in result.Faces:
        if not isinstance(face, DB.PlanarFace):
            continue
        if face.FaceNormal.DotProduct(normal) < 0.999:
            continue
        best = None
        for edge_loop in face.GetEdgesAsCurveLoops():
            zs = []
            xs = []
            curves = []
            for curve in edge_loop:
                moved = curve.CreateTransformed(back_to_centre)
                curves.append(moved)
                for k in (0, 1):
                    pt = moved.GetEndPoint(k)
                    zs.append(pt.Z)
                    xs.append((pt - a).DotProduct(direction))
            extent = (max(zs) - min(zs)) * (max(xs) - min(xs))
            if best is None or extent > best[0]:
                best = (extent, curves, min(zs))
        # Keep only pieces that stand on the wall base.
        if best is not None and best[2] <= base_z + _mm_to_ft(1.0):
            profiles.append(best[1])
    if not profiles:
        raise ValueError(u"Could not build a wall profile under the beams/floors above.")
    return profiles, cut_ids, debug


def _profile_shape(profile, origin, direction):
    """Returns (is_rectangle, z_min, z_max, t_min, t_max) for a vertical wall profile."""
    zs = []
    ts = []
    for curve in profile:
        for k in (0, 1):
            pt = curve.GetEndPoint(k)
            zs.append(round(pt.Z, 6))
            ts.append((pt - origin).DotProduct(direction))
    distinct_z = sorted(set(zs))
    is_rectangle = (len(profile) == 4 and len(distinct_z) == 2
                    and all(isinstance(c, DB.Line) for c in profile))
    return is_rectangle, min(zs), max(zs), min(ts), max(ts)


# ---------------------------------------------------------------------------
# Rooms (ceilings)
# ---------------------------------------------------------------------------

def _all_rooms(doc):
    rooms = []
    for room in (DB.FilteredElementCollector(doc)
                 .OfCategory(DB.BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType()
                 .ToElements()):
        try:
            if room.Area > 1e-6:
                rooms.append(room)
        except Exception:
            pass
    return rooms


def _param_text(element, bip_name):
    try:
        param = element.get_Parameter(getattr(DB.BuiltInParameter, bip_name))
        if param is not None:
            return _text(param.AsString())
    except Exception:
        pass
    return u""


def _room_summary(doc, room):
    level = doc.GetElement(room.LevelId)
    return {
        "id": get_element_id_text(room.Id),
        "name": _param_text(room, "ROOM_NAME"),
        "number": _param_text(room, "ROOM_NUMBER"),
        "level": _name(level),
        "area_m2": round(room.Area * SQFT_TO_M2, 3),
    }


def _room_loops(room):
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    loops = List[DB.CurveLoop]()
    for segment_list in room.GetBoundarySegments(options):
        loop = DB.CurveLoop()
        for segment in segment_list:
            loop.Append(segment.GetCurve())
        if loop.NumberOfCurves() > 0:
            loops.Add(loop)
    if loops.Count == 0:
        raise ValueError(u"Room {} has no boundary.".format(get_element_id_text(room.Id)))
    return loops


# ---------------------------------------------------------------------------
# Join priority: columns > beams > floors > walls
# ---------------------------------------------------------------------------

_JOIN_PRIORITY = (
    ("OST_StructuralColumns", 4, u"column"),
    ("OST_Columns", 4, u"column"),
    ("OST_StructuralFraming", 3, u"beam"),
    ("OST_Floors", 2, u"floor"),
    ("OST_Walls", 1, u"wall"),
)


def _join_priority_map():
    out = {}
    for ost, prio, label in _JOIN_PRIORITY:
        bic = getattr(DB.BuiltInCategory, ost, None)
        if bic is not None:
            out[int(bic)] = (prio, label)
    return out


def _priority_of(element, prio_map):
    try:
        return prio_map.get(get_element_id_value(element.Category.Id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Floor shape editing (sub-elements)
# ---------------------------------------------------------------------------

def _slab_editor(floor):
    getter = getattr(floor, "GetSlabShapeEditor", None)
    if getter is not None:
        return getter()
    return floor.SlabShapeEditor


def _editor_vertices(editor):
    try:
        return list(editor.SlabShapeVertices)
    except Exception:
        return []


def _find_vertex(editor, x_ft, y_ft, tolerance_ft):
    best = None
    for vertex in _editor_vertices(editor):
        pos = vertex.Position
        d = math.hypot(pos.X - x_ft, pos.Y - y_ft)
        if d <= tolerance_ft and (best is None or d < best[0]):
            best = (d, vertex)
    return best[1] if best else None


def _add_point(editor, xyz):
    for method_name in ("DrawPoint", "AddPoint"):
        method = getattr(editor, method_name, None)
        if method is not None:
            return method(xyz)
    raise ValueError(u"This Revit version has no slab point method.")


def _add_split_line(editor, v0, v1):
    for method_name in ("DrawSplitLine", "AddSplitLine"):
        method = getattr(editor, method_name, None)
        if method is not None:
            return method(v0, v1)
    raise ValueError(u"This Revit version has no slab split-line method.")


def _floor_top_z(floor):
    box = floor.get_BoundingBox(None)
    return box.Max.Z if box is not None else 0.0


def _vertex_list(editor, floor_top):
    out = []
    for vertex in _editor_vertices(editor):
        pos = vertex.Position
        out.append({"x_mm": _ft_to_mm(pos.X), "y_mm": _ft_to_mm(pos.Y),
                    "offset_mm": _ft_to_mm(pos.Z - floor_top)})
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_routes(api):

    # ----------------------------------------------------------- list types
    @api.route('/modeling/types', methods=['GET', 'POST'])
    def handle_list_modeling_types(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            levels = [{
                "name": _name(lv),
                "elevation_mm": _ft_to_mm(lv.Elevation),
                "id": get_element_id_text(lv.Id),
            } for lv in _all_levels(doc)]

            def symbols(bic):
                return [{
                    "family_name": _name(s.Family),
                    "type_name": _name(s),
                    "id": get_element_id_text(s.Id),
                } for s in _symbols_in_category(doc, bic)]

            def system_types(cls):
                out = []
                for t in DB.FilteredElementCollector(doc).OfClass(cls).ToElements():
                    entry = {"type_name": _name(t), "id": get_element_id_text(t.Id)}
                    try:
                        entry["kind"] = _text(t.Kind)  # WallKind for walls
                    except Exception:
                        pass
                    out.append(entry)
                return out

            def piping_systems():
                out = []
                for t in DB.FilteredElementCollector(doc).OfClass(DB.Plumbing.PipingSystemType).ToElements():
                    entry = {"type_name": _name(t), "id": get_element_id_text(t.Id)}
                    try:
                        entry["classification"] = _text(t.SystemClassification)
                    except Exception:
                        pass
                    out.append(entry)
                return out

            def fixture_types():
                out = {}
                for key, bic in _fixture_categories():
                    entries = []
                    for s_ in _symbols_in_category(doc, bic)[:200]:
                        entries.append({
                            "family_name": _name(s_.Family),
                            "type_name": _name(s_),
                            "placement": _placement_type_text(s_),
                        })
                    if entries:
                        out[key] = entries
                return out

            def stairs_types():
                out = []
                for t in DB.FilteredElementCollector(doc).OfClass(DB.Architecture.StairsType).ToElements():
                    entry = {"type_name": _name(t), "id": get_element_id_text(t.Id)}
                    for key, attr in (("max_riser_mm", "MaxRiserHeight"), ("min_tread_mm", "MinTreadDepth"),
                                      ("min_run_width_mm", "MinRunWidth")):
                        try:
                            entry[key] = _ft_to_mm(getattr(t, attr))
                        except Exception:
                            pass
                    out.append(entry)
                return out

            def room_list():
                out = []
                for room in _all_rooms(doc)[:300]:
                    out.append(_room_summary(doc, room))
                return out

            return sanitize_for_json({
                "status": "success",
                "units": "mm",
                "levels": levels,
                "structural_column_types": symbols(DB.BuiltInCategory.OST_StructuralColumns),
                "architectural_column_types": symbols(DB.BuiltInCategory.OST_Columns),
                "beam_types": symbols(DB.BuiltInCategory.OST_StructuralFraming),
                "wall_types": system_types(DB.WallType),
                "floor_types": system_types(DB.FloorType),
                "door_types": symbols(DB.BuiltInCategory.OST_Doors),
                "window_types": symbols(DB.BuiltInCategory.OST_Windows),
                "pipe_types": system_types(DB.Plumbing.PipeType),
                "piping_system_types": piping_systems(),
                "pipe_fitting_types": symbols(DB.BuiltInCategory.OST_PipeFitting)[:300],
                "mep_fixture_types": fixture_types(),
                "stairs_types": stairs_types(),
                "ceiling_types": system_types(DB.CeilingType),
                "rooms": room_list(),
            })
        except Exception as e:
            route_logger.error("Error in /modeling/types: {}".format(e), exc_info=True)
            return _error(_text(e))

    # -------------------------------------------------------------- columns
    @api.route('/modeling/create_columns', methods=['POST'])
    def handle_create_columns(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "columns")
            structural = _coerce_bool(payload.get("structural"), default=True)
            bic = DB.BuiltInCategory.OST_StructuralColumns if structural else DB.BuiltInCategory.OST_Columns
            structural_type = DB.Structure.StructuralType.Column if structural else DB.Structure.StructuralType.NonStructural
            category_label = u"structural column" if structural else u"architectural column"

            def create_one(index, item, is_dry_run):
                point_raw = item.get("point", item) if isinstance(item, dict) else item
                base_level = _find_level(doc, _merged(payload, item, "level_name"))
                top_level_name = _merged(payload, item, "top_level_name")
                top_level = _find_level(doc, top_level_name, "top_level_name") if top_level_name else None
                height_mm = _to_float(_merged(payload, item, "height_mm"), "height_mm")
                if top_level is None and height_mm is None:
                    raise ValueError(u"Give either 'top_level_name' or 'height_mm'.")
                if top_level is not None and top_level.Elevation <= base_level.Elevation and height_mm is None:
                    raise ValueError(u"top_level_name must be above level_name.")
                base_offset_mm = _to_float(_merged(payload, item, "base_offset_mm"), "base_offset_mm", 0.0)
                top_offset_mm = _to_float(_merged(payload, item, "top_offset_mm"), "top_offset_mm", 0.0)
                rotation_deg = _to_float(_merged(payload, item, "rotation_deg"), "rotation_deg", 0.0)
                symbol = _find_family_symbol(
                    doc, bic,
                    _merged(payload, item, "type_name"),
                    _merged(payload, item, "family_name"),
                    category_label,
                )
                point = _point_from(point_raw, u"columns[{}]".format(index), base_level.Elevation)

                summary = {
                    "index": index,
                    "type": _symbol_label(symbol),
                    "level": _name(base_level),
                    "x_mm": _ft_to_mm(point.X),
                    "y_mm": _ft_to_mm(point.Y),
                }
                if is_dry_run:
                    return summary

                _ensure_symbol_active(doc, symbol)
                instance = doc.Create.NewFamilyInstance(point, symbol, base_level, structural_type)

                _set_param(instance, DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, base_level.Id)
                _set_param(instance, DB.BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, _mm_to_ft(base_offset_mm))
                if top_level is not None:
                    _set_param(instance, DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, top_level.Id)
                    _set_param(instance, DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, _mm_to_ft(top_offset_mm))
                else:
                    # Fixed height: top = base level + height (+ base offset).
                    _set_param(instance, DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, base_level.Id)
                    _set_param(instance, DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM,
                               _mm_to_ft(base_offset_mm + height_mm))

                if abs(rotation_deg) > 1e-9:
                    axis = DB.Line.CreateBound(point, DB.XYZ(point.X, point.Y, point.Z + 1.0))
                    DB.ElementTransformUtils.RotateElement(doc, instance.Id, axis, math.radians(rotation_deg))

                summary["element_id"] = get_element_id_text(instance.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Columns", items, create_one, dry_run)
            return _build_response("columns", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_columns: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ---------------------------------------------------------------- walls
    @api.route('/modeling/create_walls', methods=['POST'])
    def handle_create_walls(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "walls")

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"walls[{}] must be an object with start and end.".format(index))
                base_level = _find_level(doc, _merged(payload, item, "level_name"))
                top_level_name = _merged(payload, item, "top_level_name")
                top_level = _find_level(doc, top_level_name, "top_level_name") if top_level_name else None
                height_mm = _to_float(_merged(payload, item, "height_mm"), "height_mm")
                if top_level is None and height_mm is None:
                    raise ValueError(u"Give either 'top_level_name' or 'height_mm'.")
                base_offset_mm = _to_float(_merged(payload, item, "base_offset_mm"), "base_offset_mm", 0.0)
                top_offset_mm = _to_float(_merged(payload, item, "top_offset_mm"), "top_offset_mm", 0.0)
                structural = _coerce_bool(_merged(payload, item, "structural"), default=True)
                wall_type = _find_system_type(doc, DB.WallType, _merged(payload, item, "type_name"), u"wall")

                start = _point_from(item.get("start"), u"walls[{}].start".format(index), base_level.Elevation)
                end = _point_from(item.get("end"), u"walls[{}].end".format(index), base_level.Elevation)
                if start.DistanceTo(end) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                    raise ValueError(u"walls[{}] start and end are the same point.".format(index))

                if top_level is not None:
                    unconnected_ft = max(top_level.Elevation - base_level.Elevation, 1.0 / MM_PER_FOOT)
                else:
                    unconnected_ft = _mm_to_ft(height_mm)

                summary = {
                    "index": index,
                    "type": _name(wall_type),
                    "level": _name(base_level),
                    "length_mm": _ft_to_mm(start.DistanceTo(end)),
                }

                trim = _coerce_bool(_merged(payload, item, "trim_at_columns"), default=False)
                if trim:
                    z_mid = base_level.Elevation + _mm_to_ft(base_offset_mm) + unconnected_ft / 2.0
                    segments, column_ids = _wall_pieces_between_columns(doc, start, end, z_mid)
                    if not segments:
                        raise ValueError(u"walls[{}] lies entirely inside columns.".format(index))
                    summary["trimmed_at_column_ids"] = column_ids
                    summary["piece_lengths_mm"] = [_ft_to_mm(p0.DistanceTo(p1)) for p0, p1 in segments]
                else:
                    segments = [(start, end)]

                trim_top = _coerce_bool(_merged(payload, item, "trim_top_to_structure"), default=False)
                base_z = base_level.Elevation + _mm_to_ft(base_offset_mm)
                if top_level is not None:
                    top_z = top_level.Elevation + _mm_to_ft(top_offset_mm)
                else:
                    top_z = base_z + unconnected_ft
                if top_z <= base_z:
                    raise ValueError(u"walls[{}]: the wall top is not above its base.".format(index))

                # Build a plan of what to create:
                #   ("line", p0, p1, top_abs_z)  -> normal wall; top constrained to top_level with an offset
                #   ("profile", curves, None, None) -> edited-profile wall (stepped top)
                plans = []
                cut_by = []
                piece_info = []
                for p0, p1 in segments:
                    if not trim_top:
                        plans.append(("line", p0, p1, top_z))
                        continue
                    profiles, cut_ids, debug = _wall_profiles_under_structure(doc, p0, p1, base_z, top_z)
                    cut_by.extend(i for i in cut_ids if i not in cut_by)
                    if not profiles:
                        plans.append(("line", p0, p1, top_z))
                        piece_info.append({"kind": "full_height", "top_above_base_mm": _ft_to_mm(top_z - base_z),
                                           "debug": debug})
                        continue
                    direction = (DB.XYZ(p1.X, p1.Y, 0) - DB.XYZ(p0.X, p0.Y, 0)).Normalize()
                    origin = DB.XYZ(p0.X, p0.Y, base_z)
                    for profile in profiles:
                        is_rect, z_min, z_max, t_min, t_max = _profile_shape(profile, origin, direction)
                        if is_rect:
                            q0 = p0 + direction * t_min
                            q1 = p0 + direction * t_max
                            plans.append(("line", DB.XYZ(q0.X, q0.Y, p0.Z), DB.XYZ(q1.X, q1.Y, p0.Z), z_max))
                            piece_info.append({"kind": "constant_top",
                                               "top_above_base_mm": _ft_to_mm(z_max - base_z),
                                               "length_mm": _ft_to_mm(t_max - t_min),
                                               "debug": debug})
                        else:
                            plans.append(("profile", profile, None, z_max))
                            piece_info.append({"kind": "stepped_profile",
                                               "max_top_above_base_mm": _ft_to_mm(z_max - base_z),
                                               "edge_count": len(profile),
                                               "debug": debug})
                if trim_top:
                    summary["top_cut_by_element_ids"] = cut_by
                    summary["pieces"] = piece_info

                if is_dry_run:
                    return summary

                wall_ids = []
                created_tops = []
                for kind, a_, b_, top_abs in plans:
                    if kind == "profile":
                        curve_list = List[DB.Curve]()
                        for curve in a_:
                            curve_list.Add(curve)
                        wall = DB.Wall.Create(doc, curve_list, wall_type.Id, base_level.Id, structural)
                    else:
                        line = DB.Line.CreateBound(a_, b_)
                        height_ft = max(top_abs - base_z, _mm_to_ft(1.0))
                        wall = DB.Wall.Create(doc, line, wall_type.Id, base_level.Id, height_ft,
                                              _mm_to_ft(base_offset_mm), False, structural)
                        if top_level is not None:
                            _set_param(wall, DB.BuiltInParameter.WALL_HEIGHT_TYPE, top_level.Id)
                            _set_param(wall, DB.BuiltInParameter.WALL_TOP_OFFSET, top_abs - top_level.Elevation)
                    wall_ids.append(get_element_id_text(wall.Id))
                    created_tops.append((wall, top_abs))

                if trim_top:
                    doc.Regenerate()
                    checks = []
                    for wall, expected_top in created_tops:
                        try:
                            box = wall.get_BoundingBox(None)
                            actual = box.Max.Z if box is not None else None
                        except Exception:
                            actual = None
                        checks.append({
                            "element_id": get_element_id_text(wall.Id),
                            "expected_top_mm": _ft_to_mm(expected_top),
                            "actual_top_mm": _ft_to_mm(actual) if actual is not None else None,
                        })
                    summary["top_check"] = checks

                if len(wall_ids) == 1:
                    summary["element_id"] = wall_ids[0]
                else:
                    summary["element_ids"] = wall_ids
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Walls", items, create_one, dry_run)
            return _build_response("walls", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_walls: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ---------------------------------------------------------------- beams
    @api.route('/modeling/create_beams', methods=['POST'])
    def handle_create_beams(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "beams")

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"beams[{}] must be an object with start and end.".format(index))
                level = _find_level(doc, _merged(payload, item, "level_name"))
                offset_mm = _to_float(_merged(payload, item, "offset_mm"), "offset_mm", 0.0)
                symbol = _find_family_symbol(
                    doc, DB.BuiltInCategory.OST_StructuralFraming,
                    _merged(payload, item, "type_name"),
                    _merged(payload, item, "family_name"),
                    u"beam",
                )
                z_ft = level.Elevation + _mm_to_ft(offset_mm)
                start = _point_from(item.get("start"), u"beams[{}].start".format(index), z_ft)
                end = _point_from(item.get("end"), u"beams[{}].end".format(index), z_ft)
                if start.DistanceTo(end) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                    raise ValueError(u"beams[{}] start and end are the same point.".format(index))

                summary = {
                    "index": index,
                    "type": _symbol_label(symbol),
                    "level": _name(level),
                    "length_mm": _ft_to_mm(start.DistanceTo(end)),
                    "top_offset_from_level_mm": offset_mm,
                }
                if is_dry_run:
                    return summary

                _ensure_symbol_active(doc, symbol)
                line = DB.Line.CreateBound(start, end)
                beam = doc.Create.NewFamilyInstance(line, symbol, level, DB.Structure.StructuralType.Beam)
                summary["element_id"] = get_element_id_text(beam.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Beams", items, create_one, dry_run)
            return _build_response("beams", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_beams: {}".format(e), exc_info=True)
            return _error(_text(e))

    # --------------------------------------------------------------- floors
    @api.route('/modeling/create_floors', methods=['POST'])
    def handle_create_floors(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "floors")
            has_new_api = hasattr(DB.Floor, "Create")  # Revit 2022+

            def loop_points(raw_points, label, z_ft):
                if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 3:
                    raise ValueError(u"{} needs at least 3 points.".format(label))
                pts = [_point_from(p, u"{}[{}]".format(label, i), z_ft) for i, p in enumerate(raw_points)]
                if pts[0].DistanceTo(pts[-1]) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                    pts = pts[:-1]  # closing point repeated; drop it
                if len(pts) < 3:
                    raise ValueError(u"{} needs at least 3 distinct points.".format(label))
                lines = []
                for i in range(len(pts)):
                    a, b = pts[i], pts[(i + 1) % len(pts)]
                    if a.DistanceTo(b) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                        raise ValueError(u"{} has two consecutive identical points (#{}).".format(label, i))
                    lines.append(DB.Line.CreateBound(a, b))
                return lines

            def area_m2(raw_points):
                pts = []
                for p in raw_points:
                    if isinstance(p, dict):
                        pts.append((float(p.get("x_mm", p.get("x"))), float(p.get("y_mm", p.get("y")))))
                    else:
                        pts.append((float(p[0]), float(p[1])))
                s = 0.0
                for i in range(len(pts)):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % len(pts)]
                    s += x1 * y2 - x2 * y1
                return round(abs(s) / 2.0 / 1e6, 3)

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"floors[{}] must be an object with a boundary.".format(index))
                level = _find_level(doc, _merged(payload, item, "level_name"))
                offset_mm = _to_float(_merged(payload, item, "offset_mm"), "offset_mm", 0.0)
                structural = _coerce_bool(_merged(payload, item, "structural"), default=True)
                floor_type = _find_system_type(doc, DB.FloorType, _merged(payload, item, "type_name"), u"floor")

                boundary_raw = item.get("boundary")
                outer = loop_points(boundary_raw, u"floors[{}].boundary".format(index), level.Elevation)
                openings_raw = item.get("openings") or []
                openings = [loop_points(o, u"floors[{}].openings[{}]".format(index, j), level.Elevation)
                            for j, o in enumerate(openings_raw)]
                if openings and not has_new_api:
                    raise ValueError(u"Openings need Revit 2022 or later; create the floor without openings.")

                summary = {
                    "index": index,
                    "type": _name(floor_type),
                    "level": _name(level),
                    "boundary_area_m2": area_m2(boundary_raw),
                    "opening_count": len(openings),
                }

                trim = _coerce_bool(_merged(payload, item, "trim_to_structure"), default=False)
                if trim:
                    if not has_new_api:
                        raise ValueError(u"trim_to_structure needs Revit 2022 or later.")
                    pieces, trim_info = _trimmed_floor_loops(doc, outer, openings, level, offset_mm)
                    summary.update(trim_info)
                    summary["trimmed_area_m2"] = round(sum(area for _, area in pieces), 3)
                    summary["piece_count"] = len(pieces)
                    if is_dry_run:
                        return summary

                    floor_ids = []
                    for piece_loops, _area in pieces:
                        floor = DB.Floor.Create(doc, piece_loops, floor_type.Id, level.Id)
                        if structural:
                            _set_param(floor, DB.BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL, 1)
                        if abs(offset_mm) > 1e-9:
                            _set_param(floor, DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM, _mm_to_ft(offset_mm))
                        floor_ids.append(get_element_id_text(floor.Id))
                    if len(floor_ids) == 1:
                        summary["element_id"] = floor_ids[0]
                    else:
                        summary["element_ids"] = floor_ids
                    return summary

                if is_dry_run:
                    return summary

                if has_new_api:
                    loops = List[DB.CurveLoop]()
                    for curve_list in [outer] + openings:
                        loop = DB.CurveLoop()
                        for curve in curve_list:
                            loop.Append(curve)
                        loops.Add(loop)
                    floor = DB.Floor.Create(doc, loops, floor_type.Id, level.Id)
                    if structural:
                        _set_param(floor, DB.BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL, 1)
                else:
                    curve_array = DB.CurveArray()
                    for curve in outer:
                        curve_array.Append(curve)
                    floor = doc.Create.NewFloor(curve_array, floor_type, level, structural)

                if abs(offset_mm) > 1e-9:
                    _set_param(floor, DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM, _mm_to_ft(offset_mm))

                summary["element_id"] = get_element_id_text(floor.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Floors", items, create_one, dry_run)
            return _build_response("floors", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_floors: {}".format(e), exc_info=True)
            return _error(_text(e))


    # ------------------------------------------------------ doors / windows
    def _create_hosted_openings(doc, request, list_key, bic, kind_label, default_sill_mm, transaction_name):
        payload, items, dry_run = _prepare(request, list_key)

        def create_one(index, item, is_dry_run):
            if not isinstance(item, dict):
                raise ValueError(u"{}[{}] must be an object.".format(list_key, index))
            label = u"{}[{}]".format(list_key, index)
            sill_mm = _to_float(_merged(payload, item, "sill_height_mm"), "sill_height_mm", default_sill_mm)
            symbol = _find_family_symbol(
                doc, bic,
                _merged(payload, item, "type_name"),
                _merged(payload, item, "family_name"),
                kind_label,
            )
            wall, location, level, along_mm, snap_mm = _resolve_wall_host(
                doc, payload, item, label, (sill_mm or 0.0) + 1.0)

            summary = {
                "index": index,
                "type": _symbol_label(symbol),
                "level": _name(level),
                "host_wall_id": get_element_id_text(wall.Id),
                "distance_along_wall_mm": along_mm,
                "snap_distance_mm": snap_mm,
            }
            if sill_mm is not None:
                summary["sill_height_mm"] = sill_mm
            if is_dry_run:
                return summary

            _ensure_symbol_active(doc, symbol)
            instance = doc.Create.NewFamilyInstance(
                location, symbol, wall, level, DB.Structure.StructuralType.NonStructural)
            if sill_mm is not None:
                _set_param(instance, DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM, _mm_to_ft(sill_mm))
            flipped = _apply_flips(instance, item)
            if flipped:
                summary["flipped"] = flipped
            summary["element_id"] = get_element_id_text(instance.Id)
            return summary

        created, failed, warnings, outcome = _run_creation(
            doc, transaction_name, items, create_one, dry_run)
        return _build_response(list_key, created, failed, warnings, outcome, dry_run)

    @api.route('/modeling/create_doors', methods=['POST'])
    def handle_create_doors(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            return _create_hosted_openings(doc, request, "doors", DB.BuiltInCategory.OST_Doors,
                                           u"door", None, "RevitMCP Create Doors")
        except Exception as e:
            route_logger.error("Error in /modeling/create_doors: {}".format(e), exc_info=True)
            return _error(_text(e))

    @api.route('/modeling/create_windows', methods=['POST'])
    def handle_create_windows(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            return _create_hosted_openings(doc, request, "windows", DB.BuiltInCategory.OST_Windows,
                                           u"window", None, "RevitMCP Create Windows")
        except Exception as e:
            route_logger.error("Error in /modeling/create_windows: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ---------------------------------------------------------------- pipes
    @api.route('/modeling/create_pipes', methods=['POST'])
    def handle_create_pipes(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "pipes")
            connect_default = _coerce_bool(payload.get("connect_fittings"), default=True)

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"pipes[{}] must be an object.".format(index))
                label = u"pipes[{}]".format(index)
                level = _find_level(doc, _merged(payload, item, "level_name"))
                offset_mm = _to_float(_merged(payload, item, "offset_mm"), "offset_mm", 0.0)
                diameter_mm = _to_float(_merged(payload, item, "diameter_mm"), "diameter_mm")
                pipe_type = _find_system_type(doc, DB.Plumbing.PipeType, _merged(payload, item, "type_name"), u"pipe")
                system_type = _find_system_type(doc, DB.Plumbing.PipingSystemType,
                                                _merged(payload, item, "system_type_name"), u"piping system")
                connect = _coerce_bool(item.get("connect_fittings"), default=connect_default)

                raw_points = item.get("points")
                if raw_points is None and item.get("start") is not None:
                    raw_points = [item.get("start"), item.get("end")]
                if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 2:
                    raise ValueError(u"{} needs 'points' (2 or more) or 'start' and 'end'.".format(label))
                points = [_point3_from(p, u"{}.points[{}]".format(label, i), level.Elevation, offset_mm)
                          for i, p in enumerate(raw_points)]
                for i in range(len(points) - 1):
                    if points[i].DistanceTo(points[i + 1]) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                        raise ValueError(u"{} has two consecutive identical points (#{}).".format(label, i))
                points = _drop_collinear(points)

                total_ft = sum(points[i].DistanceTo(points[i + 1]) for i in range(len(points) - 1))
                summary = {
                    "index": index,
                    "pipe_type": _name(pipe_type),
                    "system_type": _name(system_type),
                    "level": _name(level),
                    "segment_count": len(points) - 1,
                    "total_length_mm": _ft_to_mm(total_ft),
                }
                if diameter_mm is not None:
                    summary["diameter_mm"] = diameter_mm
                if is_dry_run:
                    return summary

                pipes = []
                for i in range(len(points) - 1):
                    pipe = DB.Plumbing.Pipe.Create(doc, system_type.Id, pipe_type.Id, level.Id,
                                                   points[i], points[i + 1])
                    if diameter_mm is not None:
                        _set_param(pipe, DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM, _mm_to_ft(diameter_mm))
                    pipes.append(pipe)

                fitting_ids = []
                fitting_errors = []
                if connect and len(pipes) > 1:
                    doc.Regenerate()
                    for i in range(len(pipes) - 1):
                        joint = points[i + 1]
                        c1 = _connector_near(pipes[i], joint)
                        c2 = _connector_near(pipes[i + 1], joint)
                        if c1 is None or c2 is None:
                            fitting_errors.append(u"joint {}: connectors not found".format(i + 1))
                            continue
                        try:
                            fitting = doc.Create.NewElbowFitting(c1, c2)
                            fitting_ids.append(get_element_id_text(fitting.Id))
                        except Exception as fitting_error:
                            fitting_errors.append(u"joint {}: {}".format(i + 1, _text(fitting_error)))

                summary["pipe_ids"] = [get_element_id_text(p.Id) for p in pipes]
                summary["fitting_ids"] = fitting_ids
                if fitting_errors:
                    summary["fitting_errors"] = fitting_errors
                    summary["fitting_hint"] = (u"Check the pipe type's Routing Preferences has an elbow "
                                               u"for this size; pipes were created without those fittings.")
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Pipes", items, create_one, dry_run)
            return _build_response("pipe runs", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_pipes: {}".format(e), exc_info=True)
            return _error(_text(e))

    # -------------------------------------------------- MEP fixtures/equipment
    @api.route('/modeling/create_mep_fixtures', methods=['POST'])
    def handle_create_mep_fixtures(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "fixtures")

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"fixtures[{}] must be an object.".format(index))
                label = u"fixtures[{}]".format(index)
                bic = _fixture_category(_merged(payload, item, "category"))
                symbol = _find_family_symbol(
                    doc, bic,
                    _merged(payload, item, "type_name"),
                    _merged(payload, item, "family_name"),
                    _text(_merged(payload, item, "category")),
                )
                offset_mm = _to_float(_merged(payload, item, "offset_mm"), "offset_mm", 0.0)
                rotation_deg = _to_float(_merged(payload, item, "rotation_deg"), "rotation_deg", 0.0)
                placement = _placement_type_text(symbol)
                wall_hosted = placement == u"OneLevelBasedHosted"

                face_host = None
                host_mode = _merged(payload, item, "host")
                height_ref = _text(_merged(payload, item, "height_reference") or u"insertion").strip().lower()
                if host_mode is not None and _text(host_mode).strip().lower() == u"wall_face":
                    if placement != u"WorkPlaneBased":
                        raise ValueError(u"{}: host 'wall_face' needs a work-plane based family; this one is {}.".format(
                            label, placement))
                    level = _find_level(doc, _merged(payload, item, "level_name"))
                    point = _point_from(item.get("point", item), label, level.Elevation)
                    tolerance_mm = _to_float(_merged(payload, item, "snap_tolerance_mm"), "snap_tolerance_mm",
                                             DEFAULT_SNAP_TOLERANCE_MM)
                    face_host = _wall_face_host(doc, point, level.Elevation + _mm_to_ft(offset_mm),
                                                _mm_to_ft(tolerance_mm), label)
                    location = face_host["location"]
                    wall = None
                elif wall_hosted:
                    wall, location, level, along_mm, snap_mm = _resolve_wall_host(
                        doc, payload, item, label, offset_mm + 1.0)
                else:
                    level = _find_level(doc, _merged(payload, item, "level_name"))
                    location = _point_from(item.get("point", item), label, level.Elevation)
                    wall = None

                summary = {
                    "index": index,
                    "type": _symbol_label(symbol),
                    "placement": placement,
                    "level": _name(level),
                    "x_mm": _ft_to_mm(location.X),
                    "y_mm": _ft_to_mm(location.Y),
                    "offset_mm": offset_mm,
                }
                if wall is not None:
                    summary["host_wall_id"] = get_element_id_text(wall.Id)
                    summary["snap_distance_mm"] = snap_mm
                if face_host is not None:
                    summary["wall_face"] = face_host["info"]
                    summary["height_reference"] = height_ref
                if is_dry_run:
                    return summary

                _ensure_symbol_active(doc, symbol)
                if face_host is not None:
                    instance = doc.Create.NewFamilyInstance(
                        face_host["reference"], location, face_host["ref_dir"], symbol)
                    summary["placed_by"] = "wall_face"
                    doc.Regenerate()
                    # Keep the fixture upright on the vertical face (family +Y = world up).
                    try:
                        if instance.GetTransform().BasisY.Z < -0.5:
                            axis = DB.Line.CreateBound(location, location.Add(face_host["normal"]))
                            DB.ElementTransformUtils.RotateElement(doc, instance.Id, axis, math.pi)
                            doc.Regenerate()
                            summary["turned_upright"] = True
                    except Exception:
                        summary["upright_warning"] = u"Could not check the fixture is upright; verify in 3D."
                    if height_ref == u"top":
                        box = instance.get_BoundingBox(None)
                        if box is not None:
                            dz = location.Z - box.Max.Z
                            if abs(dz) > 1e-6:
                                DB.ElementTransformUtils.MoveElement(doc, instance.Id, DB.XYZ(0.0, 0.0, dz))
                                doc.Regenerate()
                    box = instance.get_BoundingBox(None)
                    if box is not None:
                        summary["bbox_top_mm"] = _ft_to_mm(box.Max.Z - level.Elevation)
                        summary["bbox_bottom_mm"] = _ft_to_mm(box.Min.Z - level.Elevation)
                elif wall is not None:
                    instance = doc.Create.NewFamilyInstance(
                        location, symbol, wall, level, DB.Structure.StructuralType.NonStructural)
                    summary["placed_by"] = "wall_host"
                elif placement == u"WorkPlaneBased":
                    # Work-plane based families ignore the level in the (XYZ, symbol, level) overload
                    # and land on the project origin plane, so host them on the level's plane directly.
                    instance = doc.Create.NewFamilyInstance(
                        level.GetPlaneReference(), location, DB.XYZ.BasisX, symbol)
                    summary["placed_by"] = "level_work_plane"
                    # The level's plane reference can face down, which hosts the family upside-down
                    # (floor drain outlet pointing up). Flip the work plane back so the family's +Z is up.
                    doc.Regenerate()
                    try:
                        if instance.GetTransform().BasisZ.Z < 0 and instance.CanFlipWorkPlane:
                            instance.IsWorkPlaneFlipped = not instance.IsWorkPlaneFlipped
                            summary["work_plane_flipped"] = True
                    except Exception:
                        summary["flip_warning"] = u"Could not check/flip the work plane; verify the family is not upside-down."
                else:
                    # The (XYZ, symbol, level) overload treats the point's Z as an offset from the level,
                    # so pass Z = 0 here; passing the level elevation would double it.
                    level_point = DB.XYZ(location.X, location.Y, 0.0)
                    try:
                        instance = doc.Create.NewFamilyInstance(
                            level_point, symbol, level, DB.Structure.StructuralType.NonStructural)
                        summary["placed_by"] = "level"
                    except Exception as level_error:
                        # Face/work-plane based families: host on the level's plane instead.
                        try:
                            instance = doc.Create.NewFamilyInstance(
                                level.GetPlaneReference(), location, DB.XYZ.BasisX, symbol)
                            summary["placed_by"] = "level_work_plane"
                            summary["note"] = (u"Face-based family placed on the level plane (facing up). "
                                               u"Ceiling-mounted families may need a non-hosted version.")
                        except Exception:
                            raise ValueError(u"{}: could not place this family ({}): {}".format(
                                label, placement, _text(level_error)))

                if abs(offset_mm) > 1e-9 and face_host is None:
                    used = _set_first_param(instance, OFFSET_PARAM_NAMES, _mm_to_ft(offset_mm))
                    if used is None:
                        summary["offset_warning"] = u"Could not set the height offset on this family."

                if abs(rotation_deg) > 1e-9 and wall is None and face_host is None:
                    # Rotate about the requested insertion point. Reading instance.Location.Point before
                    # the document regenerates can return the origin, which swung fixtures around (0,0).
                    pt = location
                    axis = DB.Line.CreateBound(DB.XYZ(pt.X, pt.Y, 0.0), DB.XYZ(pt.X, pt.Y, 1.0))
                    DB.ElementTransformUtils.RotateElement(doc, instance.Id, axis, math.radians(rotation_deg))

                flipped = _apply_flips(instance, item)
                if flipped:
                    summary["flipped"] = flipped
                summary["element_id"] = get_element_id_text(instance.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create MEP Fixtures", items, create_one, dry_run)
            return _build_response("fixtures", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_mep_fixtures: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ------------------------------------------------------- join priority
    @api.route('/modeling/apply_join_priority', methods=['POST'])
    def handle_apply_join_priority(doc, request):
        """Join overlapping columns/beams/floors/walls so that column > beam > floor > wall cuts."""
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload = request.data if hasattr(request, "data") else {}
            if payload is None or not isinstance(payload, dict):
                raise ValueError(u"Invalid JSON payload.")
            dry_run = _coerce_bool(payload.get("dry_run"), default=False)
            try:
                max_pairs = max(1, min(5000, int(payload.get("max_pairs", 1000))))
            except Exception:
                max_pairs = 1000

            prio_map = _join_priority_map()
            cats = List[DB.BuiltInCategory]()
            for ost, _p, _l in _JOIN_PRIORITY:
                bic = getattr(DB.BuiltInCategory, ost, None)
                if bic is not None:
                    cats.Add(bic)
            collector = (DB.FilteredElementCollector(doc)
                         .WhereElementIsNotElementType()
                         .WherePasses(DB.ElementMulticategoryFilter(cats)))

            raw_ids = payload.get("element_ids")
            scope_text = u"whole model"
            if raw_ids:
                wanted = set(int(_text(i)) for i in raw_ids)
                candidates = [e for e in collector.ToElements() if get_element_id_value(e.Id) in wanted]
                scope_text = u"{} given elements".format(len(candidates))
            elif payload.get("level_name"):
                base = _find_level(doc, payload.get("level_name"))
                if payload.get("top_level_name"):
                    top = _find_level(doc, payload.get("top_level_name"), "top_level_name")
                    top_z = top.Elevation
                else:
                    above = [lv for lv in _all_levels(doc) if lv.Elevation > base.Elevation + 1e-6]
                    top_z = above[0].Elevation if above else base.Elevation + _mm_to_ft(5000)
                big = 1.0e6
                outline = DB.Outline(DB.XYZ(-big, -big, base.Elevation + _mm_to_ft(1.0)),
                                     DB.XYZ(big, big, top_z - _mm_to_ft(1.0)))
                candidates = list(collector.WherePasses(DB.BoundingBoxIntersectsFilter(outline)).ToElements())
                scope_text = u"elements between {} and {:.0f} mm".format(_name(base), _ft_to_mm(top_z))
            else:
                candidates = list(collector.ToElements())

            candidates = [e for e in candidates if _priority_of(e, prio_map)]
            id_list = List[DB.ElementId]()
            for e in candidates:
                id_list.Add(e.Id)

            pairs = []
            seen = set()
            if id_list.Count > 1:
                for a in candidates:
                    pa = _priority_of(a, prio_map)
                    try:
                        hits = (DB.FilteredElementCollector(doc, id_list)
                                .WherePasses(DB.ElementIntersectsElementFilter(a))
                                .ToElements())
                    except Exception:
                        continue
                    for b in hits:
                        if get_element_id_value(b.Id) == get_element_id_value(a.Id):
                            continue
                        pb = _priority_of(b, prio_map)
                        if pb is None or pb[0] == pa[0]:
                            continue
                        key = tuple(sorted((get_element_id_value(a.Id), get_element_id_value(b.Id))))
                        if key in seen:
                            continue
                        seen.add(key)
                        high, low = (a, b) if pa[0] > pb[0] else (b, a)
                        pairs.append((high, low))
                        if len(pairs) > max_pairs:
                            raise ValueError(u"More than {} overlapping pairs; narrow the scope "
                                             u"(level_name / element_ids) or raise max_pairs.".format(max_pairs))

            def describe(high, low):
                return u"{} {} > {} {}".format(
                    _priority_of(high, prio_map)[1], get_element_id_text(high.Id),
                    _priority_of(low, prio_map)[1], get_element_id_text(low.Id))

            state_counts = {"not_joined": 0, "joined_wrong_order": 0, "already_correct": 0}
            for high, low in pairs:
                if not DB.JoinGeometryUtils.AreElementsJoined(doc, high, low):
                    state_counts["not_joined"] += 1
                elif DB.JoinGeometryUtils.IsCuttingElementInJoin(doc, high, low):
                    state_counts["already_correct"] += 1
                else:
                    state_counts["joined_wrong_order"] += 1

            result = {
                "scope": scope_text,
                "candidate_count": len(candidates),
                "overlapping_pair_count": len(pairs),
                "before": state_counts,
            }
            if dry_run:
                result["status"] = "dry_run"
                result["message"] = u"Dry run: {} overlapping pairs found.".format(len(pairs))
                result["sample_pairs"] = [describe(h, l) for h, l in pairs[:30]]
                return sanitize_for_json(result)

            collector_warnings = _WarningCollector()
            transaction = DB.Transaction(doc, "RevitMCP Apply Join Priority")
            options = transaction.GetFailureHandlingOptions()
            options.SetFailuresPreprocessor(collector_warnings)
            transaction.SetFailureHandlingOptions(options)
            transaction.Start()
            joined = switched = unchanged = 0
            failed = []
            try:
                for high, low in pairs:
                    sub = DB.SubTransaction(doc)
                    sub.Start()
                    try:
                        if not DB.JoinGeometryUtils.AreElementsJoined(doc, high, low):
                            DB.JoinGeometryUtils.JoinGeometry(doc, high, low)
                            joined += 1
                        else:
                            unchanged += 1
                        if not DB.JoinGeometryUtils.IsCuttingElementInJoin(doc, high, low):
                            DB.JoinGeometryUtils.SwitchJoinOrder(doc, high, low)
                            switched += 1
                        sub.Commit()
                    except Exception as pair_error:
                        try:
                            sub.RollBack()
                        except Exception:
                            pass
                        failed.append({"pair": describe(high, low), "error": _text(pair_error)})
                status = transaction.Commit()
            except Exception:
                if transaction.HasStarted() and not transaction.HasEnded():
                    transaction.RollBack()
                raise

            result.update({
                "status": "success" if not failed else ("partial_success" if (joined or switched) else "error"),
                "message": u"Joined {} new pairs, switched order on {}, {} failed.".format(joined, switched, len(failed)),
                "joined_new": joined,
                "switched_order": switched,
                "already_joined": unchanged,
                "failed": failed[:50],
                "warnings": collector_warnings.warnings[:50],
                "transaction": _text(status),
                "undo_hint": u"One Ctrl+Z in Revit undoes this whole call.",
            })
            return sanitize_for_json(result)
        except Exception as e:
            route_logger.error("Error in /modeling/apply_join_priority: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ------------------------------------------------------------- ceilings
    @api.route('/modeling/create_ceilings', methods=['POST'])
    def handle_create_ceilings(doc, request):
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            if not hasattr(DB.Ceiling, "Create"):
                raise ValueError(u"Creating ceilings needs Revit 2022 or later.")
            payload = request.data if hasattr(request, "data") else {}
            if payload is None or not isinstance(payload, dict):
                raise ValueError(u"Invalid JSON payload.")

            # all_rooms_on_level expands into one item per room.
            if _coerce_bool(payload.get("all_rooms_on_level"), default=False):
                level = _find_level(doc, payload.get("level_name"))
                payload = dict(payload)
                payload["ceilings"] = [{"room_id": get_element_id_text(r.Id)} for r in _all_rooms(doc)
                                       if get_element_id_value(r.LevelId) == get_element_id_value(level.Id)]
                if not payload["ceilings"]:
                    raise ValueError(u"No placed rooms on level '{}'.".format(_name(level)))

            class _Req(object):
                pass
            fake = _Req()
            fake.data = payload
            payload, items, dry_run = _prepare(fake, "ceilings")

            def rooms_matching(item, level):
                rooms = _all_rooms(doc)
                if item.get("room_id"):
                    wanted = int(_text(item.get("room_id")))
                    return [r for r in rooms if get_element_id_value(r.Id) == wanted]
                name = _text(item.get("room_name"))
                number = _text(item.get("room_number"))
                out = []
                for r in rooms:
                    if level is not None and get_element_id_value(r.LevelId) != get_element_id_value(level.Id):
                        continue
                    if name and _param_text(r, "ROOM_NAME") != name:
                        continue
                    if number and _param_text(r, "ROOM_NUMBER") != number:
                        continue
                    out.append(r)
                return out

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"ceilings[{}] must be an object.".format(index))
                label = u"ceilings[{}]".format(index)
                ceiling_type = _find_system_type(doc, DB.CeilingType, _merged(payload, item, "type_name"), u"ceiling")
                height_mm = _to_float(_merged(payload, item, "height_mm"), "height_mm", required=True)
                level_name = _merged(payload, item, "level_name")
                level = _find_level(doc, level_name) if level_name else None

                summary = {"index": index, "type": _name(ceiling_type), "height_mm": height_mm}
                loops = None
                temp_room = None
                temp_tx = None

                if item.get("boundary") is not None:
                    if level is None:
                        raise ValueError(u"{}: level_name is required with a boundary.".format(label))
                    curves = []
                    raw = item.get("boundary")
                    pts = [_point_from(p, u"{}.boundary[{}]".format(label, i), level.Elevation) for i, p in enumerate(raw)]
                    if len(pts) >= 2 and pts[0].DistanceTo(pts[-1]) * MM_PER_FOOT < MIN_CURVE_LENGTH_MM:
                        pts = pts[:-1]
                    if len(pts) < 3:
                        raise ValueError(u"{}: boundary needs at least 3 points.".format(label))
                    for i in range(len(pts)):
                        curves.append(DB.Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
                    loops = List[DB.CurveLoop]()
                    loops.Add(_curve_loop(curves, True))
                    for j, opening in enumerate(item.get("openings") or []):
                        op = [_point_from(p, u"{}.openings[{}]".format(label, j), level.Elevation) for p in opening]
                        loops.Add(_curve_loop([DB.Line.CreateBound(op[i], op[(i + 1) % len(op)])
                                               for i in range(len(op))], False))
                    summary["source"] = "boundary"
                elif item.get("room_id") or item.get("room_name") or item.get("room_number"):
                    matches = rooms_matching(item, level)
                    if len(matches) != 1:
                        raise ValueError(u"{}: {} rooms match; give room_id, or room_name/number plus level_name.".format(
                            label, len(matches)))
                    room = matches[0]
                    level = doc.GetElement(room.LevelId)
                    loops = _room_loops(room)
                    summary.update({"source": "room", "room": _room_summary(doc, room)})
                else:
                    if level is None:
                        raise ValueError(u"{}: level_name is required with a point.".format(label))
                    pt = _point_from(item.get("point", item), label, level.Elevation + _mm_to_ft(300.0))
                    room = doc.GetRoomAtPoint(pt)
                    if room is not None:
                        loops = _room_loops(room)
                        summary.update({"source": "room_at_point", "room": _room_summary(doc, room)})
                    else:
                        # No room here: place a temporary room to read the area enclosed by walls/columns.
                        if is_dry_run:
                            temp_tx = DB.Transaction(doc, "RevitMCP probe enclosed area")
                            temp_tx.Start()
                        try:
                            temp_room = doc.Create.NewRoom(level, DB.UV(pt.X, pt.Y))
                            doc.Regenerate()
                            if temp_room is None or temp_room.Area < 1e-6:
                                raise ValueError(u"{}: the point is not inside an area enclosed by walls.".format(label))
                            loops = _room_loops(temp_room)
                            summary.update({"source": "enclosed_by_walls",
                                            "enclosed_area_m2": round(temp_room.Area * SQFT_TO_M2, 3)})
                        finally:
                            if temp_tx is not None:
                                temp_tx.RollBack()
                                temp_room = None

                summary["level"] = _name(level)
                summary["loop_count"] = loops.Count
                if is_dry_run:
                    return summary

                ceiling = DB.Ceiling.Create(doc, loops, ceiling_type.Id, level.Id)
                _set_param(ceiling, DB.BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM, _mm_to_ft(height_mm))
                if temp_room is not None:
                    doc.Delete(temp_room.Id)
                summary["element_id"] = get_element_id_text(ceiling.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Ceilings", items, create_one, dry_run)
            return _build_response("ceilings", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_ceilings: {}".format(e), exc_info=True)
            return _error(_text(e))

    # --------------------------------------------------------------- stairs
    @api.route('/modeling/create_stairs', methods=['POST'])
    def handle_create_stairs(doc, request):
        """L- or U-shaped stairs from a start point, direction, turn side and width."""
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "stairs")
            created = []
            failed = []
            warnings = []

            for index, item in enumerate(items):
                label = u"stairs[{}]".format(index)
                try:
                    if not isinstance(item, dict):
                        raise ValueError(u"{} must be an object.".format(label))
                    shape = _text(_merged(payload, item, "shape")).upper()
                    if shape not in (u"L", u"U"):
                        raise ValueError(u"{}: shape must be 'L' or 'U'.".format(label))
                    turn = _text(_merged(payload, item, "turn")).lower()
                    if turn not in (u"left", u"right"):
                        raise ValueError(u"{}: turn must be 'left' or 'right'.".format(label))
                    base = _find_level(doc, _merged(payload, item, "level_name"))
                    top = _find_level(doc, _merged(payload, item, "top_level_name"), "top_level_name")
                    height = top.Elevation - base.Elevation
                    if height <= 0:
                        raise ValueError(u"{}: top level must be above the base level.".format(label))

                    stype = None
                    type_name = _merged(payload, item, "type_name")
                    stair_types = list(DB.FilteredElementCollector(doc).OfClass(DB.Architecture.StairsType).ToElements())
                    if type_name:
                        stype = _find_system_type(doc, DB.Architecture.StairsType, type_name, u"stairs")
                    elif stair_types:
                        stype = stair_types[0]

                    max_riser = _to_float(_merged(payload, item, "max_riser_mm"), "max_riser_mm")
                    max_riser = _mm_to_ft(max_riser) if max_riser else (stype.MaxRiserHeight if stype else _mm_to_ft(180))
                    tread = _to_float(_merged(payload, item, "tread_mm"), "tread_mm")
                    tread = _mm_to_ft(tread) if tread else (stype.MinTreadDepth if stype else _mm_to_ft(260))
                    width = _to_float(_merged(payload, item, "width_mm"), "width_mm")
                    width = _mm_to_ft(width) if width else (stype.MinRunWidth if stype else _mm_to_ft(1200))
                    gap = _mm_to_ft(_to_float(_merged(payload, item, "gap_mm"), "gap_mm", 0.0))
                    angle = math.radians(_to_float(_merged(payload, item, "direction_deg"), "direction_deg", 0.0))

                    risers = int(math.ceil(height / max_riser - 1e-9))
                    first = _merged(payload, item, "first_run_risers")
                    n1 = int(first) if first else int(math.ceil(risers / 2.0))
                    n2 = risers - n1
                    if n1 < 2 or n2 < 2:
                        raise ValueError(u"{}: each run needs at least 2 risers (total {}).".format(label, risers))

                    d = DB.XYZ(math.cos(angle), math.sin(angle), 0)
                    left = DB.XYZ(-d.Y, d.X, 0)
                    side = left if turn == u"left" else left.Negate()
                    start = _point_from(item.get("start", item), label + u".start", 0.0)

                    len1 = (n1 - 1) * tread + _mm_to_ft(0.5)
                    len2 = (n2 - 1) * tread + _mm_to_ft(0.5)
                    p1 = start + d * len1
                    if shape == u"U":
                        r2_start = p1 + side * (width + gap)
                        r2_dir = d.Negate()
                    else:
                        r2_start = p1 + d * (width / 2.0) + side * (width / 2.0)
                        r2_dir = side
                    r2_end = r2_start + r2_dir * len2

                    summary = {
                        "index": index,
                        "shape": shape,
                        "turn": turn,
                        "type": _name(stype) if stype else None,
                        "base_level": _name(base),
                        "top_level": _name(top),
                        "total_risers": risers,
                        "riser_height_mm": round(_ft_to_mm(height) / risers, 2),
                        "tread_mm": _ft_to_mm(tread),
                        "width_mm": _ft_to_mm(width),
                        "run_risers": [n1, n2],
                        "run1": {"start": [_ft_to_mm(start.X), _ft_to_mm(start.Y)], "end": [_ft_to_mm(p1.X), _ft_to_mm(p1.Y)]},
                        "run2": {"start": [_ft_to_mm(r2_start.X), _ft_to_mm(r2_start.Y)],
                                 "end": [_ft_to_mm(r2_end.X), _ft_to_mm(r2_end.Y)]},
                    }
                    if dry_run:
                        created.append(summary)
                        continue

                    collector_w = _WarningCollector()
                    scope = DB.StairsEditScope(doc, "RevitMCP Create Stairs")
                    stairs_id = scope.Start(base.Id, top.Id)
                    tx = DB.Transaction(doc, "RevitMCP Stairs Runs")
                    tx.Start()
                    try:
                        stairs = doc.GetElement(stairs_id)
                        if stype is not None:
                            try:
                                stairs.ChangeTypeId(stype.Id)
                            except Exception:
                                pass
                        try:
                            stairs.DesiredRisersNumber = risers
                        except Exception:
                            pass
                        try:
                            stairs.ActualTreadDepth = tread
                        except Exception:
                            pass
                        # Run location paths use absolute (model) elevations.
                        z0 = base.Elevation
                        line1 = DB.Line.CreateBound(DB.XYZ(start.X, start.Y, z0), DB.XYZ(p1.X, p1.Y, z0))
                        run1 = DB.Architecture.StairsRun.CreateStraightRun(
                            doc, stairs_id, line1, DB.Architecture.StairsRunJustification.Center)
                        run1.ActualRunWidth = width
                        # TopElevation may be reported relative to the stairs base or absolute;
                        # pick whichever matches the expected landing height.
                        expected_rel = n1 * (height / risers)
                        top_reported = run1.TopElevation
                        if abs(top_reported - expected_rel) <= abs(top_reported - (z0 + expected_rel)):
                            z1 = z0 + top_reported
                        else:
                            z1 = top_reported
                        summary["landing_elevation_mm"] = _ft_to_mm(z1)
                        line2 = DB.Line.CreateBound(DB.XYZ(r2_start.X, r2_start.Y, z1), DB.XYZ(r2_end.X, r2_end.Y, z1))
                        run2 = DB.Architecture.StairsRun.CreateStraightRun(
                            doc, stairs_id, line2, DB.Architecture.StairsRunJustification.Center)
                        run2.ActualRunWidth = width
                        landing_ids = DB.Architecture.StairsLanding.CreateAutomaticLanding(doc, run1.Id, run2.Id)
                        tx.Commit()
                    except Exception:
                        if tx.HasStarted() and not tx.HasEnded():
                            tx.RollBack()
                        scope.Cancel()
                        raise
                    scope.Commit(collector_w)
                    warnings.extend(collector_w.warnings)

                    stairs = doc.GetElement(stairs_id)
                    summary["element_id"] = get_element_id_text(stairs_id)
                    summary["landing_count"] = len(list(landing_ids)) if landing_ids is not None else 0
                    try:
                        summary["actual_risers"] = stairs.ActualRisersNumber
                        summary["desired_risers"] = stairs.DesiredRisersNumber
                        summary["actual_riser_height_mm"] = _ft_to_mm(stairs.ActualRiserHeight)
                        summary["actual_tread_mm"] = _ft_to_mm(stairs.ActualTreadDepth)
                        summary["run_actual_risers"] = [run1.ActualRisersNumber, run2.ActualRisersNumber]
                    except Exception:
                        pass
                    created.append(summary)
                except Exception as item_error:
                    failed.append({"index": index, "error": _text(item_error)})

            outcome = "dry_run" if dry_run else ("committed" if created else "rolled_back")
            response = _build_response("stairs", created, failed, warnings, outcome, dry_run)
            if outcome == "committed":
                response["undo_hint"] = u"Each stair is its own Revit edit; undo once per stair."
            return response
        except Exception as e:
            route_logger.error("Error in /modeling/create_stairs: {}".format(e), exc_info=True)
            return _error(_text(e))

    # ------------------------------------------------- floor sub-elements
    @api.route('/modeling/edit_floor_shape', methods=['POST'])
    def handle_edit_floor_shape(doc, request):
        """Shape-edit a floor: add points, split lines and set vertex offsets (drainage, ramps...)."""
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload = request.data if hasattr(request, "data") else {}
            if payload is None or not isinstance(payload, dict):
                raise ValueError(u"Invalid JSON payload.")
            floor = doc.GetElement(make_element_id(DB, payload.get("floor_id")))
            if floor is None or not isinstance(floor, DB.Floor):
                raise ValueError(u"floor_id {} is not a floor.".format(_text(payload.get("floor_id"))))
            dry_run = _coerce_bool(payload.get("dry_run"), default=False)
            reset = _coerce_bool(payload.get("reset"), default=False)
            tolerance = _mm_to_ft(_to_float(payload.get("snap_tolerance_mm"), "snap_tolerance_mm", 20.0))
            points = _items_from(payload, "points")          # [{x_mm, y_mm, offset_mm}]
            split_lines = _items_from(payload, "split_lines")  # [{start:{x_mm,y_mm,offset_mm?}, end:{...}}]

            top = _floor_top_z(floor)
            plan = {"floor_id": get_element_id_text(floor.Id), "reset": reset,
                    "point_count": len(points), "split_line_count": len(split_lines)}
            if dry_run:
                editor = _slab_editor(floor)
                plan["status"] = "dry_run"
                plan["message"] = u"Dry run: would apply {} points and {} split lines.".format(len(points), len(split_lines))
                plan["current_vertices"] = _vertex_list(editor, top)
                return sanitize_for_json(plan)

            collector_w = _WarningCollector()
            tx = DB.Transaction(doc, "RevitMCP Edit Floor Shape")
            options = tx.GetFailureHandlingOptions()
            options.SetFailuresPreprocessor(collector_w)
            tx.SetFailureHandlingOptions(options)
            tx.Start()
            done = []
            failed = []
            try:
                editor = _slab_editor(floor)
                if reset:
                    editor.ResetSlabShape()
                try:
                    editor.Enable()
                except Exception:
                    pass
                # Corner vertices only appear after a regenerate; re-read the editor afterwards.
                doc.Regenerate()
                editor = _slab_editor(floor)

                def vertex_at(raw, label):
                    p = _point_from(raw, label, top)
                    v = _find_vertex(editor, p.X, p.Y, tolerance)
                    if v is None:
                        v = _add_point(editor, p)
                        doc.Regenerate()
                        if v is None:
                            v = _find_vertex(editor, p.X, p.Y, tolerance)
                    if v is None:
                        raise ValueError(u"{}: could not create a vertex here (is it inside the floor?).".format(label))
                    offset = _to_float(raw.get("offset_mm") if isinstance(raw, dict) else None,
                                       label + u".offset_mm", None)
                    if offset is not None:
                        editor.ModifySubElement(v, _mm_to_ft(offset))
                    return v

                for i, raw in enumerate(points):
                    try:
                        vertex_at(raw, u"points[{}]".format(i))
                        done.append(u"points[{}]".format(i))
                    except Exception as point_error:
                        failed.append({"item": u"points[{}]".format(i), "error": _text(point_error)})

                for i, raw in enumerate(split_lines):
                    try:
                        if not isinstance(raw, dict):
                            raise ValueError(u"split_lines[{}] must be {{start, end}}.".format(i))
                        v0 = vertex_at(raw.get("start"), u"split_lines[{}].start".format(i))
                        v1 = vertex_at(raw.get("end"), u"split_lines[{}].end".format(i))
                        _add_split_line(editor, v0, v1)
                        done.append(u"split_lines[{}]".format(i))
                    except Exception as line_error:
                        failed.append({"item": u"split_lines[{}]".format(i), "error": _text(line_error)})

                status = tx.Commit()
            except Exception:
                if tx.HasStarted() and not tx.HasEnded():
                    tx.RollBack()
                raise

            editor = _slab_editor(floor)
            plan.update({
                "status": "success" if not failed else ("partial_success" if done else "error"),
                "message": u"Applied {} edits, {} failed.".format(len(done), len(failed)),
                "applied": done,
                "failed": failed,
                "warnings": collector_w.warnings[:50],
                "transaction": _text(status),
                "vertices_after": _vertex_list(editor, top),
                "undo_hint": u"One Ctrl+Z in Revit undoes this whole call.",
            })
            return sanitize_for_json(plan)
        except Exception as e:
            route_logger.error("Error in /modeling/edit_floor_shape: {}".format(e), exc_info=True)
            return _error(_text(e))


    # --------------------------------------------------------------- levels
    @api.route('/modeling/create_levels', methods=['POST'])
    def handle_create_levels(doc, request):
        """Create levels by absolute elevation or by height above another level, with plan views."""
        route_logger = script.get_logger()
        try:
            _check_document(doc)
            payload, items, dry_run = _prepare(request, "levels")
            make_views = _coerce_bool(payload.get("create_plan_views"), default=True)
            make_ceiling_views = _coerce_bool(payload.get("create_ceiling_plans"), default=True)

            def view_family_type(kind):
                for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType).ToElements():
                    if vft.ViewFamily == kind:
                        return vft
                return None

            floor_vft = view_family_type(DB.ViewFamily.FloorPlan)
            ceiling_vft = view_family_type(DB.ViewFamily.CeilingPlan)
            planned_names = set()
            planned = {}

            def create_one(index, item, is_dry_run):
                if not isinstance(item, dict):
                    raise ValueError(u"levels[{}] must be an object.".format(index))
                name = _text(item.get("name"))
                if not name:
                    raise ValueError(u"levels[{}]: name is required.".format(index))
                existing = [_name(lv) for lv in _all_levels(doc)]
                if name in existing or name in planned_names:
                    raise ValueError(u"levels[{}]: a level named '{}' already exists.".format(index, name))

                if item.get("elevation_mm") is not None:
                    elevation = _mm_to_ft(_to_float(item.get("elevation_mm"), "elevation_mm"))
                else:
                    ref_name = _text(item.get("above_level"))
                    height = _to_float(item.get("height_mm"), u"levels[{}].height_mm".format(index), required=True)
                    if ref_name in planned:
                        ref_elev = planned[ref_name]
                    else:
                        ref_elev = _find_level(doc, ref_name, "above_level").Elevation
                    elevation = ref_elev + _mm_to_ft(height)

                for lv in _all_levels(doc):
                    if abs(lv.Elevation - elevation) < _mm_to_ft(1.0):
                        raise ValueError(u"levels[{}]: level '{}' is already at {} mm.".format(
                            index, _name(lv), _ft_to_mm(elevation)))

                planned_names.add(name)
                planned[name] = elevation
                summary = {"index": index, "name": name, "elevation_mm": _ft_to_mm(elevation)}
                if is_dry_run:
                    return summary

                level = DB.Level.Create(doc, elevation)
                level.Name = name
                summary["element_id"] = get_element_id_text(level.Id)
                if make_views and floor_vft is not None:
                    plan = DB.ViewPlan.Create(doc, floor_vft.Id, level.Id)
                    try:
                        plan.Name = name
                    except Exception:
                        pass
                    summary["floor_plan_id"] = get_element_id_text(plan.Id)
                if make_ceiling_views and ceiling_vft is not None:
                    cplan = DB.ViewPlan.Create(doc, ceiling_vft.Id, level.Id)
                    try:
                        cplan.Name = name
                    except Exception:
                        pass
                    summary["ceiling_plan_id"] = get_element_id_text(cplan.Id)
                return summary

            created, failed, warnings, outcome = _run_creation(
                doc, "RevitMCP Create Levels", items, create_one, dry_run)
            return _build_response("levels", created, failed, warnings, outcome, dry_run)
        except Exception as e:
            route_logger.error("Error in /modeling/create_levels: {}".format(e), exc_info=True)
            return _error(_text(e))
