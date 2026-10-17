# RevitMCP: Material HTTP routes (create/update a material with an image-textured appearance)
# -*- coding: UTF-8 -*-
#
# The texture size is the real-world size of ONE image (e.g. one tile), so the texture keeps its
# proportions on walls/floors. All lengths are millimetres.

import os

from pyrevit import routes, script, DB

from routes.json_safety import sanitize_for_json
from routes.revit_compat import get_element_id_text


MATERIAL_ROUTES_VERSION = "material-routes-v1"

try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


def _text(value):
    if value is None:
        return u""
    if isinstance(value, STRING_TYPES):
        return value.strip()
    return str(value).strip()


def _find_material(doc, name):
    for material in DB.FilteredElementCollector(doc).OfClass(DB.Material):
        if material.Name == name:
            return material
    return None


def _find_appearance_asset(doc, name):
    for asset_element in DB.FilteredElementCollector(doc).OfClass(DB.AppearanceAssetElement):
        if asset_element.Name == name:
            return asset_element
    return None


def _is_generic_asset(asset):
    try:
        return asset is not None and asset.FindByName("generic_diffuse") is not None
    except Exception:
        return False


def _new_generic_appearance_asset(doc, name):
    """Duplicate a Generic appearance asset already in the project, else take one from the library."""
    for asset_element in DB.FilteredElementCollector(doc).OfClass(DB.AppearanceAssetElement):
        try:
            if _is_generic_asset(asset_element.GetRenderingAsset()):
                return asset_element.Duplicate(name)
        except Exception:
            continue
    library_assets = doc.Application.GetAssets(DB.Visual.AssetType.Appearance)
    for asset in library_assets:
        if _is_generic_asset(asset):
            return DB.AppearanceAssetElement.Create(doc, name, asset)
    raise Exception("No Generic appearance asset found in the project or the Revit asset library.")


def _set_distance(prop, millimetres):
    unit_id = prop.GetUnitTypeId()
    prop.Value = DB.UnitUtils.Convert(float(millimetres), DB.UnitTypeId.Millimeters, unit_id)


def _set_texture(doc, asset_element, image_path, width_mm, height_mm, rotation_deg):
    with DB.Visual.AppearanceAssetEditScope(doc) as edit_scope:
        editable = edit_scope.Start(asset_element.Id)
        diffuse = editable.FindByName("generic_diffuse")
        if diffuse is None:
            raise Exception("Appearance asset has no generic_diffuse property.")
        if diffuse.NumberOfConnectedProperties == 0:
            diffuse.AddConnectedAsset("UnifiedBitmapSchema")
        bitmap = diffuse.GetSingleConnectedAsset()

        path_prop = bitmap.FindByName("unifiedbitmap_Bitmap")
        if not path_prop.IsValidValue(image_path):
            raise Exception(u"Revit rejected the image path: {}".format(image_path))
        path_prop.Value = image_path

        _set_distance(bitmap.FindByName("texture_RealWorldScaleX"), width_mm)
        _set_distance(bitmap.FindByName("texture_RealWorldScaleY"), height_mm)
        for offset_name in ("texture_RealWorldOffsetX", "texture_RealWorldOffsetY"):
            offset_prop = bitmap.FindByName(offset_name)
            if offset_prop is not None:
                _set_distance(offset_prop, 0.0)
        angle_prop = bitmap.FindByName("texture_WAngle")
        if angle_prop is not None:
            angle_prop.Value = float(rotation_deg)
        for tile_name in ("texture_URepeat", "texture_VRepeat"):
            tile_prop = bitmap.FindByName(tile_name)
            if tile_prop is not None:
                tile_prop.Value = True

        edit_scope.Commit(True)


def _color(value):
    if not isinstance(value, dict):
        return None
    try:
        return DB.Color(int(value.get("r", 0)), int(value.get("g", 0)), int(value.get("b", 0)))
    except Exception:
        return None


def _material_summary(material, asset_element):
    return {
        "material_id": get_element_id_text(material.Id),
        "material_name": material.Name,
        "appearance_asset_name": asset_element.Name if asset_element else None,
        "material_class": material.MaterialClass,
        "use_render_appearance_for_shading": material.UseRenderAppearanceForShading,
    }


def register_routes(api):

    @api.route('/materials/create_textured', methods=['POST'])
    def handle_create_textured_material(doc, request):
        route_logger = script.get_logger()
        try:
            payload = request.data if hasattr(request, 'data') else {}
            if not isinstance(payload, dict):
                return routes.Response(status=400, data={"status": "error", "error": "Invalid JSON payload"})

            name = _text(payload.get("name"))
            image_path = _text(payload.get("image_path"))
            try:
                width_mm = float(payload.get("width_mm"))
                height_mm = float(payload.get("height_mm"))
            except Exception:
                width_mm = height_mm = 0.0
            rotation_deg = float(payload.get("rotation_deg") or 0.0)
            overwrite = bool(payload.get("overwrite", False))

            if not name or not image_path or width_mm <= 0 or height_mm <= 0:
                return routes.Response(status=400, data={
                    "status": "error",
                    "error": "name, image_path, width_mm (>0) and height_mm (>0) are required.",
                })
            if not os.path.isfile(image_path):
                return routes.Response(status=400, data=sanitize_for_json({
                    "status": "error",
                    "error": u"Image file not found on this computer: {}".format(image_path),
                }))

            existing = _find_material(doc, name)
            if existing is not None and not overwrite:
                return routes.Response(status=409, data=sanitize_for_json({
                    "status": "error",
                    "error": u"Material '{}' already exists. Pass overwrite=true to update it.".format(name),
                }))

            transaction = DB.Transaction(doc, "RevitMCP create textured material")
            transaction.Start()
            try:
                material = existing
                if material is None:
                    material = doc.GetElement(DB.Material.Create(doc, name))

                asset_element = None
                if material.AppearanceAssetId != DB.ElementId.InvalidElementId:
                    current = doc.GetElement(material.AppearanceAssetId)
                    # only edit in place an asset owned by this material (same name); never a shared one
                    if current is not None and current.Name == name:
                        asset_element = current
                if asset_element is None:
                    asset_element = _find_appearance_asset(doc, name) or _new_generic_appearance_asset(doc, name)

                _set_texture(doc, asset_element, image_path, width_mm, height_mm, rotation_deg)
                material.AppearanceAssetId = asset_element.Id

                shading_color = _color(payload.get("shading_color"))
                if shading_color is not None:
                    material.Color = shading_color
                    material.UseRenderAppearanceForShading = False
                else:
                    material.UseRenderAppearanceForShading = True

                material_class = _text(payload.get("material_class"))
                if material_class:
                    material.MaterialClass = material_class
                description = _text(payload.get("description"))
                if description:
                    param = material.get_Parameter(DB.BuiltInParameter.ALL_MODEL_DESCRIPTION)
                    if param is not None and not param.IsReadOnly:
                        param.Set(description)

                transaction.Commit()
            except Exception:
                if transaction.HasStarted() and not transaction.HasEnded():
                    transaction.RollBack()
                raise

            result = {
                "status": "success",
                "version": MATERIAL_ROUTES_VERSION,
                "updated_existing": existing is not None,
                "image_path": image_path,
                "texture_width_mm": width_mm,
                "texture_height_mm": height_mm,
                "rotation_deg": rotation_deg,
            }
            result.update(_material_summary(material, asset_element))
            return sanitize_for_json(result)
        except Exception as e:
            route_logger.error("Error in /materials/create_textured: {}".format(e), exc_info=True)
            return routes.Response(status=500, data=sanitize_for_json({
                "status": "error",
                "error": "Could not create textured material.",
                "details": str(e),
            }))
