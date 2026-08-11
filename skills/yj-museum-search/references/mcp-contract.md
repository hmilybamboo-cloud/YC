# yj-museum MCP 契约

## 现网信息

- 服务别名：`yj-museum`
- Streamable HTTP：`https://mcp.yuanjie.cc:18093/mcp`
- 2026-08-11 实测服务信息：`yj-museum` `1.0.0`
- 协议版本：`2025-03-26`
- 工具：`query_coll`、`info_coll`
- 当前未暴露 MCP Apps UI resource，也未暴露直接的图像检索工具。图片必须先由模型提取文博字段，再调用 `query_coll`。

## `query_coll`

用途：按条件分页查询藏品。`authCode` 必填；其他字段可选。同一调用中的多个字段按服务端组合逻辑检索。

| 参数 | 类型 | 含义 |
|---|---|---|
| `authCode` | string | 服务商提供的访问授权码 |
| `title` | string | 藏品名称 |
| `creator` | string | 作者/制作方 |
| `dynasty` | string | 朝代或分期 |
| `kiln` | string | 窑口 |
| `material` | string | 材质/载体 |
| `technique` | string | 工艺/技法 |
| `glaze` | string | 釉色/颜色 |
| `motif` | string | 纹饰/题材 |
| `descriptors` | string | 叙词/关键词 |
| `inscription` | string | 款识/铭文 |
| `detailed_form` | string | 器型细分 |
| `pageNum` | integer | 页码，默认 1 |
| `pageSize` | integer | 每页数量，默认 10，最大 50 |

返回结果包含 `total` 与 `items`。列表项可能包含：

`coll_code`、`title`、`dynasty`、`kiln`、`technique`、`glaze`、`motif`、`inscription`、`holding_institution`、`image_count`、`image_thumbs`。

## `info_coll`

用途：按藏品唯一编码查看详情。

- 必填：`authCode`
- 必填：`coll_code`，必须取自 `query_coll` 返回值，不要猜测。

详情可能包含：`category`、`title`、`creator`、`dynasty`、`creation_time`、`region`、`kiln`、`school`、`material`、`technique`、`glaze`、`motif`、`inscription`、`transcription`、`script_type`、`dimensions`、`weight`、`holding_institution`、`accession_no`、`exhibitions`、`references`、`provenance`、`description`、`cultural_relic_grade`、`object_form`、`detailed_form`、`binding`、`obverse_description`、`reverse_description`、`image_count`、`image_info`。

## 响应兼容

现网工具结果可能把业务 JSON 放在 MCP `content[].text` 字符串中，也可能以后提供 `structuredContent`。客户端必须兼容两种形式。

错误结果可能采用 HTTP 200 + `result.isError=true`。授权码无效时，业务错误码为 `-32602`。不要仅凭 HTTP 状态判断成功。

## 交互限制

普通文本工具不能保证原生按钮。只有工具清单出现关联 UI resource 时，才使用可点击的 MCP Apps 操作。否则：

- 列表中显示稳定序号和 `coll_code`。
- 引导用户回复“查看 2”“查看 `<coll_code>`”或“查相似 2”。
- 图片 URL 或来源 URL 存在时可做普通 Markdown 链接。
- 不伪造不存在的详情 URL，也不声称纯文本名称可点击。
