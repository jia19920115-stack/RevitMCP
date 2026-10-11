"""MCP tools for plumbing/drainage modeling.

Revit-side implementation: lib/routes/mep_routes.py. All lengths are millimetres.
"""

from RevitMCP_ExternalServer.tools.registry import ToolDefinition


LINK_CATEGORIES = [
    "walls", "floors", "structural_columns", "columns", "beams", "doors", "windows",
    "rooms", "shaft_openings", "plumbing_fixtures", "generic_models",
]


def _call(services, tool_name: str, path: str, payload: dict) -> dict:
    clean_payload = {key: value for key, value in payload.items() if value is not None}
    services.logger.info("MCP Tool executed: %s", tool_name)
    result = services.revit_client.call_listener(command_path=path, method="POST", payload_data=clean_payload)
    if not isinstance(result, dict):
        return {"status": "error", "message": "Unexpected response from Revit.", "raw": str(result)[:500]}
    return result


def get_linked_elements_handler(
    services,
    link_name_contains: str = None,
    categories: list = None,
    host_level_name: str = None,
    limit: int = None,
    **_kwargs,
) -> dict:
    return _call(services, "get_linked_elements", "/mep/linked_elements", {
        "link_name_contains": link_name_contains,
        "categories": categories,
        "host_level_name": host_level_name,
        "limit": limit,
    })


def get_mep_connectors_handler(services, element_ids: list = None, **_kwargs) -> dict:
    return _call(services, "get_mep_connectors", "/mep/connectors", {"element_ids": element_ids})


def place_pipe_fittings_handler(
    services,
    fittings: list = None,
    family_name: str = None,
    type_name: str = None,
    diameter_mm: float = None,
    outlet_direction: dict = None,
    dry_run: bool = None,
    max_items: int = None,
    **_kwargs,
) -> dict:
    return _call(services, "place_pipe_fittings", "/mep/place_pipe_fittings", {
        "fittings": fittings,
        "family_name": family_name,
        "type_name": type_name,
        "diameter_mm": diameter_mm,
        "outlet_direction": outlet_direction,
        "dry_run": dry_run,
        "max_items": max_items,
    })


_COMMON = {
    "dry_run": {"type": "boolean", "description": "Validate and report only. Default false."},
    "max_items": {"type": "integer", "description": "Safety limit per call. Default 200."},
}


def build_mep_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_linked_elements",
            description=(
                "Reads elements INSIDE loaded Revit links (e.g. an architectural/structural model linked into the "
                "MEP file) and returns them in HOST project coordinates (mm, link transform applied): wall location "
                "lines and thickness, floor/room/shaft boundary loops, column/door/fixture points, and bounding boxes. "
                "Use host_level_name to keep only elements between that host level and the next one up; z_mm is then "
                "relative to that level. Read-only."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "link_name_contains": {"type": "string", "description": "Filter links by name (case-insensitive)."},
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": LINK_CATEGORIES},
                        "description": "Default: walls, floors, structural_columns, columns, doors, rooms, shaft_openings.",
                    },
                    "host_level_name": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max elements returned. Default 500."},
                },
            },
            handler=get_linked_elements_handler,
        ),
        ToolDefinition(
            name="get_mep_connectors",
            description=(
                "Lists the piping connectors of fixtures, fittings, equipment and pipes: connector_id, position "
                "(x_mm, y_mm, z_mm above the element's level, z_mm_abs), direction (points OUT of the element), "
                "system classification (Sanitary, DomesticColdWater...), diameter_mm, and what it is connected to. "
                "Use it after placing fixtures to find the drain outlet before routing pipes. Read-only."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "element_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["element_ids"],
            },
            handler=get_mep_connectors_handler,
        ),
        ToolDefinition(
            name="place_pipe_fittings",
            description=(
                "Places a pipe-fitting family (e.g. a trap / 存水彎) onto an OPEN connector of a fixture, pipe end or "
                "another fitting: sizes it (diameter_mm, default = target connector size), turns its inlet to face the "
                "target connector, optionally spins it so the outlet points along outlet_direction {x, y, z}, moves it "
                "onto the connector and connects it. Returns the outlet connector(s) so a pipe can start from it with "
                "create_pipes start_connect. Use the trap family that matches the piping system (SP vs WP). "
                "One transaction per call; supports dry_run."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "fittings": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Each item: {connect_to: {element_id, connector_id?}, family_name?, type_name?, "
                            "diameter_mm?, outlet_direction?: {x, y, z}, inlet_connector_id?}. Without connector_id "
                            "the first unconnected piping connector is used."
                        ),
                    },
                    "family_name": {"type": "string", "description": "Fitting family. type_name may be omitted if it has one type."},
                    "type_name": {"type": "string"},
                    "diameter_mm": {"type": "number"},
                    "outlet_direction": {"type": "object", "description": "Default outlet direction {x, y, z}."},
                    **_COMMON,
                },
                "required": ["fittings"],
            },
            handler=place_pipe_fittings_handler,
        ),
    ]
