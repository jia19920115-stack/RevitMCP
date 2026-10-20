# RevitMCP: automatic handling of join failures
# -*- coding: UTF-8 -*-
"""
Revit raises an error such as "Can't cut joined element" / "Cannot keep
elements joined" (無法切割接合元素 / 無法保持元素接合) when two joined
elements no longer overlap or can't cut each other. Unhandled, it pops a
modal dialog (which hangs every later MCP request) or, inside an MCP
transaction, rolls the whole batch back.

This module resolves those failures with Revit's own "Unjoin Elements"
resolution, so only the offending pair is left unjoined and the rest of the
operation commits. It is used by:
- the failure preprocessors of the MCP routes (see modeling_routes and
  element_operation_routes), and
- an application-level FailuresProcessing handler registered from
  startup.py, which covers the same failures during manual work in Revit.
"""

from pyrevit import DB

from routes.revit_compat import get_element_id_value


_JOIN_TEXT_KEYS = (u"join", u"cut", u"接合", u"切割", u"結合")

# Only "Unjoin Elements" is applied: it never deletes or moves anything, so an
# error that offers only other resolutions (e.g. Delete) still rolls back.
_RESOLUTION_NAMES = ("UnjoinElements",)

_join_failure_guids = None


def _join_guids():
    """GUIDs of every failure definition under BuiltInFailures.JoinElementsFailures."""
    global _join_failure_guids
    if _join_failure_guids is None:
        guids = set()
        holder = getattr(DB.BuiltInFailures, "JoinElementsFailures", None)
        if holder is not None:
            for name in dir(holder):
                if name.startswith("_"):
                    continue
                try:
                    value = getattr(holder, name)
                    guid = getattr(value, "Guid", None)
                    if guid is not None:
                        guids.add(str(guid))
                except Exception:
                    pass
        _join_failure_guids = guids
    return _join_failure_guids


def _description(failure):
    try:
        return u"{}".format(failure.GetDescriptionText())
    except Exception:
        return u""


def is_join_failure(failure):
    """True for the "can't cut / can't keep joined" family of failures."""
    try:
        if str(failure.GetFailureDefinitionId().Guid) in _join_guids():
            return True
    except Exception:
        pass
    text = _description(failure).lower()
    return any(key in text for key in _JOIN_TEXT_KEYS)


def failing_element_ids(failure):
    ids = []
    try:
        for element_id in failure.GetFailingElementIds():
            ids.append(get_element_id_value(element_id))
    except Exception:
        pass
    try:
        for element_id in failure.GetAdditionalElementIds():
            value = get_element_id_value(element_id)
            if value not in ids:
                ids.append(value)
    except Exception:
        pass
    return ids


def try_resolve_join_failure(failures_accessor, failure):
    """
    Resolve one join failure with "Unjoin Elements". Returns a dict describing what was done, or None when the
    failure is not a join failure or has no usable resolution.
    """
    if not is_join_failure(failure):
        return None
    if failure.GetSeverity() == DB.FailureSeverity.Warning:
        failures_accessor.DeleteWarning(failure)
        return {"message": _description(failure), "element_ids": failing_element_ids(failure),
                "resolution": "dismissed_warning"}
    try:
        if not failure.HasResolutions():
            return None
    except Exception:
        return None
    chosen = None
    for name in _RESOLUTION_NAMES:
        resolution_type = getattr(DB.FailureResolutionType, name, None)
        if resolution_type is None:
            continue
        try:
            if failure.HasResolutionOfType(resolution_type):
                failure.SetCurrentResolutionType(resolution_type)
                chosen = name
                break
        except Exception:
            pass
    if chosen is None:
        return None
    info = {"message": _description(failure), "element_ids": failing_element_ids(failure),
            "resolution": chosen}
    try:
        failures_accessor.ResolveFailure(failure)
    except Exception:
        return None
    return info


# ---------------------------------------------------------------------------
# Application-level handler (manual work in Revit)
# ---------------------------------------------------------------------------

_APPDOMAIN_KEY = "RevitMCP.JoinFailureHandler"


def _on_failures_processing(sender, args):
    try:
        accessor = args.GetFailuresAccessor()
        resolved = False
        for failure in list(accessor.GetFailureMessages()):
            try:
                if failure.GetSeverity() == DB.FailureSeverity.Warning:
                    continue
                if try_resolve_join_failure(accessor, failure) is not None:
                    resolved = True
            except Exception:
                pass
        if resolved:
            args.SetProcessingResult(DB.FailureProcessingResult.ProceedWithCommit)
    except Exception:
        pass


def register_application_handler(application, logger=None):
    """
    Subscribe the join-failure handler to Application.FailuresProcessing.
    A handler left by a previous pyRevit reload is unsubscribed first, so
    reloading never stacks handlers.
    """
    import System
    from Autodesk.Revit.DB.Events import FailuresProcessingEventArgs

    domain = System.AppDomain.CurrentDomain
    previous = domain.GetData(_APPDOMAIN_KEY)
    if previous is not None:
        try:
            application.FailuresProcessing -= previous
        except Exception:
            pass
    handler = System.EventHandler[FailuresProcessingEventArgs](_on_failures_processing)
    application.FailuresProcessing += handler
    domain.SetData(_APPDOMAIN_KEY, handler)
    if logger is not None:
        logger.info("RevitMCP join-failure handler registered (auto Unjoin for can't-cut errors).")
    return handler
