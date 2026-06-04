#!/usr/bin/env python3
"""
nodeset_to_diagram.py — Convert an OPC UA NodeSet2 XML file to a Mermaid class diagram.

Usage:
    python nodeset_to_diagram.py <nodeset.xml>
    python nodeset_to_diagram.py <nodeset.xml> -o diagram.md
    python nodeset_to_diagram.py <nodeset.xml> --include-ns 1 2
"""

import sys
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Optional

UA_NS = "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"


def qtag(name: str) -> str:
    return f"{{{UA_NS}}}{name}"


# ── Canonical OPC UA reference / node IDs (ns=0) ─────────────────────────────
HAS_SUBTYPE       = "i=45"
HAS_COMPONENT     = "i=47"
HAS_PROPERTY      = "i=46"
HAS_INTERFACE     = "i=17603"
HAS_TYPEDEF       = "i=40"
HAS_MODELLING_RULE = "i=37"
ORGANIZES         = "i=35"

BASE_OBJECT_TYPE  = "i=58"
BASE_IFACE_TYPE   = "i=17602"
FOLDER_TYPE       = "i=61"

MANDATORY         = "i=78"
OPTIONAL_RULE     = "i=80"
OPT_PLACEHOLDER   = "i=11508"

SKIP_SUPERTYPES   = {BASE_OBJECT_TYPE, BASE_IFACE_TYPE}

# ── Built-in alias resolution (supplements what the file declares) ────────────
BUILTIN_ALIASES: dict[str, str] = {
    "HasSubtype": HAS_SUBTYPE,
    "HasComponent": HAS_COMPONENT,
    "HasProperty": HAS_PROPERTY,
    "HasInterface": HAS_INTERFACE,
    "HasTypeDefinition": HAS_TYPEDEF,
    "HasModellingRule": HAS_MODELLING_RULE,
    "Organizes": ORGANIZES,
    "Mandatory": MANDATORY,
    "Optional": OPTIONAL_RULE,
    "OptionalPlaceholder": OPT_PLACEHOLDER,
    "BaseObjectType": BASE_OBJECT_TYPE,
    "BaseInterfaceType": BASE_IFACE_TYPE,
    "BaseDataVariableType": "i=63",
    "PropertyType": "i=68",
    "AnalogItemType": "i=2368",
    "FolderType": FOLDER_TYPE,
    # scalar data types
    "Boolean": "i=1", "SByte": "i=2", "Byte": "i=3",
    "Int16": "i=4", "UInt16": "i=5", "Int32": "i=6", "UInt32": "i=7",
    "Int64": "i=8", "UInt64": "i=9", "Float": "i=10", "Double": "i=11",
    "String": "i=12", "DateTime": "i=13", "Guid": "i=14",
    "ByteString": "i=15", "LocalizedText": "i=21",
    "NetworkAddressDataType": "i=15502",
}

DATA_TYPE_NAMES: dict[str, str] = {
    "i=1": "Boolean", "i=2": "SByte", "i=3": "Byte",
    "i=4": "Int16", "i=5": "UInt16", "i=6": "Int32", "i=7": "UInt32",
    "i=8": "Int64", "i=9": "UInt64", "i=10": "Float", "i=11": "Double",
    "i=12": "String", "i=13": "DateTime", "i=14": "Guid",
    "i=15": "ByteString", "i=21": "LocalizedText",
    "i=15502": "NetworkAddressDataType",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def node_ns_index(node_id: str) -> int:
    """Return the namespace index from a node ID string (0 if no ns= prefix)."""
    if node_id.startswith("ns="):
        try:
            return int(node_id.split(";", 1)[0][3:])
        except ValueError:
            pass
    return 0


def strip_ns_prefix(browse_name: str) -> str:
    """'1:MotorType' → 'MotorType'"""
    return browse_name.split(":", 1)[-1] if ":" in browse_name else browse_name


def safe_id(name: str) -> str:
    """Make a name safe for use as a Mermaid class identifier."""
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    return "".join(out)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse(path: str, target_ns: Optional[set[int]] = None) -> tuple[list[dict], set[str]]:
    """
    Parse a NodeSet2 XML file and return:
      types  — list of type-description dicts for local UAObjectType nodes
      stubs  — set of external type names that need a stub class in the diagram
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # 1. Alias resolution map ─────────────────────────────────────────────────
    aliases: dict[str, str] = dict(BUILTIN_ALIASES)
    for el in root.findall(f"{qtag('Aliases')}/{qtag('Alias')}"):
        if el.get("Alias") and el.text:
            aliases[el.get("Alias")] = el.text.strip()

    def resolve(text: str) -> str:
        t = (text or "").strip()
        return aliases.get(t, t)

    # 2. Index every node ─────────────────────────────────────────────────────
    elements: dict[str, ET.Element] = {}
    node_class: dict[str, str] = {}

    for cls in ("UAObjectType", "UAVariable", "UAObject", "UADataType", "UAReferenceType"):
        for el in root.findall(qtag(cls)):
            nid = el.get("NodeId", "")
            if nid:
                elements[nid] = el
                node_class[nid] = cls

    # 3. Helper closures ──────────────────────────────────────────────────────
    def display_name(el: ET.Element) -> str:
        dn = el.find(qtag("DisplayName"))
        if dn is not None and dn.text:
            return dn.text.strip()
        return strip_ns_prefix(el.get("BrowseName", ""))

    def description(el: ET.Element) -> str:
        desc = el.find(qtag("Description"))
        return (desc.text or "").strip() if desc is not None else ""

    def get_refs(el: ET.Element) -> list[tuple[str, str, bool]]:
        """(ref_type_id, target_id, is_forward) for every Reference child."""
        out = []
        refs_el = el.find(qtag("References"))
        if refs_el is None:
            return out
        for ref in refs_el.findall(qtag("Reference")):
            rt = resolve(ref.get("ReferenceType", ""))
            target = resolve((ref.text or "").strip())
            is_fwd = ref.get("IsForward", "true").lower() != "false"
            out.append((rt, target, is_fwd))
        return out

    def first_ref(refs: list, ref_type: str, is_forward: bool = True) -> Optional[str]:
        for rt, t, fwd in refs:
            if rt == ref_type and fwd == is_forward:
                return t
        return None

    def all_refs(refs: list, ref_type: str, is_forward: bool = True) -> list[str]:
        return [t for rt, t, fwd in refs if rt == ref_type and fwd == is_forward]

    def type_name_for(node_id: str) -> str:
        """Best-effort human-readable name for any node ID."""
        if node_id in DATA_TYPE_NAMES:
            return DATA_TYPE_NAMES[node_id]
        if node_id in elements:
            return display_name(elements[node_id])
        # reverse-lookup through aliases (catches externally declared types)
        for alias, target in aliases.items():
            if target == node_id and alias[:1].isupper():
                return alias
        return strip_ns_prefix(node_id)

    def dt_name(dt_attr: str) -> str:
        if not dt_attr:
            return ""
        resolved = resolve(dt_attr)
        return DATA_TYPE_NAMES.get(resolved, type_name_for(resolved))

    def modelling_rule(refs: list) -> str:
        mr = first_ref(refs, HAS_MODELLING_RULE)
        return {MANDATORY: "Mandatory", OPTIONAL_RULE: "Optional",
                OPT_PLACEHOLDER: "OptionalPlaceholder"}.get(mr or "", "")

    def multiplicity(rule: str) -> str:
        return {"Mandatory": "1", "Optional": "0..1",
                "OptionalPlaceholder": "0..*"}.get(rule, "0..1")

    # 4. Determine which namespaces count as "local" ──────────────────────────
    ns_uris = [el.text.strip()
               for el in root.findall(f"{qtag('NamespaceUris')}/{qtag('Uri')}")]
    if target_ns is None:
        # default: everything that isn't the OPC UA base namespace (ns=0)
        target_ns = set(range(1, len(ns_uris) + 1))

    def is_local(node_id: str) -> bool:
        return node_ns_index(node_id) in target_ns

    # 5. Index children by ParentNodeId ───────────────────────────────────────
    children_of: dict[str, list[str]] = defaultdict(list)
    for nid, el in elements.items():
        pid = el.get("ParentNodeId", "")
        if pid:
            children_of[pid].append(nid)

    # 6. Collect local UAObjectTypes ──────────────────────────────────────────
    local_types = sorted(
        [nid for nid, el in elements.items()
         if node_class.get(nid) == "UAObjectType" and is_local(nid)],
        key=lambda x: elements[x].get("BrowseName", ""),
    )

    all_type_ids = {nid for nid in elements if node_class.get(nid) == "UAObjectType"}

    # 7. Build per-type descriptor dicts ──────────────────────────────────────
    types: list[dict] = []
    stubs: set[str] = set()

    for nid in local_types:
        el = elements[nid]
        refs = get_refs(el)
        is_abstract = el.get("IsAbstract", "false").lower() == "true"

        # Determine stereotype
        supertype_id = first_ref(refs, HAS_SUBTYPE, is_forward=False) or ""
        if supertype_id == BASE_IFACE_TYPE:
            stereotype = "interface"
        elif is_abstract:
            stereotype = "abstract"
        else:
            stereotype = ""

        # Parent type for inheritance arrow (skip OPC UA roots)
        parent_id = supertype_id if supertype_id not in SKIP_SUPERTYPES else ""
        parent_name = type_name_for(parent_id) if parent_id else ""
        if parent_id and not is_local(parent_id) and parent_name:
            stubs.add(parent_name)

        # Implemented interfaces
        iface_ids = all_refs(refs, HAS_INTERFACE)
        iface_names = [type_name_for(iid) for iid in iface_ids]
        for name in iface_names:
            stubs.add(name)

        # ── Attributes (UAVariable children) ─────────────────────────────────
        # Determine which children are HasProperty vs HasComponent from parent
        prop_children = set(all_refs(refs, HAS_PROPERTY))
        comp_children = set(all_refs(refs, HAS_COMPONENT))

        attributes: list[dict] = []
        for child_id in children_of.get(nid, []):
            if node_class.get(child_id) != "UAVariable":
                continue
            child_el = elements[child_id]
            child_refs = get_refs(child_el)
            attributes.append({
                "name": display_name(child_el),
                "type": dt_name(child_el.get("DataType", "")) or "Variant",
                "is_property": child_id in prop_children,
                "rule": modelling_rule(child_refs),
            })

        # ── Compositions (UAObject children) ─────────────────────────────────
        compositions: list[dict] = []
        for child_id in children_of.get(nid, []):
            if node_class.get(child_id) != "UAObject":
                continue
            child_el = elements[child_id]
            child_refs = get_refs(child_el)
            child_name = display_name(child_el)
            typedef_id = first_ref(child_refs, HAS_TYPEDEF) or ""
            rule = modelling_rule(child_refs)

            if typedef_id == FOLDER_TYPE:
                # Folder — inspect what it organizes for the real target type
                for org_id in all_refs(child_refs, ORGANIZES):
                    if org_id not in elements:
                        continue
                    org_refs = get_refs(elements[org_id])
                    org_typedef = first_ref(org_refs, HAS_TYPEDEF) or ""
                    org_rule = modelling_rule(org_refs)
                    if org_typedef and org_typedef in all_type_ids:
                        tname = type_name_for(org_typedef)
                        compositions.append({
                            "role": child_name,
                            "target": tname,
                            "multiplicity": multiplicity(org_rule),
                        })
                        if not is_local(org_typedef):
                            stubs.add(tname)
            elif typedef_id and typedef_id in all_type_ids:
                tname = type_name_for(typedef_id)
                compositions.append({
                    "role": child_name,
                    "target": tname,
                    "multiplicity": multiplicity(rule),
                })
                if not is_local(typedef_id):
                    stubs.add(tname)

        types.append({
            "node_id": nid,
            "name": display_name(el),
            "description": description(el),
            "stereotype": stereotype,
            "parent_name": parent_name,
            "iface_names": iface_names,
            "attributes": attributes,
            "compositions": compositions,
        })

    return types, stubs


# ── Mermaid generation ────────────────────────────────────────────────────────

def generate(types: list[dict], stubs: set[str]) -> str:
    lines = ["classDiagram"]

    # Sort: interfaces/abstracts first, then alphabetically by name
    ordered = sorted(types, key=lambda t: (t["stereotype"] not in ("interface", "abstract"), t["name"]))

    # ── Class definitions ─────────────────────────────────────────────────────
    for t in ordered:
        cid = safe_id(t["name"])
        lines.append(f"    class {cid} {{")
        if t["stereotype"]:
            lines.append(f"        <<{t['stereotype']}>>")
        for attr in t["attributes"]:
            lines.append(f"        +{attr['name']} : {attr['type']}")
        lines.append("    }")

    # External stubs (remove any that ended up being local)
    local_names = {t["name"] for t in types}
    for stub in sorted(stubs - local_names):
        lines.append(f"    class {safe_id(stub)} {{")
        lines.append("        <<external>>")
        lines.append("    }")

    lines.append("")

    # ── Relationships ─────────────────────────────────────────────────────────
    for t in ordered:
        cid = safe_id(t["name"])

        if t["parent_name"]:
            lines.append(f"    {cid} --|> {safe_id(t['parent_name'])} : extends")

        for iface in t["iface_names"]:
            lines.append(f"    {cid} ..|> {safe_id(iface)} : implements")

        for comp in t["compositions"]:
            tid = safe_id(comp["target"])
            mult = comp["multiplicity"]
            role = comp["role"]
            lines.append(f'    {cid} "1" *-- "{mult}" {tid} : {role}')

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert an OPC UA NodeSet2 XML file to a Mermaid class diagram."
    )
    parser.add_argument("nodeset", help="Path to the NodeSet2 XML file")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Write diagram to FILE (.md wraps in a code block)")
    parser.add_argument("--include-ns", nargs="+", type=int, metavar="N",
                        help="Namespace indices to include (default: all non-zero)")
    args = parser.parse_args()

    try:
        types, stubs = parse(args.nodeset, set(args.include_ns) if args.include_ns else None)
    except FileNotFoundError:
        sys.exit(f"error: file not found: {args.nodeset}")
    except ET.ParseError as exc:
        sys.exit(f"error: invalid XML: {exc}")

    if not types:
        sys.exit("error: no UAObjectType nodes found in the specified namespace(s)")

    diagram = generate(types, stubs)

    if args.output:
        content = f"```mermaid\n{diagram}\n```\n" if args.output.endswith(".md") else diagram
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Wrote {len(types)} types → {args.output}", file=sys.stderr)
    else:
        print(diagram)


if __name__ == "__main__":
    main()
