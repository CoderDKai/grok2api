# Append Token Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增后台 token 追加接口，并将 `/admin/token` 页面导入流程改为调用该接口，从而支持追加而非覆盖。

**Architecture:** 后端在 `app/api/v1/admin/token.py` 增加独立的 `POST /v1/admin/tokens/append`，复用 `_sanitize_token_text()`、`TokenManager.get_pool_name_for_token()` 与 `TokenManager.add()` 实现全局判重后的增量追加。前端仅改 `submitImport()`，将单个选中池与多行文本组装为字符串数组请求体，成功后重新加载数据，失败时保留弹窗和输入内容。

**Tech Stack:** FastAPI, Python 3.13, 标准库 `unittest`, 原生前端 JavaScript

---

执行前提：必须在独立 git worktree 的非 `main` 分支执行，不能直接在当前 `main` 分支上改代码。

### Task 1: 后端追加接口测试

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/api/__init__.py`
- Create: `tests/api/v1/__init__.py`
- Create: `tests/api/v1/admin/__init__.py`
- Create: `tests/api/v1/admin/test_token_append.py`
- Reference: `app/api/v1/admin/token.py`

- [ ] **Step 1: 写追加接口的失败测试**

```python
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.api.v1.admin import token as token_api


class _Lock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TokenAppendApiTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_append_adds_new_tokens_and_skips_existing(self, mock_get_manager):
        mgr = Mock()
        mgr.get_pool_name_for_token.side_effect = lambda token: "legacy" if token == "exists" else None
        mgr.add = AsyncMock(return_value=True)
        mock_get_manager.return_value = mgr

        payload = await token_api.append_tokens(
            {"ssoBasic": ["new-token", "exists", "  ", "new-token"]}
        )

        self.assertEqual(payload["summary"], {"added": 1, "skipped": 2, "invalid": 1})
        mgr.add.assert_awaited_once_with("new-token", "ssoBasic")

    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_append_ignores_extra_fields_in_object_items(self, mock_get_manager):
        mgr = Mock()
        mgr.get_pool_name_for_token.return_value = None
        mgr.add = AsyncMock(return_value=True)
        mock_get_manager.return_value = mgr

        payload = await token_api.append_tokens(
            {"ssoBasic": [{"token": "abc", "note": "ignored", "tags": ["x"]}]}
        )

        self.assertEqual(payload["summary"], {"added": 1, "skipped": 0, "invalid": 0})
        mgr.add.assert_awaited_once_with("abc", "ssoBasic")

    @patch("app.api.v1.admin.token.get_storage")
    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_update_tokens_keeps_replace_semantics(self, mock_get_manager, mock_get_storage):
        storage = Mock()
        storage.acquire_lock.return_value = _Lock()
        storage.load_tokens = AsyncMock(return_value={"ssoBasic": [{"token": "old-token"}]})
        storage.save_tokens = AsyncMock()
        mock_get_storage.return_value = storage

        mgr = Mock()
        mgr.reload = AsyncMock()
        mock_get_manager.return_value = mgr

        payload = await token_api.update_tokens({"ssoBasic": ["new-token"]})

        self.assertEqual(payload["status"], "success")
        storage.save_tokens.assert_awaited_once_with({"ssoBasic": [{"token": "new-token", "tags": []}]})
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m unittest tests.api.v1.admin.test_token_append -v`
Expected: 因 `token_api.append_tokens` 尚不存在而失败，报 `AttributeError` 或等价缺失错误。

- [ ] **Step 3: 如果测试先因 mock 夹具失败，先修夹具不写生产代码**

要求：
- `get_token_manager` 保持 `AsyncMock`，因为它本身是异步工厂。
- `mgr.get_pool_name_for_token` 使用普通 `Mock` 或同步 `side_effect`。
- 不引入 `TestClient`、不覆盖认证依赖，因为这里直接测试 handler 函数。

- [ ] **Step 4: 再次运行测试确认仍为红灯**

Run: `python -m unittest tests.api.v1.admin.test_token_append -v`
Expected: 仅剩新增接口缺失或返回结构不匹配导致的失败。


### Task 2: 实现后端追加接口

**Files:**
- Modify: `app/api/v1/admin/token.py`
- Test: `tests/api/v1/admin/test_token_append.py`

- [ ] **Step 1: 增加请求归一化辅助逻辑**

```python
def _extract_token_value(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("token")
    return ""
```

再加一个小的计数 helper，负责初始化：

```python
def _empty_append_counts() -> dict[str, int]:
    return {"added": 0, "skipped": 0, "invalid": 0}
```

- [ ] **Step 2: 新增 `POST /v1/admin/tokens/append` 实现**

```python
@router.post("/tokens/append", dependencies=[Depends(verify_app_key)])
async def append_tokens(data: dict):
    mgr = await get_token_manager()
    summary = _empty_append_counts()
    pools = {}
    seen = set()

    for pool_name, tokens in (data or {}).items():
        if not isinstance(tokens, list):
            continue
        pool_counts = _empty_append_counts()
        pools[pool_name] = pool_counts
        for item in tokens:
            raw_token = _extract_token_value(item)
            token = _sanitize_token_text(raw_token)
            if not token:
                pool_counts["invalid"] += 1
                summary["invalid"] += 1
                continue
            if token in seen:
                pool_counts["skipped"] += 1
                summary["skipped"] += 1
                continue
            seen.add(token)
            if mgr.get_pool_name_for_token(token):
                pool_counts["skipped"] += 1
                summary["skipped"] += 1
                continue
            added = await mgr.add(token, pool_name)
            if added:
                pool_counts["added"] += 1
                summary["added"] += 1
            else:
                pool_counts["skipped"] += 1
                summary["skipped"] += 1

    return {
        "status": "success",
        "message": "Token 追加完成",
        "summary": summary,
        "pools": pools,
    }
```

实现要求：
- 使用 `mgr.get_pool_name_for_token(token)` 做全局判重。
- 请求中的对象项只识别 `token`，忽略 `note`、`tags` 等附加字段。
- 不触碰现有 `update_tokens()` 的覆盖逻辑。

- [ ] **Step 3: 运行后端测试确认转绿**

Run: `python -m unittest tests.api.v1.admin.test_token_append -v`
Expected: 3 个测试全部通过。

- [ ] **Step 4: 只做最小清理**

```python
seen = set()
# 对同一请求中的清洗后 token 做全局去重，和规格保持一致
```

只整理命名或重复，不改变行为。

- [ ] **Step 5: 再跑一次后端测试**

Run: `python -m unittest tests.api.v1.admin.test_token_append -v`
Expected: 仍然全部通过。


### Task 3: 改造前端导入流程

**Files:**
- Modify: `_public/static/admin/js/token.js`
- Reference: `docs/superpowers/specs/2026-03-28-append-token-design.md`

- [ ] **Step 1: 先写出前端目标行为注释块到计划外脑图，不改生产代码**

```js
// submitImport target behavior:
// 1. parse textarea into non-empty lines
// 2. POST { [pool]: lines } to /v1/admin/tokens/append
// 3. on success: show summary toast, close modal, reload data
// 4. on failure: keep modal open and preserve textarea content
```

这是执行时的临时思路，不写进生产文件。

- [ ] **Step 2: 用最小改动重写 `submitImport()`**

```js
async function submitImport() {
  const pool = byId('import-pool').value.trim() || 'ssoBasic';
  const text = byId('import-text').value;
  const tokens = text
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean);

  if (tokens.length === 0) {
    showToast(t('token.tokenEmpty'), 'error');
    return;
  }

  try {
    const res = await fetch('/v1/admin/tokens/append', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ [pool]: tokens })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || t('common.saveFailed'));

    const summary = data.summary || { added: 0, skipped: 0, invalid: 0 };
    showToast(`新增 ${summary.added} 个，跳过 ${summary.skipped} 个，非法 ${summary.invalid} 个`, 'success');
    closeImportModal();
    await loadData();
  } catch (e) {
    showToast(t('common.saveError', { msg: e.message }), 'error');
  }
}
```

要求：
- 删除对 `flatTokens.push(...)` 和 `syncToServer()` 的依赖。
- 失败时不关闭弹窗，不清空输入。
- 成功时才关闭弹窗并刷新。

- [ ] **Step 3: 做最小人工验证**

Run: `python -m compileall app`
Expected: Python 代码编译通过。

然后人工检查 `submitImport()`：
- 没有 `flatTokens.push(`
- 没有 `await syncToServer()`
- 有 `fetch('/v1/admin/tokens/append'`
- `try/catch` 中只有成功分支会调用 `closeImportModal()` 与 `loadData()`


### Task 4: 整体验证

**Files:**
- Test: `tests/api/v1/admin/test_token_append.py`
- Verify: `app/api/v1/admin/token.py`
- Verify: `_public/static/admin/js/token.js`

- [ ] **Step 1: 跑本次后端回归测试**

Run: `python -m unittest tests.api.v1.admin.test_token_append -v`
Expected: 全部通过。

- [ ] **Step 2: 跑 Python 语法验证**

Run: `python -m compileall app`
Expected: 编译成功，无语法错误。

- [ ] **Step 3: 跑针对前端改动的文本校验**

Run: `python - <<'PY'
from pathlib import Path
text = Path('_public/static/admin/js/token.js').read_text()
start = text.index('async function submitImport()')
end = text.index('// Export Logic')
block = text[start:end]
assert "fetch('/v1/admin/tokens/append'" in block
assert 'await syncToServer()' not in block
assert 'flatTokens.push({' not in block
assert 'closeImportModal();' in block
assert 'await loadData();' in block
print('ok')
PY`
Expected: 输出 `ok`。

- [ ] **Step 4: 做前端最小语法校验**

Run: `node -e "new Function(require('fs').readFileSync('_public/static/admin/js/token.js','utf8'))"`
Expected: 无输出且退出码为 0；若因浏览器全局变量报错则说明命令写法不对，需要保持只做语法解析，不执行页面逻辑。

- [ ] **Step 5: 准备交付说明，不提交**

记录以下结果用于最终回复：
- 新接口路径
- 旧覆盖接口保持不变
- 已执行的测试命令及结果
- 前端导入失败路径保持弹窗不变
