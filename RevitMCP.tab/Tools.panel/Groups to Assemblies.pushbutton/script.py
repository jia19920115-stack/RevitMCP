# RevitMCP: This script runs in pyRevit (IronPython). Use Python 2.7 syntax (no f-strings).
#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""Ungroup model groups whose name starts with a prefix and rebuild each as a same-named Assembly,
so an IFC export carries IfcElementAssembly (BIMvision 'Select group' picks the whole assembly)."""

__title__ = u"Groups to\nAssemblies"

import io
import os
import traceback
from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List

doc = revit.doc
output = script.get_output()

prefix = forms.ask_for_string(
    default=u"1F",
    prompt=u"Convert model groups whose name starts with (empty = ALL groups):",
    title=u"Groups to Assemblies",
)
if prefix is None:
    script.exit()
prefix = prefix.strip()

groups = [
    g for g in DB.FilteredElementCollector(doc)
    .OfCategory(DB.BuiltInCategory.OST_IOSModelGroups)
    .WhereElementIsNotElementType()
    if isinstance(g, DB.Group)
]
targets = [g for g in groups if g.Name.startswith(prefix)] if prefix else groups

if not targets:
    forms.alert(u"No model group matches '{}'.".format(prefix), title=u"Groups to Assemblies")
    script.exit()

if not forms.alert(
    u"{} group(s) will be ungrouped and rebuilt as assemblies.\n"
    u"Save the project as a TEST copy first; this cannot be undone except with Ctrl+Z.".format(len(targets)),
    title=u"Groups to Assemblies", yes=True, no=True,
):
    script.exit()

done = []
failed = []
skipped_log = []
used_names = set()


def _probe_ok(el):
    """True if this single element can be put into an assembly (trial create, rolled back)."""
    st = DB.SubTransaction(doc)
    st.Start()
    try:
        one = List[DB.ElementId]()
        one.Add(el.Id)
        DB.AssemblyInstance.Create(doc, one, el.Category.Id)
        return True
    except Exception:
        return False
    finally:
        st.RollBack()


for g in targets:
    name = g.Name
    gid = g.Id.IntegerValue if hasattr(g.Id, "IntegerValue") else g.Id.Value
    t = DB.Transaction(doc, u"Group to Assembly: {}".format(name))
    t.Start()
    try:
        members = list(g.UngroupMembers())
        doc.Regenerate()
        ids = List[DB.ElementId]()
        first = None
        skipped = []
        for mid in members:
            el = doc.GetElement(mid)
            if el is None or el.Category is None or not _probe_ok(el):
                skipped.append(u"{} {}".format(mid.IntegerValue if hasattr(mid, "IntegerValue") else mid.Value,
                                               el.Category.Name if (el is not None and el.Category is not None) else u"?"))
                continue
            ids.Add(mid)
            if first is None:
                first = el
        if first is None:
            raise Exception("no valid members")
        asm = DB.AssemblyInstance.Create(doc, ids, first.Category.Id)
        asm_id = asm.Id
        t.Commit()
    except Exception as ex:
        t.RollBack()
        failed.append((name, u"{} | {}".format(ex, traceback.format_exc())))
        continue
    final = name
    if final in used_names:
        final = u"{}_{}".format(name, gid)
    t2 = DB.Transaction(doc, u"Rename assembly: {}".format(name))
    t2.Start()
    try:
        doc.GetElement(asm_id).AssemblyTypeName = final
        t2.Commit()
        used_names.add(final)
    except Exception as ex:
        t2.RollBack()
        skipped.append(u"RENAME FAILED ({}); assembly keeps default name".format(ex))
    done.append((final, ids.Count))
    if skipped:
        skipped_log.append((final, skipped))

output.print_md(u"## Done: {}  Failed: {}".format(len(done), len(failed)))
for n, c in done:
    output.print_md(u"- {} ({} elements)".format(n, c))
for n, e in failed:
    output.print_md(u"- **FAILED** {}: {}".format(n, e))
for n, sk in skipped_log:
    output.print_md(u"- {} skipped {}: {}".format(n, len(sk), u"; ".join(sk[:20])))
log_path = os.path.join(os.path.expanduser("~"), "Desktop", "Groups_to_Assemblies_log.txt")
try:
    with io.open(log_path, "w", encoding="utf-8") as fh:
        fh.write(u"done={} failed={}\n".format(len(done), len(failed)))
        for n, c in done:
            fh.write(u"OK {} ({})\n".format(n, c))
        for n, sk in skipped_log:
            fh.write(u"SKIPPED {} ({}): {}\n".format(n, len(sk), u"; ".join(sk)))
        for n, e in failed:
            fh.write(u"FAILED {}: {}\n".format(n, e))
except Exception:
    pass

forms.alert(u"Assemblies created: {}\nFailed (rolled back): {}\nSee pyRevit output window for details.".format(len(done), len(failed)),
            title=u"Groups to Assemblies")

