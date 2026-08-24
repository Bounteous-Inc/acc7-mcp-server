"""SOAP envelope builders.

Shapes verified against the live instance on 2026-08-06. Note that
`GetEntityIfMoreRecent` lives on `xtk:persist` (not `xtk:session`) and takes
`strPk` / `strMd5` / `bMustExist` — see my-ref.md section 5.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

# Attribute values need quotes escaped too, since expressions carry them:
#   <condition expr="@namespace = 'cus'"/>
ENTITIES = {'"': "&quot;", "'": "&apos;"}

# Adobe's reserved namespaces. Anything else is a customer-built schema.
RESERVED_NAMESPACES = ("xtk", "nl", "nms", "ncm", "temp", "ncl", "crm", "xxl")

_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xmlns:ns="http://xml.apache.org/xml-soap"
                   xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
{body}
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

_ENCODING_STYLE = "http://schemas.xmlsoap.org/soap/encoding/"
_LITERAL_XML = "http://xml.apache.org/xml-soap/literalxml"


def _wrap(body: str) -> str:
    return _ENVELOPE.format(body=body)


def build_logon(login: str, password: str) -> str:
    """`xtk:session#Logon`. Leading <sessiontoken> is empty by design."""
    body = f"""    <Logon xmlns="urn:xtk:session" SOAP-ENV:encodingStyle="{_ENCODING_STYLE}">
      <sessiontoken xsi:type="xsd:string"></sessiontoken>
      <strLogin xsi:type="xsd:string">{escape(login)}</strLogin>
      <strPassword xsi:type="xsd:string">{escape(password)}</strPassword>
      <elemParameters xsi:type="ns:Element" SOAP-ENV:encodingStyle="{_LITERAL_XML}"/>
    </Logon>"""
    return _wrap(body)


def build_execute_query(
    session_token: str,
    schema: str,
    fields: list[str],
    where: str | None = None,
    order_by: list[str] | None = None,
    line_count: int | None = None,
    start_line: int | None = None,
    operation: str = "select",
) -> str:
    """`xtk:queryDef#ExecuteQuery`. `operation` is "select" or "count".

    An `orderBy` should always be supplied for paged reads — offset paging
    without a deterministic sort silently skips or duplicates rows. Without
    `line_count`, ACC caps the result at 10,000 rows.
    """
    attributes = [
        f'operation="{escape(operation)}"',
        f'schema="{escape(schema)}"',
        'xtkschema="xtk:queryDef"',
    ]
    if line_count is not None:
        attributes.append(f'lineCount="{int(line_count)}"')
    if start_line is not None:
        attributes.append(f'startLine="{int(start_line)}"')

    parts: list[str] = []
    if fields:
        nodes = "".join(f'<node expr="{escape(field, ENTITIES)}"/>' for field in fields)
        parts.append(f"<select>{nodes}</select>")
    if where:
        parts.append(f'<where><condition expr="{escape(where, ENTITIES)}"/></where>')
    if order_by:
        nodes = "".join(f'<node expr="{escape(node, ENTITIES)}"/>' for node in order_by)
        parts.append(f"<orderBy>{nodes}</orderBy>")

    query_def = f'<queryDef {" ".join(attributes)}>{"".join(parts)}</queryDef>'
    body = f"""    <ExecuteQuery xmlns="urn:xtk:queryDef" SOAP-ENV:encodingStyle="{_ENCODING_STYLE}">
      <sessiontoken xsi:type="xsd:string">{escape(session_token)}</sessiontoken>
      <entity xsi:type="ns:Element" SOAP-ENV:encodingStyle="{_LITERAL_XML}">
        {query_def}
      </entity>
    </ExecuteQuery>"""
    return _wrap(body)


def build_get_entity(
    session_token: str,
    pk: str,
    md5: str = "",
    must_exist: bool = True,
) -> str:
    """`xtk:persist#GetEntityIfMoreRecent`."""
    raise NotImplementedError
