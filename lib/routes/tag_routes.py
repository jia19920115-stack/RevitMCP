# RevitMCP: Tag- and room-stamp HTTP routes
# -*- coding: UTF-8 -*-
#
# Drei Endpunkte:
#   POST /tags/by_id          - explizite Element-IDs in aktiver View taggen
#   POST /tags/all_in_view    - alle ungetaggten Elemente einer Kategorie taggen
#   POST /tags/rooms          - Raumstempel (RoomTag) fuer ausgewaehlte/alle Raeume
#
# Handler-Konvention: pyRevit Routes injiziert uidoc/doc/request auf dem
# UI-Thread. Schreib-Operationen in Transaction.

import System
from pyrevit import routes, script, DB
from System.Collections.Generic import List
from Autodesk.Revit.Exceptions import RegenerationFailedException

from routes.json_safety import sanitize_for_json
from routes.revit_compat import get_element_id_value as eid_int
from routes.revit_compat import make_element_id
from routes.revit_compat import TEXT_TYPES


# Kategorie -> Tag-Kategorie-Mapping (Host-Kat -> zugehoerige Tag-Kat)
HOST_TO_TAG_CATEGORY = {
    'OST_Walls':                'OST_WallTags',
    'OST_Doors':                'OST_DoorTags',
    'OST_Windows':              'OST_WindowTags',
    'OST_Floors':               'OST_FloorTags',
    'OST_Ceilings':             'OST_CeilingTags',
    'OST_Roofs':                'OST_RoofTags',
    'OST_Stairs':               'OST_StairsTags',
    'OST_StairsRailing':        'OST_StairsRailingTags',
    'OST_Rooms':                'OST_RoomTags',
    'OST_Areas':                'OST_AreaTags',
    'OST_Furniture':            'OST_FurnitureTags',
    'OST_Columns':              'OST_ColumnTags',
    'OST_StructuralColumns':    'OST_StructuralColumnTags',
    'OST_StructuralFraming':    'OST_StructuralFramingTags',
    'OST_PlumbingFixtures':     'OST_PlumbingFixtureTags',
    'OST_LightingFixtures':     'OST_LightingFixtureTags',
    'OST_GenericModel':         'OST_GenericModelTags',
}


def _get_built_in_category(name):
    """Resolve OST_* string to a BuiltInCategory enum value, or return None."""
    try:
        return getattr(DB.BuiltInCategory, name)
    except AttributeError:
        return None


def _find_default_tag_symbol(doc, host_category_name):
    """Find the first available FamilySymbol in the matching tag category."""
    tag_category_name = HOST_TO_TAG_CATEGORY.get(host_category_name)
    if not tag_category_name:
        return None, "No tag category mapped for {}".format(host_category_name)

    tag_built_in = _get_built_in_category(tag_category_name)
    if tag_built_in is None:
        return None, "Tag category {} not found in BuiltInCategory enum".format(tag_category_name)

    collector = DB.FilteredElementCollector(doc).OfCategory(tag_built_in).OfClass(DB.FamilySymbol)
    symbol = collector.FirstElement()
    if symbol is None:
        return None, "No tag family symbol loaded for category {}".format(tag_category_name)
    return symbol, None


def _get_element_center_xyz(element):
    """Best-effort center point of an element for tag placement."""
    location = getattr(element, 'Location', None)
    if location is not None:
        # LocationPoint (most family instances)
        if hasattr(location, 'Point') and location.Point is not None:
            return location.Point
        # LocationCurve (walls, lines)
        if hasattr(location, 'Curve') and location.Curve is not None:
            curve = location.Curve
            try:
                return curve.Evaluate(0.5, True)
            except RegenerationFailedException:
                raise
            except Exception:
                pass

    # Fallback: BoundingBox center
    try:
        bbox = element.get_BoundingBox(None)
        if bbox is not None:
            return DB.XYZ(
                (bbox.Min.X + bbox.Max.X) / 2.0,
                (bbox.Min.Y + bbox.Max.Y) / 2.0,
                (bbox.Min.Z + bbox.Max.Z) / 2.0,
            )
    except RegenerationFailedException:
        raise
    except Exception:
        pass
    return DB.XYZ(0.0, 0.0, 0.0)


def _existing_tagged_element_ids(doc, view_id):
    """Return the set of element IDs that already carry an IndependentTag in the view."""
    tagged = set()
    tag_collector = DB.FilteredElementCollector(doc, view_id).OfClass(DB.IndependentTag)
    for tag in tag_collector:
        # Revit 2022+: GetTaggedLocalElementIds returns ICollection<ElementId>
        if hasattr(tag, 'GetTaggedLocalElementIds'):
            for tid in tag.GetTaggedLocalElementIds():
                tagged.add(eid_int(tid))
        elif hasattr(tag, 'TaggedLocalElementId'):
            tagged.add(eid_int(tag.TaggedLocalElementId))
        else:
            raise RuntimeError("Cannot determine the elements referenced by an existing tag")
    # RoomTag is not supported by ElementClassFilter; use its built-in category.
    rt_collector = (DB.FilteredElementCollector(doc, view_id)
                    .OfCategory(DB.BuiltInCategory.OST_RoomTags)
                    .WhereElementIsNotElementType())
    for rt in rt_collector:
        tagged.add(eid_int(rt.TaggedLocalRoomId))
    # Enumeration errors must propagate before any new tags are created.
    return tagged


def _resolve_element_ids(doc, raw_ids):
    """Convert a list of stringified ints into a list of (element, ElementId) tuples + invalid list."""
    valid = []
    invalid = []
    seen = set()
    if not raw_ids:
        return valid, invalid
    for raw in raw_ids:
        try:
            eid = make_element_id(DB.ElementId, raw)
        except Exception:
            invalid.append(str(raw))
            continue
        try:
            element = doc.GetElement(eid)
        except Exception:
            element = None
        if element is None:
            invalid.append(str(raw))
            continue
        element_id = eid_int(element.Id)
        if element_id not in seen:
            seen.add(element_id)
            valid.append((element, eid))
    return valid, invalid


def _create_independent_tag(doc, view, element, tag_symbol_id, head_point, add_leader=False):
    """Create an IndependentTag for a non-room element. Returns (tag, error_string)."""
    try:
        # Reference to the element
        reference = DB.Reference(element)
    except RegenerationFailedException:
        raise
    except Exception as ref_error:
        return None, "Could not create reference: {}".format(ref_error)

    try:
        # Revit 2019+ signature with explicit tag type
        tag = DB.IndependentTag.Create(
            doc,
            tag_symbol_id,
            view.Id,
            reference,
            bool(add_leader),
            DB.TagOrientation.Horizontal,
            head_point,
        )
        return tag, None
    except RegenerationFailedException:
        raise
    except Exception as create_error:
        # Fallback: older signature without explicit tag type
        try:
            tag = DB.IndependentTag.Create(
                doc,
                view.Id,
                reference,
                bool(add_leader),
                DB.TagMode.TM_ADDBY_CATEGORY,
                DB.TagOrientation.Horizontal,
                head_point,
            )
            return tag, None
        except RegenerationFailedException:
            raise
        except Exception as fallback_error:
            return None, "Tag creation failed: {} / fallback: {}".format(create_error, fallback_error)


def _create_room_tag(doc, view, room, tag_symbol_id=None):
    """Create a RoomTag for a Room element. Returns (tag, error_string)."""
    try:
        location = getattr(room, 'Location', None)
        if location is None or not hasattr(location, 'Point') or location.Point is None:
            return None, "Room {} has no location point (likely unplaced)".format(eid_int(room.Id))

        room_point = location.Point
        uv_point = DB.UV(room_point.X, room_point.Y)
        link_room_id = DB.LinkElementId(room.Id)

        room_tag = doc.Create.NewRoomTag(link_room_id, uv_point, view.Id)
        if room_tag is None:
            return None, "RoomTag creation returned no tag"

        # If a specific tag-type was requested, swap it
        if tag_symbol_id is not None:
            try:
                room_tag.RoomTagType = doc.GetElement(tag_symbol_id)
            except RegenerationFailedException:
                raise
            except Exception:
                pass

        return room_tag, None
    except RegenerationFailedException:
        raise
    except Exception as room_error:
        return None, "RoomTag creation failed: {}".format(room_error)


def _ensure_tag_symbol_active(doc, symbol):
    """Tag family symbols must be Active before placement. Activate if needed."""
    if symbol is None:
        return
    if hasattr(symbol, 'IsActive') and not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()


def _operation_status(applied, failed):
    if not failed:
        return "success"
    return "partial_success" if applied else "error"


def _commit_transaction(transaction):
    """Return an error response unless Revit confirms that changes were committed."""
    status = transaction.Commit()
    if status == DB.TransactionStatus.Committed:
        return None
    return routes.Response(status=409, data=sanitize_for_json({
        "status": "error",
        "error": "Tag transaction was not committed: {}".format(status),
        "transaction_status": str(status),
        "applied_count": 0,
        "applied": [],
    }))


def register_routes(api):
    """Register tag-related routes onto the pyRevit Routes API."""

    @api.route('/tags/by_id', methods=['POST'])
    def handle_tag_elements_by_id(uidoc, doc, request):
        """Tag explicit elements (non-Room) in the active view.

        Payload:
          element_ids:  list of element-id strings (required)
          add_leader:   bool, default False
          refresh_view: bool, default True
        """
        route_logger = script.get_logger()
        try:
            payload = request.data if hasattr(request, 'data') else {}
            if not isinstance(payload, dict):
                return routes.Response(status=400, data={"status": "error", "error": "Invalid JSON payload"})

            active_view = uidoc.ActiveView if uidoc else None
            if active_view is None:
                return routes.Response(status=503, data={"status": "error", "error": "No active Revit view"})
            if getattr(active_view, "IsTemplate", False):
                return routes.Response(status=400, data={"status": "error", "error": "Active view is a template"})

            element_ids = payload.get('element_ids')
            if not isinstance(element_ids, list) or not element_ids:
                return routes.Response(status=400, data={"status": "error", "error": "element_ids list required"})
            valid, invalid = _resolve_element_ids(doc, element_ids)
            if not valid:
                return routes.Response(status=400, data=sanitize_for_json({
                    "status": "error",
                    "error": "No valid elements resolved",
                    "invalid_ids": invalid,
                }))

            add_leader = bool(payload.get('add_leader', False))
            refresh_view = payload.get('refresh_view')
            refresh_view = True if refresh_view is None else bool(refresh_view)

            applied = []
            failed = [{"element_id": raw, "error": "Invalid or missing element"} for raw in invalid]
            symbol_cache = {}

            transaction = DB.Transaction(doc, "Tag Elements By ID")
            transaction.Start()
            try:
                for element, eid in valid:
                    try:
                        category = element.Category
                        if category is None:
                            failed.append({"element_id": str(eid_int(eid)), "error": "Element has no category"})
                            continue
                        # Resolve OST_* name from BuiltInCategory enum value of the category
                        try:
                            built_in = System.Enum.ToObject(DB.BuiltInCategory, eid_int(category.Id))
                            host_cat_name = str(built_in)
                        except Exception:
                            host_cat_name = None

                        if host_cat_name is None or host_cat_name not in HOST_TO_TAG_CATEGORY:
                            failed.append({"element_id": str(eid_int(eid)), "error": "Unsupported category: {}".format(host_cat_name)})
                            continue

                        # RoomTag is special; redirect to room handler implicitly
                        if host_cat_name == 'OST_Rooms':
                            room_tag, room_error = _create_room_tag(doc, active_view, element)
                            if room_error:
                                failed.append({"element_id": str(eid_int(eid)), "error": room_error})
                            else:
                                applied.append({"element_id": str(eid_int(eid)), "tag_id": str(eid_int(room_tag.Id)), "category": host_cat_name})
                            continue

                        # Get/cache tag symbol for this category
                        if host_cat_name not in symbol_cache:
                            symbol, sym_error = _find_default_tag_symbol(doc, host_cat_name)
                            if sym_error:
                                symbol_cache[host_cat_name] = (None, sym_error)
                            else:
                                _ensure_tag_symbol_active(doc, symbol)
                                symbol_cache[host_cat_name] = (symbol, None)
                        symbol, sym_error = symbol_cache[host_cat_name]
                        if sym_error or symbol is None:
                            failed.append({"element_id": str(eid_int(eid)), "error": sym_error or "No tag symbol"})
                            continue

                        head_point = _get_element_center_xyz(element)
                        tag, tag_error = _create_independent_tag(
                            doc, active_view, element, symbol.Id, head_point, add_leader=add_leader,
                        )
                        if tag_error:
                            failed.append({"element_id": str(eid_int(eid)), "error": tag_error})
                        else:
                            applied.append({
                                "element_id": str(eid_int(eid)),
                                "tag_id": str(eid_int(tag.Id)),
                                "category": host_cat_name,
                            })
                    except RegenerationFailedException:
                        raise
                    except Exception as item_error:
                        failed.append({"element_id": str(eid_int(eid)), "error": str(item_error)})

                commit_error = _commit_transaction(transaction)
                if commit_error is not None:
                    return commit_error
            except Exception:
                try: transaction.RollBack()
                except Exception: pass
                raise

            if refresh_view:
                try: uidoc.RefreshActiveView()
                except Exception: pass

            return sanitize_for_json({
                "status": _operation_status(applied, failed),
                "message": "Tagged {} of {} elements in view '{}'.".format(
                    len(applied), len(valid), active_view.Name,
                ),
                "view": {"id": str(eid_int(active_view.Id)), "name": active_view.Name},
                "applied_count": len(applied),
                "applied": applied,
                "failed_count": len(failed),
                "failed": failed,
                "invalid_ids": invalid,
            })
        except Exception as global_error:
            route_logger.error("tag_elements_by_id failed: {}".format(global_error), exc_info=True)
            return routes.Response(status=500, data={"status": "error", "error": str(global_error)})

    @api.route('/tags/all_in_view', methods=['POST'])
    def handle_tag_all_in_view(uidoc, doc, request):
        """Tag all untagged elements of one or more categories in the active view.

        Payload:
          category_names: list of OST_* names (e.g. ['OST_Doors', 'OST_Windows'])
          add_leader:     bool, default False
          refresh_view:   bool, default True
        """
        route_logger = script.get_logger()
        try:
            payload = request.data if hasattr(request, 'data') else {}
            if not isinstance(payload, dict):
                return routes.Response(status=400, data={"status": "error", "error": "Invalid JSON payload"})

            categories = payload.get('category_names') or []
            if not categories or not isinstance(categories, list):
                return routes.Response(status=400, data={"status": "error", "error": "category_names list required"})
            if any(not isinstance(name, TEXT_TYPES) for name in categories):
                return routes.Response(status=400, data={"status": "error", "error": "category_names must contain strings"})
            unique_categories = []
            for name in categories:
                if name not in unique_categories:
                    unique_categories.append(name)
            categories = unique_categories

            active_view = uidoc.ActiveView if uidoc else None
            if active_view is None:
                return routes.Response(status=503, data={"status": "error", "error": "No active Revit view"})
            if getattr(active_view, "IsTemplate", False):
                return routes.Response(status=400, data={"status": "error", "error": "Active view is a template"})

            add_leader = bool(payload.get('add_leader', False))
            refresh_view = payload.get('refresh_view')
            refresh_view = True if refresh_view is None else bool(refresh_view)

            already_tagged = _existing_tagged_element_ids(doc, active_view.Id)

            applied_total = []
            failed_total = []
            skipped_already_tagged = 0
            per_category = {}

            transaction = DB.Transaction(doc, "Tag All In View")
            transaction.Start()
            try:
                for cat_name in categories:
                    built_in = _get_built_in_category(cat_name)
                    if built_in is None:
                        per_category[cat_name] = {"error": "Unknown category"}
                        failed_total.append({"category": cat_name, "error": "Unknown category"})
                        continue

                    # Special-case rooms
                    if cat_name == 'OST_Rooms':
                        applied_cat, failed_cat, skipped_cat = _bulk_tag_rooms_in_view(
                            doc, active_view, already_tagged,
                        )
                        per_category[cat_name] = {
                            "applied": len(applied_cat),
                            "failed": len(failed_cat),
                            "skipped_already_tagged": skipped_cat,
                        }
                        applied_total.extend(applied_cat)
                        failed_total.extend(failed_cat)
                        skipped_already_tagged += skipped_cat
                        continue

                    if cat_name not in HOST_TO_TAG_CATEGORY:
                        per_category[cat_name] = {"error": "No tag mapping"}
                        failed_total.append({"category": cat_name, "error": "No tag mapping"})
                        continue

                    symbol, sym_error = _find_default_tag_symbol(doc, cat_name)
                    if sym_error:
                        per_category[cat_name] = {"error": sym_error}
                        failed_total.append({"category": cat_name, "error": sym_error})
                        continue
                    _ensure_tag_symbol_active(doc, symbol)

                    collector = (DB.FilteredElementCollector(doc, active_view.Id)
                                 .OfCategory(built_in)
                                 .WhereElementIsNotElementType())
                    cat_applied = 0
                    cat_failed = 0
                    cat_skipped = 0
                    for element in collector:
                        eid = element.Id
                        if eid_int(eid) in already_tagged:
                            cat_skipped += 1
                            continue
                        try:
                            head_point = _get_element_center_xyz(element)
                            tag, tag_error = _create_independent_tag(
                                doc, active_view, element, symbol.Id, head_point, add_leader=add_leader,
                            )
                            if tag_error:
                                failed_total.append({"element_id": str(eid_int(eid)), "category": cat_name, "error": tag_error})
                                cat_failed += 1
                            else:
                                applied_total.append({
                                    "element_id": str(eid_int(eid)),
                                    "tag_id": str(eid_int(tag.Id)),
                                    "category": cat_name,
                                })
                                cat_applied += 1
                                already_tagged.add(eid_int(eid))
                        except RegenerationFailedException:
                            raise
                        except Exception as item_error:
                            failed_total.append({"element_id": str(eid_int(eid)), "category": cat_name, "error": str(item_error)})
                            cat_failed += 1

                    skipped_already_tagged += cat_skipped
                    per_category[cat_name] = {
                        "applied": cat_applied,
                        "failed": cat_failed,
                        "skipped_already_tagged": cat_skipped,
                    }

                commit_error = _commit_transaction(transaction)
                if commit_error is not None:
                    return commit_error
            except Exception:
                try: transaction.RollBack()
                except Exception: pass
                raise

            if refresh_view:
                try: uidoc.RefreshActiveView()
                except Exception: pass

            return sanitize_for_json({
                "status": _operation_status(applied_total, failed_total),
                "message": "Tagged {} elements across {} categories in view '{}' (skipped {} already-tagged).".format(
                    len(applied_total), len(categories), active_view.Name, skipped_already_tagged,
                ),
                "view": {"id": str(eid_int(active_view.Id)), "name": active_view.Name},
                "per_category": per_category,
                "applied_count": len(applied_total),
                "failed_count": len(failed_total),
                "skipped_already_tagged": skipped_already_tagged,
                "failed_sample": failed_total[:10],  # cap output
            })
        except Exception as global_error:
            route_logger.error("tag_all_in_view failed: {}".format(global_error), exc_info=True)
            return routes.Response(status=500, data={"status": "error", "error": str(global_error)})

    @api.route('/tags/rooms', methods=['POST'])
    def handle_tag_rooms(uidoc, doc, request):
        """Place RoomTags for explicit room IDs OR all untagged rooms in the active view.

        Payload (one of):
          room_ids:     list of element-id strings of Room elements
          all_in_view:  bool, when true tag every untagged room visible in active view
          refresh_view: bool, default True
        """
        route_logger = script.get_logger()
        try:
            payload = request.data if hasattr(request, 'data') else {}
            if not isinstance(payload, dict):
                return routes.Response(status=400, data={"status": "error", "error": "Invalid JSON payload"})

            all_in_view = payload.get('all_in_view', False)
            if not isinstance(all_in_view, bool):
                return routes.Response(status=400, data={"status": "error", "error": "all_in_view must be a boolean"})
            room_ids = payload.get('room_ids') or []
            if not all_in_view and (not isinstance(room_ids, list) or not room_ids):
                return routes.Response(status=400, data={"status": "error", "error": "room_ids list required or set all_in_view=true"})

            active_view = uidoc.ActiveView if uidoc else None
            if active_view is None:
                return routes.Response(status=503, data={"status": "error", "error": "No active Revit view"})
            if getattr(active_view, "IsTemplate", False):
                return routes.Response(status=400, data={"status": "error", "error": "Active view is a template"})

            refresh_view = payload.get('refresh_view')
            refresh_view = True if refresh_view is None else bool(refresh_view)

            already_tagged = _existing_tagged_element_ids(doc, active_view.Id)
            applied = []
            failed = []
            invalid = []
            skipped = 0

            rooms_to_tag = []
            if all_in_view:
                collector = (DB.FilteredElementCollector(doc, active_view.Id)
                             .OfCategory(DB.BuiltInCategory.OST_Rooms)
                             .WhereElementIsNotElementType())
                for room in collector:
                    rooms_to_tag.append(room)
            else:
                valid, invalid = _resolve_element_ids(doc, room_ids)
                failed.extend({"element_id": raw, "error": "Invalid or missing room"} for raw in invalid)
                for element, _eid in valid:
                    if element.Category is not None and eid_int(element.Category.Id) == int(DB.BuiltInCategory.OST_Rooms):
                        rooms_to_tag.append(element)
                    else:
                        failed.append({"element_id": str(eid_int(element.Id)), "error": "Not a Room"})

            transaction = DB.Transaction(doc, "Tag Rooms")
            transaction.Start()
            try:
                for room in rooms_to_tag:
                    if eid_int(room.Id) in already_tagged:
                        skipped += 1
                        continue
                    room_tag, room_error = _create_room_tag(doc, active_view, room)
                    if room_error:
                        failed.append({"element_id": str(eid_int(room.Id)), "error": room_error})
                    else:
                        applied.append({
                            "room_id": str(eid_int(room.Id)),
                            "tag_id": str(eid_int(room_tag.Id)),
                        })
                        already_tagged.add(eid_int(room.Id))
                commit_error = _commit_transaction(transaction)
                if commit_error is not None:
                    return commit_error
            except Exception:
                try: transaction.RollBack()
                except Exception: pass
                raise

            if refresh_view:
                try: uidoc.RefreshActiveView()
                except Exception: pass

            return sanitize_for_json({
                "status": _operation_status(applied, failed),
                "message": "Tagged {} of {} rooms in view '{}' (skipped {} already-tagged).".format(
                    len(applied), len(rooms_to_tag), active_view.Name, skipped,
                ),
                "view": {"id": str(eid_int(active_view.Id)), "name": active_view.Name},
                "applied_count": len(applied),
                "applied": applied,
                "failed_count": len(failed),
                "failed": failed,
                "invalid_ids": invalid,
                "skipped_already_tagged": skipped,
            })
        except Exception as global_error:
            route_logger.error("tag_rooms failed: {}".format(global_error), exc_info=True)
            return routes.Response(status=500, data={"status": "error", "error": str(global_error)})


def _bulk_tag_rooms_in_view(doc, active_view, already_tagged):
    """Helper for the all_in_view route: tag every untagged room in the view."""
    applied = []
    failed = []
    skipped = 0
    collector = (DB.FilteredElementCollector(doc, active_view.Id)
                 .OfCategory(DB.BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType())
    for room in collector:
        if eid_int(room.Id) in already_tagged:
            skipped += 1
            continue
        room_tag, room_error = _create_room_tag(doc, active_view, room)
        if room_error:
            failed.append({"element_id": str(eid_int(room.Id)), "category": "OST_Rooms", "error": room_error})
        else:
            applied.append({
                "element_id": str(eid_int(room.Id)),
                "tag_id": str(eid_int(room_tag.Id)),
                "category": "OST_Rooms",
            })
            already_tagged.add(eid_int(room.Id))
    return applied, failed, skipped
