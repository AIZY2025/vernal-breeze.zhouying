# AGENTS.md

五个 Agent 组成的「春风」决策沙盘（遇事不决，可问春风）。纯标准库 Python（3.8+），**零第三方依赖，不要 pip install**，没有 lint / typecheck / 构建步骤。session 产物全在 `runs/`，已 gitignore。

## 命令

```powershell
python run.py --check                 # 自检：提示词、金额、算术、路由
python run.py --demo --fast           # 预置剧本全链路演示，不调模型
python run.py --demo-full --fast      # 加跑补充背景 + 质疑正方，回归 D 分支口径
python run.py                         # 交互模式（default config.json）
python run_web.py --no-browser        # 本地 Web，默认 127.0.0.1:8765

python -m unittest discover -s tests -t tests                  # 全量，~663 例，约 6 秒
python -m unittest discover -s tests -t tests -p "test_router.py"   # 单文件
```

- 单文件必须用 `discover -p`。`python -m unittest tests.test_router` 会失败：测试顶层 `import _support` 依赖 `-t tests` 把 tests/ 放进 sys.path。
- `config.json` 只在本机使用（gitignore 覆盖）。**不要打印、不要提交**；真实密钥只从本机配置或环境变量读取，示例见 `config.example.json`。

## 不可违反的约定

- **流转只认三个标记，不解析正文**：`_BACKGROUND_READY=true`、`_SIDE=PRO/CON/NEU`、`_SESSION=CLOSED`（产出在 `core/board.py` 的 `render_archive` / `render_report`，判定在 `Sandbox._probe` / `_judge_parallel`；`web/static/app.js` 的 `renderShow` / `handleInfo` 也依赖）。新增阶段/改动输出格式时，先保住这些标记。
- **沙盘内指令路由永不交给模型**：`core/router.py` 纯规则，优先级 `STOP > CLOSE > CHALLENGE > PARAM > NEW_FACT > UNKNOWN`；否定/条件从句（别结束、还没结束、结束之前）不算 STOP/CLOSE，同句既有明确改参数又有质疑时按 PARAM 处理。判错会改错用户参数，改关键词后跑 `test_router.py`。
- **停止推演 ≠ 结束沙盘**：`STOP` 路由与 `cancel_event` 走 `SandboxCancelled`，入口层落盘「已停止」且不调用 A5；`CLOSE` 才生成最终报告。A1 探询阶段用户回答命中 STOP 也立即取消（`Sandbox._probe`），不要只依赖 Ctrl+C 或 Web 停止按钮。改路由或取消检查点时不要合并两者，也别移除阶段边界的 `_check_cancel()`。
- **参数值域在编排层兜底**：路由允许把负数与超大值原样上送，`Sandbox._coerce_number` 必须按 `PARAM_LIMITS` 拒绝并回问，禁止取绝对值或静默截断；非法值不写档案、不生成看板。改阈值跑 `test_router.py` 与 `test_orchestrator.py`。
- **入口接待是软门禁，方向相反**：`core/intent.py` 规则优先、模型兜底，模型异常/超时/判不出一律放行。漏判（挡掉真决策）比误判（多跑一场）代价大。
- **量化数字只由 `core/quant.py` 计算，模型只负责解释**。`MockTeam` 与 `LLMTeam` 共用；质疑后修订的月度增量存在 `archive.finance.monthly_delta`，`resolve_finance` 缺省读取它，D 全量重算只能回填不得回退默认值，任何量化变化都要进漂移日志。年/月成本口径统一走 `quant.cost_period`（「每月」优先于「明年」）；应急金月数、敏感性浮动、机会成本年化与缺失默认值由 `config.quant` 经 `settings.build_quant` 注入，`board.finance_line` 必须用 `quant.default_amount` 取假设值。改公式跑 `test_quant.py`，改 D 分支跑 `test_orchestrator.py`。
- **模型输出契约统一在 `core/contract.py`**，与 `agents/*.md` 末尾【系统输出契约】成对修改。契约失败会自动补一次修正请求，仍失败则中止整场（不带半成品生成看板）。改字段要同步 `test_contract.py`。
- **仲裁必须给明确判定**：A5 的 `conclusion.verdict` / `report.verdict` 三选一（执行主方案 / 暂缓 / 放弃主方案），`reason` 不得为空，不得出现「视情况而定」这类模糊词；看板与报告均以 `【仲裁判定】` 行开头，`web/static/app.js` 的 `splitVerdict` 依赖该前缀做高亮。收尾报告不得静默改判：`Sandbox._lock_report_verdict` 在没有新增事实时锁定最近看板判定；`reason` 以「改判：」开头视为 A5 已明确声明新增事实，允许保留新判定。改契约跑 `test_contract.py`、`test_board.py`、`test_orchestrator.py`。
- **外部情报由 Agent 提请求、编排层统一执行**：A1 在档案阶段输出 `research_requests`，A2/A3 在交叉质询初稿输出 `evidence_requests`，A4 在关键参数缺失时输出 `evidence_requests`；只写自然语言 query（≤40 字）与 purpose/target，不得写 URL。编排层按 `core/research.MAX_REQUESTS=6`、`MAX_ITEMS=10`、每方上限（A1 2 / A2/A3/A4 各 1）去重执行，结果统一编号进 `archive.external.items` 并共享给所有 Agent；A2/A3 第二阶段只允许更新 cross 的 point/basis，不得增删条目。入选条目并发抓正文并写 `facts`，抓取失败只标 `page_status`。A4 的 `external_use.cited` 必须能在条目 `facts` 里核到；A2/A3 只贴 `[外N]` 的由 `board.cite_audit` 标弱引用；A5 reason 在正反方已引用时必须带真实 `[外N]`，模型漏标时只允许用 `contract.ensure_reason_cite` 按正反方真实引用过的编号补注并在 transcript 记一行日志（不得编造条目，也不得因漏标中止整场）。交互阶段仅在命中价格/政策/利率等词时重检，60 秒节流。改检索流程跑 `test_research.py`、`test_search.py`、`test_orchestrator.py`，改引用跑 `test_board.py`、`test_contract.py`、`test_quant.py`。
- **交叉质询默认 1 轮，按冲突追加第 2 轮**：`Sandbox._find_conflicts` 只认两类保守冲突（A2 接受反方风险但 A3 未降级；A3 反驳正方论据但 A2 未修订）。命中后只调用被点名一方的 `respond_pro/con`，`contract.check_respond` 只允许回应冲突 target；第 2 轮不检索、不改写论据，结果写 `pro/con.cross_rounds` 与 `cross_state`，看板显示「第 1 轮 → 第 2 轮 → 收敛/未收敛」。改流程跑 `test_orchestrator.py`、`test_contract.py`、`test_board.py`。
- **改提示词只动 `agents/*.md`**：`core/prompts.py` 每次启动重新加载，首行 `# 标题` 会被剥掉。
- **Web 层不碰编排**：`web/server.py` 只提供 ui 实现、SSE 事件总线与队列回灌，`Sandbox` 的阻塞 `ask()` 原样复用；停止只通过 `cancel_event` 注入，不改六阶段流程。前端原生 HTML/CSS/JS，无框架、无 CDN、无打包步骤。模型调用次数与耗时由 `LLMTeam.stats()` 经 `stats` 事件推送，不写进看板正文。
- **Web 响应脱敏**：`Handler.version_string()` 固定返回 `sandbox`，不得恢复产品名、Python 运行时版本；`Session.snapshot()` 与 SSE `status`/`done` 只下发 `run_id`，不得回传 `run_dir` 等服务器绝对路径。改 Web 跑 `test_web_http.py`、`test_web_server.py`。
- **续跑只从状态快照恢复，不重跑 A1**：`SessionStore(reuse=run_id)` 复用同一 `runs/` 目录并追加 transcript，`Sandbox.restore()` 恢复档案/三方输出/`version`/`drift`/`syncs`/`history`，再由 `Sandbox.resume()` 直接进入交互阶段。快照必须带 `query`、`archive`、`conclusion` 与 `version`；不要在续跑时重置版本号或覆盖旧 transcript。改续跑跑 `test_store.py`、`test_orchestrator.py`、`test_web_server.py`。
- **收尾必须落复查清单**：`Sandbox._final` 把 `report.switch` 与 `report.fallback` 写成 `复查清单.json`（30 天后到期）；Web 通过 `/api/reviews` 提示到期场次，`review_run` 只更新 `reviewed_at`，不得改写判定与条件。改复查跑 `test_orchestrator.py`、`test_web_server.py`。

## gotchas

- `provider: "auto"` 有 Key 走模型、无 Key 静默落回预置剧本；显式 `"llm"` 无 Key 直接 `SystemExit`。现场断网演示用 `search.mode: "cache"`。
- 外部情报是 A1/A2/A3/A4 的取证工具，**不是第六个 Agent**。缺 Key / 超时 / 条目缺 `url` 或 `date` 都只记「未获取」并继续，不得伪造来源；未获取的请求也要记入 `external.requests` 并在看板显示原因。检索在 A1 建档后执行，由 Agent 提出请求，`core.research` 负责去重、预算与证据池编号；`core.search.search_queries` 只执行指定短查询，抓正文后写 `facts`。看板用 `core.board.external_usage_text` 显示每个请求、条目发起方、引用与弱引用。不要退回「代码统一拼 query 搜一次」。
- Web 只接受回环客户端且校验 `Host`/`Origin`（`Handler._guard`）；`--allow-remote` 会绕过客户端检查，不要用它暴露到不可信网络。
- Web 测试自起 `http.server`，Windows 上偶发回环/端口失败，先重跑再怀疑代码。
- 测试用 `tempfile` + `tests/_support.py` 的 `FakeUI`；新增测试不要写真实 `runs/`。
- 路径含中文和空格，shell 命令一律加引号。
- `README.md` 的用例计数要与实际一致（当前 663），改测试后同步数字。
- 检索趋势词按 `archive.type` 定制（`search.QUERY_SUFFIX`），缓存 key 必须包含 `max_items`/`per_site`；A1 的问答历史经 `_history_text` 按 12000 字截断，不要改回全量拼接。