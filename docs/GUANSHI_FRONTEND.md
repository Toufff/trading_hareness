# 观市界面维护

2026-09-22：采用用户确认的《瑞鹤图》参考风格。石青页首、暖纸底色、墨色正文、淡金分隔，品牌仅为“观市”与小印。功能名称保持直白，行情红绿仅表达涨跌。不是对业务评分、推荐或交易规则的修改。

## 维护入口

- `frontend/src/theme/tokens.css`：唯一色彩、字体及 Element Plus 变量。
- `frontend/src/theme/surfaces.css`：共享界面、响应式和减少动效支持。
- `frontend/src/theme/chart-theme.ts`：Canvas 图表主题，需与 CSS 色值保持一致；单元测试检查正文对比度。
- `frontend/src/components/WorkspaceHeader.vue`：九个页面的统一导航。
- `frontend/src/components/stock-workbench.css`：个股图形研究台专用布局。

覆盖 `/market`、`/holdings`、`/intraday`、`/sector-heat`、`/research`（含11个页签）、`/agent-paper`、`/monitor`、`/workbench`、`/relay`。既有 `/market-decision` 入口继续有效。无外部字体、图片或 CDN 依赖。

## 验收与发布

1. 前端 typecheck、完整单元测试、build。
2. 构建后运行 `npx vite preview --host 127.0.0.1 --port 13849`，使用真实本机服务；运行 `e2e/guanshi-theme.spec.ts`，检验1440/390/320宽度、真实推荐/持仓/板块、个股K线、策略切换、研究页签和浏览器后退。必须验证生产构建，不能只测 Vite dev：曾有嵌套条件动态 import 的预加载依赖被合并，导致生产分支丢失 CSS，而开发模式正常。入口使用独立 loader 保持依赖隔离。只读，不点击采集、生成、写入或交易按钮。
3. 提交并推送用户 fork；使用 `scripts/windows/publish-stock-release.ps1` 正式发布，不跳过测试或部署窗口。
4. 使用 `scripts/deploy-stock-dashboard.ps1` 原子发布公网静态资源并校验网关。
5. 将 `PLAYWRIGHT_BASE_URL` 指向公网，设置本机 `GUANSHI_CREDENTIALS_FILE` 凭据文件路径重复验收；凭据只注入 Cookie，不输出或提交。可用 `PLAYWRIGHT_CHANNEL=msedge` 使用已安装浏览器。

样式通过不能代替数据链验收：持仓过期、研究不足、数据质量警告仍须如实展示。不能因为更换主题而隐藏警告或把历史数据当作实时结果。
