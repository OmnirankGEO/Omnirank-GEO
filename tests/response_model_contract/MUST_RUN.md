# 响应契约门禁 · 必跑集(G3 接线)

> 🔴 存在的理由:「handler 多返回一个键 → 该端点每次必 500」在 2026-08 内**发生三次**,
> 每次都是上线后 Owner 点按钮才发现。第一次修完在代码里留了警告注释,
> 那条注释就写在第二次要改的类上方十几行 —— 照样没拦住。
> **注释传不出去,只有会自己报错的东西才拦得住。**

| 事故 | 出口 | 多出来的键 |
|---|---|---|
| 08-06 | 列表 `UserListItem` | `account_origin` |
| 08-13 | 详情 `AdminUserDetailResponse.overview`(**嵌套层**) | `needs_attention` |
| 08-14 | 补录 `GovernanceMutationResponse` | `backfill` |

## 两层,缺一不可

| 层 | 位置 | 接在哪 | 抓什么 |
|---|---|---|---|
| **G1 静态** | `scripts/response_model_contract_gate.py` | **preflight**(与镜像密钥扫描同级) | 遍历路由表 → AST 扫返回体字面量键 vs 模型字段树(**含嵌套**) |
| **G2 运行时** | `tests/response_model_contract/` | **本必跑集** | 每条 forbid 路由过真模型 + **双向**反向对照 |

### G1 明确抓不到(是边界,不是缺陷)

`**kwargs` 展开 · `d.update(...)` · 运行时计算出来的键 · 超过一跳的间接构造。
→ 所以 **G1 不能单独交付**,必须配 G2。

### G1 已建模的两种"不算违规"

- `result.pop("k")` 之后再 return —— 顶层 pop 会被扣除
  (`permission_version` 三条路由就是这个形状,不扣就是误报);
- 一跳递归:`return svc(...)` 与 `result = svc(...); return result` 两种写法都跟。

🔴 **门禁一旦有误报就会被当噪音整体忽略,那比没有门禁更糟** —— 所以误报要当 bug 修。

## 跑法

```bash
# G1(门禁模式,有问题 exit 1)
DATABASE_URL=<test> python scripts/response_model_contract_gate.py

# G2(两个环境变量都要给:根 conftest 强制 TEST_DATABASE_URL)
DATABASE_URL=<test> TEST_DATABASE_URL=<test> python -m pytest tests/response_model_contract -q
```

## 覆盖数(打印出来,不写"全部覆盖")

```
路由总数(反向对照)          = 1815
声明了 response_model 的路由 = 68   ← G1 覆盖数
其中 response_model 本身 forbid = 29   ← G2 覆盖数
response_model 树里 forbid 嵌套出现 = 78
```

## 🔴 红线

- 不许为了让门禁过就把 `extra="forbid"` 批量改成 `ignore` —— 那是换掉被测对象;
  口径若要改,单独交付、单独说明。
- 不许用「维护一张路由清单」代替「遍历路由表」—— 清单必漏。
  G1/G2 都是遍历 `server.app.routes`,新增路由自动纳入。
