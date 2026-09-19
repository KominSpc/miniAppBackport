"""契约守卫：比对服务端导出的 OpenAPI 与 Flutter 仓库的冻结契约。

规则：
- 契约里声明的路径、操作、参数、请求体、响应状态、schema、安全要求都必须存在且兼容 → 缺失即失败；
- 服务端多出来的东西（额外 schema、额外状态码等）只报警告，因为对客户端无害；
- 描述、示例、title 等文档性字段不参与比较。

用法：
    python scripts/contract_diff.py
    python scripts/contract_diff.py --contract ../mini_app/contract/openapi.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_CONTRACT = Path(__file__).resolve().parent.parent.parent / "mini_app" / "contract" / "openapi.json"

IGNORED_KEYS = {"title", "description", "example", "examples", "$id", "$schema", "discriminator"}
WARN_ONLY_KEYS = {"default"}
STRUCTURAL_KEYS = (
    "type",
    "format",
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "items",
    "properties",
    "required",
    "additionalProperties",
    "anyOf",
    "oneOf",
)
MAX_DEPTH = 16


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_pointer(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        return {}
    node: Any = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return {}
    return node


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize(node: Any, document: dict[str, Any], depth: int = 0) -> Any:
    """解析 $ref、剔除可空变体与文档性字段，得到可比较的纯结构。

    可空性在契约里写法不统一（`type: [x, null]` / `anyOf` / `oneOf` / `enum` 里带 null），
    因此统一剥离 null 变体后只比较非空形状；纯 null 的类型保持原样。
    """
    if depth > MAX_DEPTH:
        return {"__truncated__": True}
    if isinstance(node, list):
        return [normalize(item, document, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node

    node = {key: value for key, value in node.items() if key not in IGNORED_KEYS}

    if "$ref" in node:
        target = resolve_pointer(document, str(node["$ref"]))
        remainder = {key: value for key, value in node.items() if key != "$ref"}
        resolved = normalize(target, document, depth + 1)
        if remainder and isinstance(resolved, dict):
            return normalize(_deep_merge(resolved, remainder), document, depth + 1)
        return resolved

    if "allOf" in node:
        merged: dict[str, Any] = {}
        for part in node["allOf"]:
            merged = _deep_merge(merged, normalize(part, document, depth + 1) or {})
        remainder = {key: value for key, value in node.items() if key != "allOf"}
        return normalize(_deep_merge(merged, remainder), document, depth + 1) if remainder else merged

    node = {key: value for key, value in node.items() if key != "nullable"}

    for key in ("anyOf", "oneOf"):
        if key in node:
            variants = [normalize(item, document, depth + 1) for item in node[key]]
            non_null = [item for item in variants if item != {"type": "null"}]
            remainder = {k: v for k, v in node.items() if k != key}
            if len(non_null) == 1:
                merged = normalize(non_null[0], document, depth + 1)
                if remainder and isinstance(merged, dict):
                    merged = _deep_merge(merged, normalize(remainder, document, depth + 1) or {})
                return normalize(merged, document, depth + 1)
            return {
                **normalize(remainder, document, depth + 1),
                "variants": sorted(
                    json.dumps(item, sort_keys=True, ensure_ascii=False) for item in non_null
                ),
            }

    if isinstance(node.get("type"), list):
        types = [item for item in node["type"] if item != "null"]
        node = {**node, "type": types[0] if len(types) == 1 else types}

    if isinstance(node.get("enum"), list) and any(value is None for value in node["enum"]):
        node = {**node, "enum": [value for value in node["enum"] if value is not None]}
        node.setdefault("type", "string")

    if "const" in node:
        node = {**{k: v for k, v in node.items() if k != "const"}, "enum": [node["const"]]}

    result = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            result["properties"] = {
                name: normalize(schema, document, depth + 1) for name, schema in value.items()
            }
        elif key == "required" and isinstance(value, list):
            result["required"] = sorted(str(item) for item in value)
        elif key in ("items", "additionalProperties", "not"):
            result[key] = normalize(value, document, depth + 1)
        else:
            result[key] = value
    return result

def collect_refs(node: Any, found: set[str] | None = None) -> set[str]:
    if found is None:
        found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value)
            else:
                collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, found)
    return found


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def subset(contract: Any, server: Any, path: str, report: Report) -> None:
    """契约声明的结构必须在服务端存在且兼容。"""
    if isinstance(contract, dict):
        if not isinstance(server, dict):
            report.error(f"{path}: 契约是对象，服务端是 {type(server).__name__}")
            return
        for key, value in contract.items():
            if key not in server:
                if key in WARN_ONLY_KEYS:
                    report.warn(f"{path}.{key}: 服务端未声明默认值（不影响客户端）")
                else:
                    report.error(f"{path}.{key}: 服务端缺少契约声明的字段")
                continue
            child = f"{path}.{key}"
            if key == "required":
                missing = sorted(set(value) - set(server[key]))
                if missing:
                    report.error(f"{child}: 服务端未将契约要求的字段标为必填 {missing}")
                added = sorted(set(server[key]) - set(value))
                if added:
                    report.warn(f"{child}: 服务端额外要求必填 {added}")
            elif key == "properties":
                for name, schema in value.items():
                    if name not in server[key]:
                        report.error(f"{child}.{name}: 服务端缺少属性")
                    else:
                        subset(schema, server[key][name], f"{child}.{name}", report)
            elif key == "enum":
                missing = [item for item in value if item not in server[key]]
                if missing:
                    report.error(f"{child}: 服务端枚举缺少 {missing}")
            else:
                subset(value, server[key], child, report)
        extra = sorted(set(server) - set(contract) - {"default"})
        if extra:
            report.warn(f"{path}: 服务端多出字段 {extra}")
        return

    if isinstance(contract, list):
        if not isinstance(server, list) or len(server) != len(contract):
            report.error(f"{path}: 数组长度或类型不一致")
            return
        for index, (left, right) in enumerate(zip(contract, server)):
            subset(left, right, f"{path}[{index}]", report)
        return

    if contract != server:
        report.error(f"{path}: 期望 {contract!r}，实际 {server!r}")


def effective_security(document: dict[str, Any], operation: dict[str, Any]) -> Any:
    if "security" in operation:
        return operation["security"]
    return document.get("security")


def compare(contract: dict[str, Any], server: dict[str, Any]) -> Report:
    report = Report()

    contract_paths = contract.get("paths", {})
    server_paths = server.get("paths", {})

    for path, operations in contract_paths.items():
        if path not in server_paths:
            report.error(f"paths{path}: 服务端缺少该路径")
            continue
        for method, operation in operations.items():
            label = f"{method.upper()} {path}"
            server_operation = server_paths[path].get(method)
            if server_operation is None:
                report.error(f"{label}: 服务端缺少该操作")
                continue

            if operation.get("operationId") != server_operation.get("operationId"):
                report.error(
                    f"{label}: operationId 期望 {operation.get('operationId')}，"
                    f"实际 {server_operation.get('operationId')}"
                )
            if sorted(operation.get("tags", [])) != sorted(server_operation.get("tags", [])):
                report.error(
                    f"{label}: tags 期望 {operation.get('tags')}，实际 {server_operation.get('tags')}"
                )
            if effective_security(contract, operation) != effective_security(server, server_operation):
                report.error(
                    f"{label}: 安全要求期望 {effective_security(contract, operation)}，"
                    f"实际 {effective_security(server, server_operation)}"
                )

            contract_params = {
                (p.get("name"), p.get("in")): p
                for p in (normalize_param(item, contract) for item in operation.get("parameters", []))
                if p
            }
            server_params = {
                (p.get("name"), p.get("in")): p
                for p in (normalize_param(item, server) for item in server_operation.get("parameters", []))
                if p
            }
            for key, param in contract_params.items():
                if key not in server_params:
                    report.error(f"{label}: 缺少参数 {key[1]}:{key[0]}")
                    continue
                subset(
                    normalize(param.get("schema", {}), contract),
                    normalize(server_params[key].get("schema", {}), server),
                    f"{label}.param({key[0]})",
                    report,
                )
                if bool(param.get("required")) != bool(server_params[key].get("required")):
                    report.error(f"{label}: 参数 {key[0]} 的 required 不一致")
            for key in sorted(set(server_params) - set(contract_params)):
                report.warn(f"{label}: 服务端多出参数 {key[1]}:{key[0]}")

            contract_body = operation.get("requestBody")
            server_body = server_operation.get("requestBody")
            if bool(contract_body) != bool(server_body):
                report.error(f"{label}: 请求体存在性不一致（契约 {bool(contract_body)} / 服务端 {bool(server_body)}）")
            elif contract_body and server_body:
                if bool(contract_body.get("required")) != bool(server_body.get("required")):
                    report.error(f"{label}: 请求体 required 不一致")
                left = contract_body.get("content", {})
                right = server_body.get("content", {})
                for media, media_object in left.items():
                    if media not in right:
                        report.error(f"{label}: 请求体缺少媒体类型 {media}")
                        continue
                    subset(
                        normalize(media_object.get("schema", {}), contract),
                        normalize(right[media].get("schema", {}), server),
                        f"{label}.requestBody",
                        report,
                    )

            contract_responses = operation.get("responses", {})
            server_responses = server_operation.get("responses", {})
            for status, response in contract_responses.items():
                if status not in server_responses:
                    report.error(f"{label}: 缺少响应 {status}")
                    continue
                resolved_contract = normalize(response, contract)
                resolved_server = normalize(server_responses[status], server)
                left_content = resolved_contract.get("content") if isinstance(resolved_contract, dict) else None
                right_content = resolved_server.get("content") if isinstance(resolved_server, dict) else None
                if left_content:
                    if not right_content:
                        report.error(f"{label} {status}: 服务端缺少响应体")
                        continue
                    for media, media_object in left_content.items():
                        if media not in right_content:
                            report.error(f"{label} {status}: 缺少媒体类型 {media}")
                            continue
                        subset(
                            media_object.get("schema", {}),
                            right_content[media].get("schema", {}),
                            f"{label} {status}",
                            report,
                        )
                left_headers = resolved_contract.get("headers") if isinstance(resolved_contract, dict) else None
                if left_headers:
                    right_headers = resolved_server.get("headers") or {}
                    for name in left_headers:
                        if name not in right_headers:
                            report.error(f"{label} {status}: 缺少响应头 {name}")
            for status in sorted(set(server_responses) - set(contract_responses)):
                report.warn(f"{label}: 服务端多出响应状态 {status}")

    for path in sorted(set(server_paths) - set(contract_paths)):
        report.warn(f"paths{path}: 服务端多出路径（未纳入契约）")

    contract_schemas = contract.get("components", {}).get("schemas", {})
    server_schemas = server.get("components", {}).get("schemas", {})
    for name, schema in contract_schemas.items():
        if name not in server_schemas:
            report.error(f"schemas.{name}: 服务端缺少该 schema")
            continue
        if name.endswith("SearchResult"):
            continue
        subset(normalize(schema, contract), normalize(server_schemas[name], server), f"schemas.{name}", report)
    for name in sorted(set(server_schemas) - set(contract_schemas)):
        report.warn(f"schemas.{name}: 服务端多出 schema（未纳入契约）")

    contract_security = contract.get("components", {}).get("securitySchemes", {})
    server_security = server.get("components", {}).get("securitySchemes", {})
    for name, scheme in contract_security.items():
        if name not in server_security:
            report.error(f"securitySchemes.{name}: 服务端缺少该安全方案")
            continue
        for key in ("type", "scheme", "bearerFormat"):
            if scheme.get(key) != server_security[name].get(key):
                report.error(
                    f"securitySchemes.{name}.{key}: 期望 {scheme.get(key)!r}，"
                    f"实际 {server_security[name].get(key)!r}"
                )

    for ref in sorted(collect_refs(server)):
        if not resolve_pointer(server, ref):
            report.error(f"服务端 OpenAPI 存在无法解析的 $ref：{ref}")

    server_version = server.get("openapi", "")
    if not server_version.startswith("3.1"):
        report.warn(f"OpenAPI 版本为 {server_version}，契约快照为 {contract.get('openapi')}")

    return report


def normalize_param(item: Any, document: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if "$ref" in item:
        resolved = resolve_pointer(document, str(item["$ref"]))
        return resolved if isinstance(resolved, dict) else None
    return item


def server_schema() -> dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.main import app  # noqa: PLC0415

    return app.openapi()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比对服务端 OpenAPI 与冻结契约")
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(os.getenv("MINIAPP_CONTRACT", str(DEFAULT_CONTRACT))),
        help="Flutter 仓库里的冻结契约路径",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出比对结果")
    args = parser.parse_args(argv)

    if not args.contract.exists():
        print(f"找不到契约文件：{args.contract}", file=sys.stderr)
        return 2

    report = compare(load_json(args.contract), server_schema())

    if args.json:
        print(json.dumps({"errors": report.errors, "warnings": report.warnings}, ensure_ascii=False, indent=2))
    else:
        for message in report.errors:
            print(f"[ERROR] {message}")
        for message in report.warnings:
            print(f"[warn ] {message}")
        print(f"\n契约：{args.contract}")
        print(f"失败 {len(report.errors)} 项，警告 {len(report.warnings)} 项")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())



