"""把 OpenAI / JSON Schema 形式的工具参数降级成 Gemini Schema 能接受的子集。

Gemini 的 `google.protobuf.Schema` 只认识有限的一批字段（type/format/description/
properties/required/items/enum/anyOf/...）。JSON Schema 方言里的
`additionalProperties`、`$schema`、`$ref`、`oneOf`、`const` 之类字段发过去，上游会直接
400：

    Invalid JSON payload received. Unknown name "additionalProperties" at
    'tools[0].function_declarations[3].parameters...': Cannot find field.

hajimi 原版把 `parameters` 原样透传（只删了 `$schema`），所以任何带函数调用的
客户端（DSH、Cursor、Cline 等）都会整轮失败。这里做一次递归白名单化。
"""

from typing import Any

# Gemini Schema 支持的键。protobuf JSON 解析对 camelCase / snake_case 都接受，
# 这里统一按原样保留 camelCase。
_SUPPORTED_KEYS = frozenset(
    {
        "type",
        "format",
        "title",
        "description",
        "nullable",
        "default",
        "items",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
        "pattern",
        "enum",
        "properties",
        "required",
        "example",
        "anyOf",
        "propertyOrdering",
    }
)

# Gemini 接受的 format 取值；其余（比如 JSON Schema 的 "uri"、"email"）会让上游报
# 未知枚举值，直接丢弃。
_SUPPORTED_FORMATS = frozenset(
    {
        "enum",
        "date-time",
        "int32",
        "int64",
        "float",
        "double",
        "byte",
        "time",
        "duration",
        "date",
    }
)

_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})


def _merge_all_of(node: dict) -> list:
    """把 allOf 里的对象分支合并成 (properties, required) 两个集合。"""
    properties: dict = {}
    required: list = []
    for branch in node.get("allOf") or []:
        if not isinstance(branch, dict):
            continue
        if isinstance(branch.get("properties"), dict):
            properties.update(branch["properties"])
        if isinstance(branch.get("required"), list):
            required.extend(branch["required"])
    return [properties, required]


def sanitize_gemini_schema(node: Any) -> Any:
    """递归返回只含 Gemini 支持字段的新结构；非法/未知字段一律丢弃。"""
    # JSON Schema 允许布尔 schema（true = 任意值，false = 不允许），Gemini 不接受。
    if isinstance(node, bool):
        return {}
    if isinstance(node, list):
        return [sanitize_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict = {}

    # const -> enum。Gemini 的 enum 只支持字符串取值，非字符串的 const 直接丢弃。
    if "const" in node and "enum" not in node and isinstance(node["const"], str):
        out["enum"] = [node["const"]]

    # type 允许 ["string", "null"] 这种联合写法，Gemini 只接受单一类型。
    raw_type = node.get("type")
    if isinstance(raw_type, list):
        non_null = [t for t in raw_type if isinstance(t, str) and t != "null"]
        if non_null:
            out["type"] = non_null[0]
        if "null" in raw_type:
            out["nullable"] = True
    elif isinstance(raw_type, str):
        out["type"] = raw_type

    # allOf 不支持，拍平到 properties / required。
    if isinstance(node.get("allOf"), list):
        merged_properties, merged_required = _merge_all_of(node)
        if merged_properties:
            node = {**node, "properties": {**(node.get("properties") or {}), **merged_properties}}
        if merged_required:
            node = {**node, "required": list(node.get("required") or []) + merged_required}

    for key, value in node.items():
        if key not in _SUPPORTED_KEYS:
            continue
        if key in ("type", "nullable"):
            continue  # 上面已经处理
        if key == "format":
            if isinstance(value, str) and value in _SUPPORTED_FORMATS:
                out[key] = value
            continue
        if key == "properties":
            if isinstance(value, dict):
                cleaned = {
                    name: sanitize_gemini_schema(schema)
                    for name, schema in value.items()
                    if isinstance(name, str)
                }
                if cleaned:
                    out[key] = cleaned
            continue
        if key == "required":
            if isinstance(value, list):
                kept = [name for name in value if isinstance(name, str)]
                if kept:
                    out[key] = kept
            continue
        if key in ("items", "anyOf"):
            out[key] = sanitize_gemini_schema(value)
            continue
        if key == "enum":
            # Gemini 的 enum 只允许字符串取值，数字/布尔枚举会让上游 400。
            if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                out[key] = value
            continue
        out[key] = sanitize_gemini_schema(value) if isinstance(value, (dict, list)) else value

    # Gemini 只有 anyOf，没有 oneOf。
    if "oneOf" in node and "anyOf" not in out:
        out["anyOf"] = sanitize_gemini_schema(node["oneOf"])

    # anyOf 里的 {"type": "null"} 分支等价于 nullable，Gemini 的 union 类型支持有限，
    # 改成父级 nullable 更稳。
    if isinstance(out.get("anyOf"), list):
        branches = [
            branch
            for branch in out["anyOf"]
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        saw_null = len(branches) != len(out["anyOf"])
        if branches:
            out["anyOf"] = branches
            if saw_null:
                out["nullable"] = True
        else:
            out.pop("anyOf")
            out["type"] = "null"

    # 有 properties 却没写 type 的，补成 object，否则上游会当成缺省类型。
    if "type" not in out and isinstance(out.get("properties"), dict):
        out["type"] = "object"

    # 去掉空壳（比如 required: []、properties: {}）已在上面的条件里处理。
    return out
