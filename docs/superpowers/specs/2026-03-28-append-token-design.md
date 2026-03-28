# Token 追加接口设计

## 背景

当前后台 Token 管理页面的导入流程会先在前端重建完整 token 快照，再调用 `POST /v1/admin/tokens` 落库。该接口在服务端调用 `save_tokens()`，语义是全量覆盖，因此导入动作本质上是“覆盖保存”，而不是“追加保存”。

Boss 的目标是新增一个真正的追加接口，使导入 token 时只追加不存在的 token，不覆盖、不删除已有 token，并将后台页面导入流程切换到该追加接口。

## 目标

- 保留现有 `POST /v1/admin/tokens` 的全量覆盖语义，避免破坏兼容性。
- 新增独立追加接口，专门用于“只追加新 token，跳过已存在 token”。
- 后台 `/admin/token` 页面中的导入流程改为调用追加接口。
- 追加流程复用现有 token 清洗与增量保存能力，不引入额外兼容层。

## 非目标

- 不修改现有覆盖接口的行为。
- 不把“追加”和“覆盖”混进同一个接口模式参数里。
- 不为已存在 token 做字段更新或覆盖。
- 不改动普通编辑、删除、启用/禁用等现有管理流程。

## 当前实现

### 现状链路

1. 页面 `/_public/static/admin/js/token.js` 中的 `submitImport()` 读取导入文本。
2. 导入内容被追加到前端内存态 `flatTokens`。
3. 随后调用 `syncToServer()`。
4. `syncToServer()` 将 `flatTokens` 重建为完整 `{ pool: [...] }` 结构并 POST 到 `/v1/admin/tokens`。
5. 服务端 `app/api/v1/admin/token.py` 中的 `update_tokens()` 调用 `storage.save_tokens(normalized)`。
6. 各存储实现将新集合视为完整真值来源，删除未出现在请求体中的旧 token。

### 已有可复用能力

- `app/api/v1/admin/token.py` 中的 `_sanitize_token_text()` 已提供 token 文本清洗逻辑。
- `app/services/token/manager.py` 中的 `TokenManager.add()` 已提供单 token 追加能力。
- `app/core/storage.py` 中的 `save_tokens_delta()` 已提供增量保存能力。

结论：底层已有追加能力，缺的是一个明确的后台追加 API，以及前端导入流程切换。

## 方案对比

### 方案 A：新增独立追加接口

- 新增 `POST /v1/admin/tokens/append`。
- 保留原 `POST /v1/admin/tokens` 的覆盖语义。
- 页面导入流程改调新接口。

优点：语义清晰、兼容性最好、风险最低。  
缺点：多一个接口。

### 方案 B：在现有覆盖接口加模式参数

- 继续使用 `POST /v1/admin/tokens`。
- 通过 body 或 query 参数切换 `append` / `replace`。

优点：接口数量少。  
缺点：一个接口承载两种写语义，维护和排错成本高。

### 方案 C：直接把现有覆盖接口改成追加

- 不新增接口，直接改变 `POST /v1/admin/tokens` 的语义。

优点：表面改动少。  
缺点：破坏现有兼容性，风险最高。

## 决策

采用方案 A。

## 设计

### 1. 新增接口

在 `app/api/v1/admin/token.py` 新增：

- `POST /v1/admin/tokens/append`

请求体沿用当前接口的 `{ pool: [...] }` 结构，但前端导入场景只会发送字符串数组：

```json
{
  "ssoBasic": ["xxx", "yyy"]
}
```

页面 `submitImport()` 实际只有“单个选中 pool + 多行文本”输入，因此请求体会组装成：

```json
{
  "<selectedPool>": ["line1", "line2", "line3"]
}
```

这样可以最小化前端改动，避免为导入单独引入另一套数据模型。

### 2. 追加语义

新接口的语义固定如下：

- 只追加不存在的 token。
- 已存在 token 跳过，不更新已有字段。
- 不删除任何已有 token。
- 新 pool 允许自动创建。
- token 唯一性按全局判定，不按单个 pool 局部判定。

### 3. 服务端处理流程

接口内部处理顺序：

1. 遍历每个 pool 的输入数组。
2. 支持数组项为字符串或对象；对象场景仅提取 `token` 字段，其他字段忽略。
3. 使用 `_sanitize_token_text()` 清洗 token。
4. 清洗后为空的计入 `invalid`。
5. 用请求内去重集合避免同一请求重复追加同一 token。
6. 使用 `TokenManager.get_pool_name_for_token(token)` 做全局判重。
7. 若任意 pool 中已存在，则计入 `skipped`。
8. 仅在全局不存在时调用 `TokenManager.add(token, pool_name)`。
9. 汇总每个 pool 和全局统计并返回。

该路径不调用 `save_tokens()`，从而避免覆盖现有集合。

### 4. 返回结构

接口返回应包含可直接用于前端提示的统计结果：

```json
{
  "status": "success",
  "message": "Token 追加完成",
  "summary": {
    "added": 3,
    "skipped": 2,
    "invalid": 1
  },
  "pools": {
    "ssoBasic": {
      "added": 2,
      "skipped": 1,
      "invalid": 1
    },
    "oai-api": {
      "added": 1,
      "skipped": 1,
      "invalid": 0
    }
  }
}
```

这允许前端在导入后提示“新增 X 个，跳过 Y 个，非法 Z 个”。

### 5. 前端改动

在 `/_public/static/admin/js/token.js` 中：

- 保留 `syncToServer()` 供现有覆盖保存逻辑使用。
- 修改 `submitImport()`，不再先改 `flatTokens` 再调用 `syncToServer()`。
- `submitImport()` 改为：
  - 解析输入文本
  - 按当前选中 pool 组装 `{ [selectedPool]: [line1, line2, ...] }`
  - 调用 `POST /v1/admin/tokens/append`
  - 成功后关闭弹窗并调用 `loadData()` 重新加载
- 失败时不关闭弹窗、不调用 `loadData()`、不修改本地 `flatTokens`
- 导入成功提示展示服务端统计结果。

这样页面导入会成为真正的服务端追加，而不是前端全量覆盖。

## 边界与错误处理

### 输入兼容

- 初版接口契约只保证 token 追加。
- 继续兼容数组项是字符串或对象，但对象输入只读取 `token` 字段。
- `note`、`tags` 等附加字段全部忽略，不参与写入。
- 前端导入流程只发送字符串数组，不发送对象数组。

### 重复处理

- 同一请求体内部重复 token：只处理一次，其余计入 `skipped`。
- 服务端任意 pool 已存在 token：计入 `skipped`。

### 新 pool

- 沿用 `TokenManager.add()` 行为，pool 不存在则自动创建。

### 失败处理

- 单个 token 非法不应导致整批失败。
- 真正的接口级异常返回 500。
- 前端沿用现有 toast 失败提示，且失败时保持弹窗与输入内容不变。

## 测试策略

优先补后端接口测试，覆盖：

1. 追加新 token 成功。
2. 已存在 token 被跳过，且原 token 不被覆盖。
3. 其他 pool 已存在相同 token 时被跳过。
4. 空 token / 清洗后为空 token 被计入 `invalid`。
5. 同一请求内重复 token 被跳过。
6. 新 pool 自动创建。
7. 对象输入中的附加字段被忽略，不影响追加结果。
8. 旧 `POST /v1/admin/tokens` 仍保持覆盖语义。

前端至少做最小回归验证：

1. `submitImport()` 改调 `/v1/admin/tokens/append`。
2. 导入不再调用 `syncToServer()`。
3. 成功后刷新列表并显示统计结果。
4. 失败时不关闭弹窗、不刷新列表、不污染本地内存态。

## 实施影响

- 影响文件：
  - `app/api/v1/admin/token.py`
  - `_public/static/admin/js/token.js`
- 可能新增测试文件，取决于现有测试结构。

## 风险

- `TokenManager.add()` 当前只显式接收 `token` 和 `pool_name`，因此初版追加接口不会为新 token 初始化 `note`、`tags` 等附加字段。
- 如果 Boss 后续要求导入时连带初始化这些字段，需要追加一个批量 add/upsert 领域方法，而不是继续复用当前最小能力的 `add()`。

当前 Boss 已确认的范围是不覆盖、不删除、已存在即跳过，因此初版按最小追加能力实现，并在接口契约中明确忽略附加字段。
