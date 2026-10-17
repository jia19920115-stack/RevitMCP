"""MCP tools for materials.

Revit-side implementation: lib/routes/material_routes.py. All lengths are millimetres.
"""

from RevitMCP_ExternalServer.tools.registry import ToolDefinition


CREATE_TEXTURED_MATERIAL_TOOL_NAME = "create_textured_material"


def create_textured_material_handler(
    services,
    name: str = None,
    image_path: str = None,
    width_mm: float = None,
    height_mm: float = None,
    rotation_deg: float = None,
    shading_color: dict = None,
    material_class: str = None,
    description: str = None,
    overwrite: bool = None,
    **_kwargs,
) -> dict:
    payload = {
        "name": name,
        "image_path": image_path,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "rotation_deg": rotation_deg,
        "shading_color": shading_color,
        "material_class": material_class,
        "description": description,
        "overwrite": overwrite,
    }
    clean_payload = {key: value for key, value in payload.items() if value is not None}
    services.logger.info("MCP Tool executed: %s (%s)", CREATE_TEXTURED_MATERIAL_TOOL_NAME, name)
    result = services.revit_client.call_listener(
        command_path="/materials/create_textured",
        method="POST",
        payload_data=clean_payload,
    )
    if not isinstance(result, dict):
        return {"status": "error", "message": "Unexpected response from Revit.", "raw": str(result)[:500]}
    return result


def build_material_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=CREATE_TEXTURED_MATERIAL_TOOL_NAME,
            description=(
                "Creates (or with overwrite=true updates) a Revit material whose appearance is a Generic asset "
                "using an image file as the diffuse texture. width_mm/height_mm are the real-world size of ONE "
                "copy of the image (e.g. one tile), so the texture keeps its proportions. image_path is a path "
                "on the computer running Revit."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Material name."},
                    "image_path": {"type": "string", "description": "Absolute path of the texture image on the Revit computer."},
                    "width_mm": {"type": "number", "description": "Real-world width of the image (texture X), mm."},
                    "height_mm": {"type": "number", "description": "Real-world height of the image (texture Y), mm."},
                    "rotation_deg": {"type": "number", "description": "Texture rotation in degrees. Default 0."},
                    "shading_color": {
                        "type": "object",
                        "properties": {"r": {"type": "integer"}, "g": {"type": "integer"}, "b": {"type": "integer"}},
                        "description": "Optional graphics shading color; when omitted the render appearance is used for shading.",
                    },
                    "material_class": {"type": "string", "description": "Optional material class, e.g. Ceramic."},
                    "description": {"type": "string"},
                    "overwrite": {"type": "boolean", "description": "Update the material if it already exists. Default false."},
                },
                "required": ["name", "image_path", "width_mm", "height_mm"],
            },
            handler=create_textured_material_handler,
        ),
    ]
