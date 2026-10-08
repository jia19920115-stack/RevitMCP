"""MCP tools that create new Revit elements: columns, walls, beams, floors, doors,
windows, pipes and MEP fixtures/equipment.

All lengths are millimetres. Each call is one Revit transaction (one Ctrl+Z undoes it).
The Revit-side implementation lives in lib/routes/modeling_routes.py.
"""

from RevitMCP_ExternalServer.tools.registry import ToolDefinition


LIST_MODELING_TYPES_TOOL_NAME = "list_modeling_types"
CREATE_COLUMNS_TOOL_NAME = "create_columns"
CREATE_WALLS_TOOL_NAME = "create_walls"
CREATE_BEAMS_TOOL_NAME = "create_beams"
CREATE_FLOORS_TOOL_NAME = "create_floors"
CREATE_DOORS_TOOL_NAME = "create_doors"
CREATE_WINDOWS_TOOL_NAME = "create_windows"
CREATE_PIPES_TOOL_NAME = "create_pipes"
CREATE_MEP_FIXTURES_TOOL_NAME = "create_mep_fixtures"

FIXTURE_CATEGORIES = [
    "plumbing_fixtures",
    "plumbing_equipment",
    "sprinklers",
    "mechanical_equipment",
    "lighting_fixtures",
    "electrical_fixtures",
    "electrical_equipment",
    "fire_alarm_devices",
]

_PRESERVE_KEYS = [
    "status",
    "message",
    "dry_run",
    "created_count",
    "created",
    "failed",
    "warnings",
    "warning_count",
    "undo_hint",
]

_POINT_SCHEMA = {
    "type": "object",
    "properties": {
        "x_mm": {"type": "number"},
        "y_mm": {"type": "number"},
    },
    "required": ["x_mm", "y_mm"],
    "description": "Plan point in millimetres, in Revit internal (project base) coordinates.",
}

_COMMON_PROPERTIES = {
    "dry_run": {
        "type": "boolean",
        "description": "When true, only validates levels, types and geometry and reports what would be created. Default false.",
    },
    "max_items": {
        "type": "integer",
        "description": "Safety limit on the number of items in one call. Default 200, max 1000.",
    },
}


def _call(services, tool_name: str, path: str, payload: dict) -> dict:
    clean_payload = {key: value for key, value in payload.items() if value is not None}
    services.logger.info("MCP Tool executed: %s", tool_name)
    result = services.revit_client.call_listener(command_path=path, method="POST", payload_data=clean_payload)
    if not isinstance(result, dict):
        return {"status": "error", "message": "Unexpected response from Revit.", "raw": str(result)[:500]}
    return result


def list_modeling_types_handler(services, **_kwargs) -> dict:
    return _call(services, LIST_MODELING_TYPES_TOOL_NAME, "/modeling/types", {})


def create_columns_handler(
    services,
    columns: list = None,
    level_name: str = None,
    type_name: str = None,
    family_name: str = None,
    top_level_name: str = None,
    height_mm: float = None,
    base_offset_mm: float = None,
    top_offset_mm: float = None,
    rotation_deg: float = None,
    structural: bool = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_COLUMNS_TOOL_NAME, "/modeling/create_columns", {
        "columns": columns,
        "level_name": level_name,
        "type_name": type_name,
        "family_name": family_name,
        "top_level_name": top_level_name,
        "height_mm": height_mm,
        "base_offset_mm": base_offset_mm,
        "top_offset_mm": top_offset_mm,
        "rotation_deg": rotation_deg,
        "structural": structural,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def create_walls_handler(
    services,
    walls: list = None,
    level_name: str = None,
    type_name: str = None,
    top_level_name: str = None,
    height_mm: float = None,
    base_offset_mm: float = None,
    top_offset_mm: float = None,
    structural: bool = None,
    trim_at_columns: bool = None,
    trim_top_to_structure: bool = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_WALLS_TOOL_NAME, "/modeling/create_walls", {
        "trim_at_columns": trim_at_columns,
        "trim_top_to_structure": trim_top_to_structure,
        "walls": walls,
        "level_name": level_name,
        "type_name": type_name,
        "top_level_name": top_level_name,
        "height_mm": height_mm,
        "base_offset_mm": base_offset_mm,
        "top_offset_mm": top_offset_mm,
        "structural": structural,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def create_beams_handler(
    services,
    beams: list = None,
    level_name: str = None,
    type_name: str = None,
    family_name: str = None,
    offset_mm: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_BEAMS_TOOL_NAME, "/modeling/create_beams", {
        "beams": beams,
        "level_name": level_name,
        "type_name": type_name,
        "family_name": family_name,
        "offset_mm": offset_mm,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def create_floors_handler(
    services,
    floors: list = None,
    level_name: str = None,
    type_name: str = None,
    offset_mm: float = None,
    structural: bool = None,
    trim_to_structure: bool = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_FLOORS_TOOL_NAME, "/modeling/create_floors", {
        "trim_to_structure": trim_to_structure,
        "floors": floors,
        "level_name": level_name,
        "type_name": type_name,
        "offset_mm": offset_mm,
        "structural": structural,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def _hosted_payload(items_key, items, level_name, type_name, family_name, sill_height_mm,
                    snap_tolerance_mm, dry_run, max_items):
    return {
        items_key: items,
        "level_name": level_name,
        "type_name": type_name,
        "family_name": family_name,
        "sill_height_mm": sill_height_mm,
        "snap_tolerance_mm": snap_tolerance_mm,
        "dry_run": dry_run,
        "max_items": max_items,
    }


def create_doors_handler(
    services,
    doors: list = None,
    level_name: str = None,
    type_name: str = None,
    family_name: str = None,
    sill_height_mm: float = None,
    snap_tolerance_mm: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_DOORS_TOOL_NAME, "/modeling/create_doors", _hosted_payload(
        "doors", doors, level_name, type_name, family_name, sill_height_mm, snap_tolerance_mm, dry_run, max_items))


def create_windows_handler(
    services,
    windows: list = None,
    level_name: str = None,
    type_name: str = None,
    family_name: str = None,
    sill_height_mm: float = None,
    snap_tolerance_mm: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_WINDOWS_TOOL_NAME, "/modeling/create_windows", _hosted_payload(
        "windows", windows, level_name, type_name, family_name, sill_height_mm, snap_tolerance_mm, dry_run, max_items))


def create_pipes_handler(
    services,
    pipes: list = None,
    level_name: str = None,
    type_name: str = None,
    system_type_name: str = None,
    diameter_mm: float = None,
    offset_mm: float = None,
    connect_fittings: bool = None,
    slope_percent: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_PIPES_TOOL_NAME, "/mep/create_pipes", {
        "pipes": pipes,
        "level_name": level_name,
        "type_name": type_name,
        "system_type_name": system_type_name,
        "diameter_mm": diameter_mm,
        "offset_mm": offset_mm,
        "connect_fittings": connect_fittings,
        "slope_percent": slope_percent,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def create_mep_fixtures_handler(
    services,
    fixtures: list = None,
    category: str = None,
    level_name: str = None,
    type_name: str = None,
    family_name: str = None,
    offset_mm: float = None,
    rotation_deg: float = None,
    snap_tolerance_mm: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, CREATE_MEP_FIXTURES_TOOL_NAME, "/modeling/create_mep_fixtures", {
        "fixtures": fixtures,
        "category": category,
        "level_name": level_name,
        "type_name": type_name,
        "family_name": family_name,
        "offset_mm": offset_mm,
        "rotation_deg": rotation_deg,
        "snap_tolerance_mm": snap_tolerance_mm,
        "dry_run": dry_run,
        "max_items": max_items,
    })


_HOSTED_ITEM_DESCRIPTION = (
    "Each item uses ONE of two placement modes: (A) {wall_id, distance_mm} = distance from the wall's start "
    "point along its location line; or (B) {x_mm, y_mm} = plan point, snapped to the nearest wall within "
    "snap_tolerance_mm (default 300) on level_name. Items may also set type_name, family_name, "
    "sill_height_mm, flip_facing, flip_hand."
)


def _hosted_schema(items_key: str, kind: str) -> dict:
    return {
        "type": "object",
        "properties": {
            items_key: {
                "type": "array",
                "description": _HOSTED_ITEM_DESCRIPTION,
                "items": {
                    "type": "object",
                    "properties": {
                        "wall_id": {"type": "string"},
                        "distance_mm": {"type": "number"},
                        "x_mm": {"type": "number"},
                        "y_mm": {"type": "number"},
                        "flip_facing": {"type": "boolean"},
                        "flip_hand": {"type": "boolean"},
                    },
                },
            },
            "level_name": {
                "type": "string",
                "description": "Level for point mode (required there). In wall_id mode defaults to the wall's base level.",
            },
            "type_name": {"type": "string", "description": "{} family type name, or 'Family : Type'.".format(kind)},
            "family_name": {"type": "string"},
            "sill_height_mm": {"type": "number", "description": "Sill height. Omit to keep the family default."},
            "snap_tolerance_mm": {"type": "number"},
            **_COMMON_PROPERTIES,
        },
        "required": [items_key],
    }


def apply_join_priority_handler(
    services,
    level_name: str = None,
    top_level_name: str = None,
    element_ids: list = None,
    max_pairs: int = None,
    dry_run: bool = None,
    **_kwargs,
) -> dict:
    return _call(services, "apply_join_priority", "/modeling/apply_join_priority", {
        "level_name": level_name,
        "top_level_name": top_level_name,
        "element_ids": element_ids,
        "max_pairs": max_pairs,
        "dry_run": dry_run,
    })


def create_ceilings_handler(
    services,
    ceilings: list = None,
    all_rooms_on_level: bool = None,
    level_name: str = None,
    type_name: str = None,
    height_mm: float = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, "create_ceilings", "/modeling/create_ceilings", {
        "ceilings": ceilings,
        "all_rooms_on_level": all_rooms_on_level,
        "level_name": level_name,
        "type_name": type_name,
        "height_mm": height_mm,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def create_stairs_handler(
    services,
    stairs: list = None,
    shape: str = None,
    turn: str = None,
    level_name: str = None,
    top_level_name: str = None,
    type_name: str = None,
    width_mm: float = None,
    direction_deg: float = None,
    max_riser_mm: float = None,
    tread_mm: float = None,
    gap_mm: float = None,
    first_run_risers: int = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, "create_stairs", "/modeling/create_stairs", {
        "stairs": stairs,
        "shape": shape,
        "turn": turn,
        "level_name": level_name,
        "top_level_name": top_level_name,
        "type_name": type_name,
        "width_mm": width_mm,
        "direction_deg": direction_deg,
        "max_riser_mm": max_riser_mm,
        "tread_mm": tread_mm,
        "gap_mm": gap_mm,
        "first_run_risers": first_run_risers,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def edit_floor_shape_handler(
    services,
    floor_id: str = None,
    points: list = None,
    split_lines: list = None,
    reset: bool = None,
    snap_tolerance_mm: float = None,
    dry_run: bool = None,
    **_kwargs,
) -> dict:
    return _call(services, "edit_floor_shape", "/modeling/edit_floor_shape", {
        "floor_id": floor_id,
        "points": points,
        "split_lines": split_lines,
        "reset": reset,
        "snap_tolerance_mm": snap_tolerance_mm,
        "dry_run": dry_run,
    })


def create_levels_handler(
    services,
    levels: list = None,
    create_plan_views: bool = None,
    create_ceiling_plans: bool = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, "create_levels", "/modeling/create_levels", {
        "levels": levels,
        "create_plan_views": create_plan_views,
        "create_ceiling_plans": create_ceiling_plans,
        "dry_run": dry_run,
        "max_items": max_items,
    })


def build_modeling_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=LIST_MODELING_TYPES_TOOL_NAME,
            description=(
                "Lists what is available for creating elements: levels (with elevations in mm), column, beam, wall, "
                "floor, door and window types, pipe types, piping system types, and MEP fixture/equipment family "
                "types grouped by category (with each family's placement type), stairs and ceiling types, and placed "
                "rooms. Call this before any create_* tool to get exact names."
            ),
            json_schema={"type": "object", "properties": {}},
            handler=list_modeling_types_handler,
        ),
        ToolDefinition(
            name=CREATE_COLUMNS_TOOL_NAME,
            description=(
                "Creates columns at plan points (mm). Call-level settings (level_name, type_name, top_level_name or "
                "height_mm, offsets, rotation_deg) apply to every item and can be overridden per item. "
                "structural=true (default) uses Structural Columns, false uses architectural Columns. "
                "Give top_level_name (top constrained to a level, plus top_offset_mm) or height_mm (fixed height). "
                "One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "array",
                        "description": (
                            "Column locations. Each item is {x_mm, y_mm} and may also override level_name, "
                            "type_name, family_name, top_level_name, height_mm, base_offset_mm, top_offset_mm, rotation_deg."
                        ),
                        "items": {"type": "object"},
                    },
                    "level_name": {"type": "string", "description": "Base level name (exact)."},
                    "type_name": {"type": "string", "description": "Family type name, or 'Family : Type'."},
                    "family_name": {"type": "string", "description": "Family name, needed only if type names repeat."},
                    "top_level_name": {"type": "string"},
                    "height_mm": {"type": "number"},
                    "base_offset_mm": {"type": "number"},
                    "top_offset_mm": {"type": "number"},
                    "rotation_deg": {"type": "number", "description": "Counter-clockwise rotation in plan, degrees."},
                    "structural": {"type": "boolean"},
                    **_COMMON_PROPERTIES,
                },
                "required": ["columns"],
            },
            handler=create_columns_handler,
        ),
        ToolDefinition(
            name=CREATE_WALLS_TOOL_NAME,
            description=(
                "Creates straight walls from start/end plan points (mm). The line is the wall centerline. "
                "trim_at_columns=true stops the walls at existing column faces; trim_top_to_structure=true stops the "
                "wall top at the underside of beams/floors above (level line where there are none). "
                "Give top_level_name (top constrained, plus top_offset_mm) or height_mm (unconnected height). "
                "Call-level settings can be overridden per item. One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "walls": {
                        "type": "array",
                        "description": (
                            "Each item is {start: {x_mm, y_mm}, end: {x_mm, y_mm}} and may override level_name, "
                            "type_name, top_level_name, height_mm, base_offset_mm, top_offset_mm, structural."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {"start": _POINT_SCHEMA, "end": _POINT_SCHEMA},
                            "required": ["start", "end"],
                        },
                    },
                    "level_name": {"type": "string", "description": "Base level name (exact)."},
                    "type_name": {"type": "string", "description": "Wall type name (exact)."},
                    "top_level_name": {"type": "string"},
                    "height_mm": {"type": "number"},
                    "base_offset_mm": {"type": "number"},
                    "top_offset_mm": {"type": "number"},
                    "structural": {"type": "boolean", "description": "Structural wall. Default true (house rule: walls are structural)."},
                    "trim_at_columns": {
                        "type": "boolean",
                        "description": (
                            "When true, the wall line is split where it passes through existing structural or "
                            "architectural columns, so each wall piece stops at the column faces. Default false."
                        ),
                    },
                    "trim_top_to_structure": {
                        "type": "boolean",
                        "description": (
                            "When true, the wall top follows the underside of existing beams and floors above its "
                            "centreline: under a beam it stops at the beam bottom, under a slab at the slab bottom, "
                            "and where there is neither it goes up to the top level/height. Such walls are created "
                            "with an edited profile (they do not update if the beams move). Create beams and floors "
                            "first. Default false."
                        ),
                    },
                    **_COMMON_PROPERTIES,
                },
                "required": ["walls"],
            },
            handler=create_walls_handler,
        ),
        ToolDefinition(
            name=CREATE_BEAMS_TOOL_NAME,
            description=(
                "Creates straight structural beams from start/end plan points (mm). The beam is placed on "
                "level_name with its top at the level elevation + offset_mm (Revit's default top justification). "
                "Call-level settings can be overridden per item. One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "beams": {
                        "type": "array",
                        "description": (
                            "Each item is {start: {x_mm, y_mm}, end: {x_mm, y_mm}} and may override level_name, "
                            "type_name, family_name, offset_mm."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {"start": _POINT_SCHEMA, "end": _POINT_SCHEMA},
                            "required": ["start", "end"],
                        },
                    },
                    "level_name": {"type": "string", "description": "Reference level name (exact)."},
                    "type_name": {"type": "string", "description": "Beam family type name, or 'Family : Type'."},
                    "family_name": {"type": "string"},
                    "offset_mm": {"type": "number", "description": "Beam top offset from the level. Default 0."},
                    **_COMMON_PROPERTIES,
                },
                "required": ["beams"],
            },
            handler=create_beams_handler,
        ),
        ToolDefinition(
            name=CREATE_FLOORS_TOOL_NAME,
            description=(
                "Creates floors from closed plan boundaries (mm, at least 3 points; do not repeat the first point). "
                "Optional openings (inner boundaries) need Revit 2022+. offset_mm sets Height Offset From Level. "
                "trim_to_structure=true cuts the floor back to the inner faces of existing columns and beams. "
                "Call-level settings can be overridden per item. One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "floors": {
                        "type": "array",
                        "description": (
                            "Each item is {boundary: [{x_mm, y_mm}, ...], openings: [[{x_mm, y_mm}, ...], ...]} "
                            "and may override level_name, type_name, offset_mm, structural."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "boundary": {"type": "array", "items": _POINT_SCHEMA},
                                "openings": {
                                    "type": "array",
                                    "items": {"type": "array", "items": _POINT_SCHEMA},
                                },
                            },
                            "required": ["boundary"],
                        },
                    },
                    "level_name": {"type": "string", "description": "Level name (exact)."},
                    "type_name": {"type": "string", "description": "Floor type name (exact)."},
                    "offset_mm": {"type": "number"},
                    "structural": {"type": "boolean", "description": "Structural floor. Default true (house rule: slabs are structural)."},
                    "trim_to_structure": {
                        "type": "boolean",
                        "description": (
                            "When true, existing columns and beams are subtracted from the boundary so the floor "
                            "follows their inner faces instead of overlapping them (give the boundary on column "
                            "centerlines). May produce several floor pieces. Revit 2022+. Default false."
                        ),
                    },
                    **_COMMON_PROPERTIES,
                },
                "required": ["floors"],
            },
            handler=create_floors_handler,
        ),
        ToolDefinition(
            name=CREATE_DOORS_TOOL_NAME,
            description=(
                "Places doors in existing walls. " + _HOSTED_ITEM_DESCRIPTION +
                " Curtain walls are not supported. One transaction per call; supports dry_run."
            ),
            json_schema=_hosted_schema("doors", "Door"),
            handler=create_doors_handler,
        ),
        ToolDefinition(
            name=CREATE_WINDOWS_TOOL_NAME,
            description=(
                "Places windows in existing walls. " + _HOSTED_ITEM_DESCRIPTION +
                " sill_height_mm sets the sill height (family default if omitted). "
                "One transaction per call; supports dry_run."
            ),
            json_schema=_hosted_schema("windows", "Window"),
            handler=create_windows_handler,
        ),
        ToolDefinition(
            name=CREATE_PIPES_TOOL_NAME,
            description=(
                "Creates pipe runs (plumbing, drainage, fire protection...). Each run is a polyline in FLOW order; "
                "segments are joined with elbows, and end tees use the PIPE TYPE's routing preferences, so each "
                "system keeps its own fittings. Points are {x_mm, y_mm, z_mm?}; z_mm is the pipe CENTRE height above "
                "level_name. slope_percent (e.g. 1 for 1%) fills in every point WITHOUT z_mm by falling from the "
                "previous point along the horizontal distance; give z_mm explicitly for vertical drops. "
                "start_connect {element_id, connector_id?} starts the run at a fixture/fitting/pipe-end connector "
                "(that connector is the first point, diameter defaults to its size) and connects it. "
                "end_connect is either {pipe_id} or {run_index} (a run earlier in this call) to TEE into that pipe "
                "(last point is snapped onto its centreline), or {element_id, connector_id?} to connect to a "
                "connector; snap_tolerance_mm default 500. Use get_mep_connectors to find connectors. "
                "One transaction per call; supports dry_run (returns the computed points)."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "pipes": {
                        "type": "array",
                        "description": (
                            "Pipe runs. Each item is {points: [{x_mm, y_mm, z_mm?}, ...], start_connect?, end_connect?} "
                            "or {start, end}, and may override level_name, type_name, system_type_name, diameter_mm, "
                            "offset_mm, slope_percent, connect_fittings."
                        ),
                        "items": {"type": "object"},
                    },
                    "level_name": {"type": "string"},
                    "type_name": {"type": "string", "description": "Pipe type name (exact)."},
                    "system_type_name": {"type": "string", "description": "Piping system type name (exact)."},
                    "diameter_mm": {"type": "number", "description": "Nominal diameter. Omit to keep the type default or the start connector size."},
                    "offset_mm": {"type": "number", "description": "Default centre height above the level. Default 0."},
                    "slope_percent": {"type": "number", "description": "Fall in % along the flow for points without z_mm."},
                    "connect_fittings": {"type": "boolean"},
                    **_COMMON_PROPERTIES,
                },
                "required": ["pipes"],
            },
            handler=create_pipes_handler,
        ),
        ToolDefinition(
            name=CREATE_MEP_FIXTURES_TOOL_NAME,
            description=(
                "Places MEP fixtures and equipment (plumbing fixtures, sprinklers, mechanical equipment, lighting, "
                "electrical fixtures/equipment, fire alarm devices). category selects the Revit category. "
                "Level-based families are placed at {x_mm, y_mm} on level_name with offset_mm height; wall-hosted "
                "families use the same wall placement modes as create_doors ({wall_id, distance_mm} or a point "
                "snapped to the nearest wall); face-based families are placed on the level's plane. "
                "Use get_mep_connectors on the result to find drain outlets, then create_pipes start_connect. "
                "One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "fixtures": {
                        "type": "array",
                        "description": (
                            "Each item is {x_mm, y_mm} or {wall_id, distance_mm}, and may override category, "
                            "level_name, type_name, family_name, offset_mm, rotation_deg, flip_facing, flip_hand."
                        ),
                        "items": {"type": "object"},
                    },
                    "category": {"type": "string", "enum": FIXTURE_CATEGORIES},
                    "level_name": {"type": "string"},
                    "type_name": {"type": "string", "description": "Family type name, or 'Family : Type'."},
                    "family_name": {"type": "string"},
                    "offset_mm": {"type": "number", "description": "Height offset above the level. Default 0."},
                    "rotation_deg": {"type": "number", "description": "Plan rotation for non-hosted families."},
                    "snap_tolerance_mm": {"type": "number"},
                    **_COMMON_PROPERTIES,
                },
                "required": ["fixtures"],
            },
            handler=create_mep_fixtures_handler,
        ),
        ToolDefinition(
            name="apply_join_priority",
            description=(
                "Joins overlapping columns, beams, floors and walls so the higher-priority element cuts the lower one: "
                "column > beam > floor > wall. Joins missing pairs and switches wrong join order. Scope with "
                "level_name (elements between that level and the next one up, or top_level_name), or element_ids; "
                "no scope means the whole model. dry_run reports the pairs and their current state. One transaction."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "level_name": {"type": "string"},
                    "top_level_name": {"type": "string"},
                    "element_ids": {"type": "array", "items": {"type": "string"}},
                    "max_pairs": {"type": "integer", "description": "Safety limit. Default 1000."},
                    "dry_run": {"type": "boolean"},
                },
            },
            handler=apply_join_priority_handler,
        ),
        ToolDefinition(
            name="create_ceilings",
            description=(
                "Creates ceilings (Revit 2022+). Each item uses one of: {boundary: [{x_mm, y_mm}...], openings?} with "
                "level_name; {room_id} or {room_name / room_number} (+ level_name) to follow a placed room's finish "
                "boundary; or a point {x_mm, y_mm} with level_name - uses the room there, or if there is none, the "
                "area enclosed by walls/columns around the point. all_rooms_on_level=true makes one ceiling per placed "
                "room on level_name. height_mm is the height above the level (required). Supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "ceilings": {"type": "array", "items": {"type": "object"}},
                    "all_rooms_on_level": {"type": "boolean"},
                    "level_name": {"type": "string"},
                    "type_name": {"type": "string", "description": "Ceiling type name (exact)."},
                    "height_mm": {"type": "number", "description": "Ceiling height above the level."},
                    **_COMMON_PROPERTIES,
                },
            },
            handler=create_ceilings_handler,
        ),
        ToolDefinition(
            name="create_stairs",
            description=(
                "Creates L- or U-shaped stairs between level_name and top_level_name. Each item gives start "
                "{x_mm, y_mm} = start of the first run's centreline; the first run goes in direction_deg (0 = +X, "
                "counter-clockwise), then turns 'left' or 'right' at an automatic landing. Riser count comes from the "
                "stairs type's max riser height (or max_riser_mm), tread from its min tread (or tread_mm), width from "
                "width_mm (or the type minimum). U stairs: the second run is parallel, offset by width + gap_mm. "
                "Risers are split evenly unless first_run_risers is given. Each stair is its own edit; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "stairs": {
                        "type": "array",
                        "description": "Each item: {x_mm, y_mm} start point; may override any call-level setting.",
                        "items": {"type": "object"},
                    },
                    "shape": {"type": "string", "enum": ["L", "U"]},
                    "turn": {"type": "string", "enum": ["left", "right"]},
                    "level_name": {"type": "string"},
                    "top_level_name": {"type": "string"},
                    "type_name": {"type": "string", "description": "Stairs type name (exact)."},
                    "width_mm": {"type": "number"},
                    "direction_deg": {"type": "number"},
                    "max_riser_mm": {"type": "number"},
                    "tread_mm": {"type": "number"},
                    "gap_mm": {"type": "number", "description": "U stairs: gap between the two runs. Default 0."},
                    "first_run_risers": {"type": "integer"},
                    **_COMMON_PROPERTIES,
                },
                "required": ["stairs"],
            },
            handler=create_stairs_handler,
        ),
        ToolDefinition(
            name="edit_floor_shape",
            description=(
                "Edits a floor's sub-elements (Modify Sub Elements): points [{x_mm, y_mm, offset_mm}] add a point "
                "(or reuse an existing vertex within snap_tolerance_mm, default 20) and set its offset from the "
                "original top (negative = lower, e.g. a drain); split_lines [{start:{x_mm,y_mm,offset_mm?}, "
                "end:{...}}] add split lines (e.g. ridges/valleys for drainage, or set two edge vertices lower for a "
                "ramp). Existing corner vertices can be moved by giving their coordinates. reset=true clears earlier "
                "shape edits first. dry_run lists current vertices. One transaction."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "floor_id": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "object"}},
                    "split_lines": {"type": "array", "items": {"type": "object"}},
                    "reset": {"type": "boolean"},
                    "snap_tolerance_mm": {"type": "number"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["floor_id"],
            },
            handler=edit_floor_shape_handler,
        ),
        ToolDefinition(
            name="create_levels",
            description=(
                "Creates levels. Each item is {name, elevation_mm} (absolute) or {name, above_level, height_mm} "
                "(height above an existing level or one created earlier in the same call, so a whole stack like "
                "FL3, FL4, RF can be made at once). Creates a floor plan and a ceiling plan for each new level by "
                "default. Refuses duplicate names or elevations. Supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "levels": {"type": "array", "items": {"type": "object"}},
                    "create_plan_views": {"type": "boolean", "description": "Default true."},
                    "create_ceiling_plans": {"type": "boolean", "description": "Default true."},
                    **_COMMON_PROPERTIES,
                },
                "required": ["levels"],
            },
            handler=create_levels_handler,
        ),
    ]
