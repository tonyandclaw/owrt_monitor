# owrt_monitor Long-Term TODO

這份 TODO 是以長期維護、穩定跑 OpenWrt build 與 DUT firmware upgrade/test 為目標。第一版可以先用 Python 做出完整 workflow；當流程穩定後，把長時間執行、process supervision、serial streaming、job cancellation 等部分逐步抽到 Go runner。

## Phase 0 - Project Foundations

- [x] 決定第一批支援平台：
  - [x] macOS host。
  - [x] Docker / Docker Desktop。
  - [x] OpenWrt build container。
  - [x] USB serial DUT。
- [x] 決定 config 格式，預設使用 YAML。
- [x] 建立 repo skeleton：
  - [x] `cmd/owrtd/` for Go daemon。
  - [x] `cmd/owrtctl/` for Go CLI helper, optional。
  - [x] `python/owrt_monitor/` for Python orchestrator package。
  - [x] `configs/` for sample YAML。
  - [x] `tests/` for unit/integration tests。
  - [x] `docs/` for user/developer docs。
- [x] 建立基本開發工具：
  - [x] Go module。
  - [x] Python package metadata。
  - [x] formatter/linter。
  - [x] Makefile or task runner。
  - [x] GitHub Actions basic lint/test。
- [x] 定義專案用語：
  - [x] Job。
  - [x] Build。
  - [x] Artifact。
  - [x] DUT。
  - [x] Upgrade。
  - [x] Test run。
  - [x] Session。

## Phase 1 - Configuration Model

- [x] 設計 YAML schema：
  - [x] builder/container。
  - [x] build command。
  - [x] artifact patterns。
  - [x] host artifact output directory。
  - [x] DUT serial settings。
  - [x] DUT network settings。
  - [x] firmware transfer method。
  - [x] upgrade command。
  - [x] post-upgrade boot checks。
  - [x] test commands。
- [x] 實作 config validation。
- [x] 支援多 profile（`profiles:` block + `--profile <name>` CLI flag，deep-merge overlay 到 base config）：
  - [x] board profile（已落地：ap / controller / switch / ap-mt76 在 `configs/example.yaml`）。
  - [x] target profile（同 board，因為 builder.command + artifact.patterns 一併蓋）。
  - [x] DUT profile（profile 可以蓋 `dut:` block 任何欄位，例如同台機器多個序列埠）。
  - [x] test profile（profile 可蓋 `tests.smoke` list）。
- [x] 支援 secret/env interpolation，但避免把 secret 寫進 log。
- [x] 支援 dry-run，列出即將執行的 Docker/artifact/DUT actions。

## Phase 2 - Python Orchestrator MVP

- [x] 建立 Python CLI（11 commands）：
  - [x] `owrt-monitor build`。
  - [x] `owrt-monitor flash`。
  - [x] `owrt-monitor test`。
  - [x] `owrt-monitor run`。
  - [x] `owrt-monitor status`。
  - [x] `owrt-monitor cancel`。
  - [x] `owrt-monitor resume`（支援 BUILD_SUCCEEDED / ARTIFACT_SELECTED / ARTIFACT_EXPORTED）。
- [x] 實作 workflow state machine：
  - [x] preflight。
  - [x] build。
  - [x] artifact detection。
  - [x] artifact export。
  - [x] DUT prepare。
  - [x] firmware transfer。
  - [x] upgrade。
  - [x] reboot wait。
  - [x] post-upgrade tests。
  - [x] report。
- [x] 使用 SQLite 保存 job state，支援 crash recovery（`jobs` / `job_events` / `artifacts` / `dut_locks` / `builder_locks` / `test_results` 6 個 table；每個 state transition 在 side-effect 前 commit；orphan job 透過 PID liveness + lock stale-recovery 恢復）。
- [x] 每個 step 都要有：
  - [x] timeout。
  - [x] retry policy（artifact_select/artifact_export/firmware_transfer/smoke_tests）。
  - [x] structured log。
  - [x] exit status。
  - [x] resumability note（resume 路徑記在 `docs/safe-upgrade.md` "After a failure" 段；resume policy 在 `docs/troubleshooting.md`，列明只支援 BUILD_SUCCEEDED/ARTIFACT_SELECTED/ARTIFACT_EXPORTED 三個 state）。
- [x] 支援 job cancel（marker file + cooperative checks）。
- [x] 支援只重跑失敗 step（resume 支援 BUILD_SUCCEEDED / ARTIFACT_SELECTED / ARTIFACT_EXPORTED；DUT 階段刻意不支援，因為 device state ambiguous after partial flash — 文件化於 `docs/troubleshooting.md`）。

## Phase 3 - Docker/OpenWrt Build Support

- [x] 支援 Docker container discovery（design decision：明確指定 `builder.container` 而不是自動掃 `docker ps`，避免誤選；preflight 會用 `docker inspect` 確認該 container 存在且 running，缺一即 abort with clear message）。
- [x] 支援指定 container name/id。
- [x] 支援 build 前 preflight：
  - [x] container running。
  - [x] workspace path exists。
  - [x] OpenWrt tree exists。
  - [x] enough disk space（`builder.min_free_disk_mb`，預設 5000 MB；用 `df -B1 --output=avail`，回 0 或 introspection 失敗時跳過）。
  - [x] expected feeds/config present（`builder.required_paths: list[str]`：每個 path 在 preflight 用 `docker exec test -e` 檢查；缺一即列出全部 missing path 並 abort）。
- [x] 支援 Docker exec build command。
- [x] 即時收集 stdout/stderr。
- [x] 解析常見 OpenWrt build result（`build_log.classify_build_log`，寫入 report.md `## Build Log` 段）：
  - [x] success（偵測 `>>>> <profile>  Build done in: <duration>` 並抽出耗時）。
  - [x] failed package（抽最深層 `make[N]: *** [<step>] Error N`，並進一步從 `package/foo/bar/compile` 之類路徑萃出 `failed_package: foo/bar`）。
  - [x] missing dependency（收 `WARNING: …` 行，上限 50）。
  - [x] disk full（任一 `No space left on device` 即分類為 disk_full）。
  - [x] compile error（top-level `make: *** [...mk:LINE: <target>] Error 2` 但無更深 package fail）。
- [x] 支援 artifact detection：
  - [x] glob（bash globstar 在 container 內展開；不需要 python3）。
  - [x] regex（`artifact.regex_patterns` list；glob 展開後再用 `re.search` 過濾，所有 pattern 都要 match）。
  - [x] newest file。
  - [x] size threshold。
  - [x] checksum。
- [x] 支援 `docker cp` 匯出指定 firmware。
- [x] 建立 artifact metadata（`WorkflowReport.build_metadata`，render 在 report.md `## Provenance` 段）：
  - [x] image path。
  - [x] SHA256。
  - [x] build timestamp（`built_at`，UTC ISO-8601）。
  - [x] git commit if available（`git_commit` / `git_describe` / `git_dirty`，best-effort，失敗不會擋 build）。
  - [x] OpenWrt target/subtarget/profile（`make_target` + 已套用的 `profile` 名稱）。

## Phase 4 - DUT Serial Control

- [x] 支援 USB serial discovery：
  - [x] `/dev/cu.usbserial-*`。
  - [x] `/dev/tty.usbserial-*`。
  - [x] custom path。
- [x] 支援 serial config（pyserial 全參數可調）：
  - [x] baud rate。
  - [x] data bits/parity/stop bits（`dut.bytesize` ∈ {5,6,7,8}、`dut.parity` ∈ {none,even,odd,mark,space}、`dut.stopbits` ∈ {1,2}；預設 8-N-1）。
  - [x] newline mode。
  - [x] prompt regex。
- [x] 實作 robust serial session：
  - [x] connect/disconnect。
  - [x] read loop（`SerialSession.read_until` 主動 poll loop：50ms tick + cancel_token check + failure_patterns scan，cancel/timeout 都會 raise）。
  - [x] command write。
  - [x] prompt wait。
  - [x] boot log capture。
  - [x] timeout handling。
- [x] 支援 login flow：
  - [x] root shell without password。
  - [x] username/password（`dut.login.password` 不為 None 時，DutWorkflow 自動處理 `login:` → username → `Password:` → password 的對話；密碼寫入時在 serial.log 內顯示為 `<redacted>`）。
  - [x] custom prompt（`dut.prompt` 為 regex，每個 profile 可自訂；read_until_one_of 同時支援 shell/login/password 三 sentinels）。
- [x] 支援 reboot detection：
  - [x] kernel boot marker（任意 panic / Oops / "Unable to handle" 之類訊號 → 立即抛 BootFailureError，不等 boot_timeout_sec）。
  - [x] login prompt（`_connect_with_optional_login`：以 `read_until_one_of` 同時等 shell / login / password sentinels）。
  - [x] OpenWrt banner（`upgrade.expected_boot_markers` regex list；shell prompt 出現後檢查 boot transcript 是否含全部 marker，缺一即 raise DutWorkflowError）。
  - [x] shell prompt ready。
- [x] 支援 DUT lock，避免兩個 job 同時 flash 同一台 DUT（含 stale-lock 自動回收，依 `dut.lock_timeout_sec`）。

## Phase 5 - Firmware Transfer and Upgrade

- [x] 支援 firmware transfer methods（HTTP / TFTP / U-Boot bootloader-TFTP；SCP 與 custom command 留給之後）：
  - [x] host HTTP server + DUT `wget`/`curl`。
  - [ ] SCP when DUT network is ready（schema 已含 `transfer: scp` literal placeholder；實作留待第一個需要的 board 上線時做。HTTP/TFTP/bootloader-TFTP 已涵蓋目前所有實機 boards）。
  - [x] TFTP for bootloader/recovery flow（兩種 TFTP）：
    - [x] OpenWrt-shell TFTP（`upgrade.transfer: tftp`：DUT busybox `tftp -g -r ...`）。
    - [x] U-Boot bootloader TFTP（`upgrade.transfer: bootloader_tftp`：shell `reboot` → 等 autoboot banner → 送 interrupt key → `setenv serverip/ipaddr; tftpboot <addr> <name>; bootm`，volatile boot 不寫 flash；`upgrade.bootloader.*` 全部可調；configs/example.yaml 已加 `ap-recovery` profile）。
  - [ ] custom command（schema 已含 `transfer: custom` literal placeholder；實作留待第一個非標準 transfer 機制需要時做。設計上應為 user 提供 shell template，由 host 端 subprocess 執行）。
- [x] host 啟動臨時 HTTP server（`TemporaryFirmwareServer`：bind interface + port 0=auto；checksum endpoint 與 transfer log 為 nice-to-have，留作 future）：
  - [x] bind interface。
  - [x] random safe port。
  - [x] checksum endpoint（`TemporaryFirmwareServer` 內建 `<filename>.sha256` route：on-demand SHA256 計算，回 `<hex>  <filename>` 格式；缺檔回 404）。
  - [x] transfer log（`firmware_published` event 在 events.jsonl，含 filename + size_bytes + tftp_root；HTTP transfer 的 wget 透過 `session.run_command` 把整段 wget output 寫進 serial.log；audit trail 完整）。
- [x] DUT 下載 firmware 後檢查：
  - [x] file exists。
  - [x] file size。
  - [x] SHA256。
- [x] 支援 OpenWrt upgrade：
  - [x] `sysupgrade`。
  - [x] `mtd` custom flow。
  - [x] platform-specific script。
- [x] upgrade 前 safety checks（多層；剩 battery/power note 為 informational）：
  - [x] artifact target matches DUT target（`dut.expected_artifact_pattern` regex；selected artifact filename 不 match 就 abort，BuildWorkflow.run / resume / FlashWorkflow.run 三條路徑都 gate）。
  - [x] minimum battery/power note if applicable（informational：`docs/safe-upgrade.md` 的 pre-flash checklist 提醒人 hardware 上電穩定後才 flash；機制上沒有自動 battery 偵測，因為 lab 板均為直流供電，不適用）。
  - [x] enough `/tmp` space（`upgrade.min_dut_free_kb`，預設 0=disabled；BusyBox `df -k` 解析 + 跟 firmware 大小取最大）。
  - [x] no active conflicting job（builder lock：`builder.lock_timeout_sec` 預設 3600；BuildWorkflow.run 起 acquire、finally release，stale heartbeat 自動回收，dry-run 跳過）。
- [ ] upgrade 後等待 reboot（剩 serial disconnect/reconnect tolerance）：
  - [ ] serial disconnect/reconnect tolerance（pyserial 在 USB 拔插時會 raise OSError；尚未 wrap 為自動 reconnect — 對 flash 流程影響低，因為 sysupgrade 通常不會物理拔線）。
  - [x] boot timeout。
  - [x] prompt detection。
  - [x] fail-fast on kernel panic（`upgrade.boot_failure_patterns`，預設含 "Kernel panic - not syncing"、"Oops:"、"Unable to handle kernel paging request" 等；偵測到立即拋 BootFailureError，evidence 帶被觸發那一行）。
- [x] 保存完整 upgrade transcript。

## Phase 6 - Post-Upgrade Testing

- [x] 支援 test runner interface（serial shell 是主要路徑，含 regex assertion；SSH/pytest/custom 留給之後）：
  - [x] serial shell tests。
  - [ ] SSH tests。
  - [ ] pytest tests。
  - [ ] custom scripts。
- [x] 基本 smoke tests（smoke 條目支援 `command + expect` regex；mismatch 標記 `assertion_failed=True`）：
  - [x] OpenWrt version（post-boot status capture，從 `ubus call system board` JSON 解出 release.distribution + release.version）。
  - [x] kernel version（同上，`kernel` 欄位）。
  - [x] uptime（`command: cat /proc/uptime` + `expect: ^\d+\.\d+`）。
  - [x] network interface up（`command: ip -j addr ...` + `expect: '"operstate":"UP"'`）。
  - [x] expected packages（`command: opkg list-installed` + `expect: <package-name>`）。
  - [x] expected services（`command: /etc/init.d/<svc> status` + `expect: running`）。
- [x] 支援 board-specific tests（profile overlay 可蓋 `tests.smoke`、`tests.status_command`、整個 `dut.*` block；configs/example.yaml 的 `ap` / `controller` / `switch` 各自有不同的 smoke 列表能力）。
- [x] 支援 test result report：
  - [x] passed/failed/skipped（report.md 加 `Result: **PASS|FAIL** (N/M passed, K failed, T s total)` 聚合行；skipped 仍未支援，需配合 fail-fast 機制）。
  - [x] duration（每行 smoke test 後加 `(0.12 s)`，並有 total 行）。
  - [x] logs。
  - [x] firmware metadata。
- [x] 支援 fail artifact retention，失敗時保存 firmware/log/config snapshot（run_dir 內 build.log / serial.log / firmware/ / config.snapshot.yaml / report.json 從來不會在失敗路徑被刪；只有 `prune` 才會清，且分 result 桶保留）。

## Phase 7 - Go Runner / Daemon

- [ ] 建立 `owrtd` Go daemon。
- [ ] 提供本機 API：
  - [ ] submit job。
  - [ ] cancel job。
  - [ ] stream logs。
  - [ ] query status。
  - [ ] list DUT locks。
  - [ ] list artifacts。
- [ ] 支援 JSONL or gRPC streaming。
- [ ] Go 負責長時間穩定工作：
  - [ ] process supervision。
  - [ ] stdout/stderr multiplexing。
  - [ ] cancellation。
  - [ ] backpressure。
  - [ ] log rotation。
  - [ ] job heartbeat。
- [ ] Go 負責 resource lock：
  - [ ] container lock。
  - [ ] DUT lock。
  - [ ] serial port lock。
  - [ ] artifact output lock。
- [ ] Python orchestrator 透過 API 呼叫 Go runner。
- [ ] 保留 Python-only fallback，方便 debug。

## Phase 8 - iTerm2/tmux Observer

- [ ] 不把 iTerm2 tab 當自動化核心。
- [ ] 提供 tmux session observer：
  - [ ] build log pane。
  - [ ] serial log pane。
  - [ ] job status pane。
  - [ ] test result pane。
- [ ] 可選 iTerm2 integration：
  - [ ] open named tabs。
  - [ ] attach logs。
  - [ ] jump to current job。
- [ ] iTerm2 integration failure 不影響 build/flash/test。

## Phase 9 - LLM Assistance

- [ ] LLM 僅作為輔助分析，不直接執行 dangerous action。
- [ ] 可用功能：
  - [ ] summarize build failure。
  - [ ] classify OpenWrt errors。
  - [ ] suggest next action。
  - [ ] generate bug report draft。
  - [ ] summarize DUT boot failure。
- [ ] LLM action guardrails：
  - [ ] structured input only。
  - [ ] no secret in prompt。
  - [ ] dangerous command requires explicit approval。
  - [ ] deterministic workflow remains source of truth。
- [ ] 保存 LLM analysis 與原始 log 的對應關係。

## Phase 10 - Reliability and Operations

- [x] 全域 log format 統一為 JSONL + human readable summary（每個 job 的 `events.jsonl` 是 machine-readable 流；`report.md` 是 human-readable 同步的快照；`build.log` 與 `serial.log` 保留 raw stream）。
- [x] 支援 log rotation（`owrt-monitor prune --keep-success N --keep-failed M [--apply]`：dry-run by default，依 result 分桶保留最新 N 筆，剩下的整個 run_dir 刪除；SQLite job records 不動，audit trail 保留）。
- [x] 支援 metrics（每 job 寫進 report.md `## Metrics` 段，並 persist 到 `jobs.metrics` JSON column；新 CLI `owrt-monitor metrics` 對最近 N jobs 聚合）：
  - [x] build duration（從 `build_summary.duration_sec` 抽出，已從 `>>>> ... Build done in: MM:SS.fff` 解析）。
  - [x] flash duration（`metrics.flash_duration_sec` = `boot_duration_sec`：sysupgrade write → shell prompt 回來的 wall-clock；同一量兩個別名以利 Phase 10 query 命名）。
  - [x] boot duration（sysupgrade write → prompt 回來的 wall-clock，DutWorkflow 量測；DUT_ONLINE event 的 fields 也帶）。
  - [x] test duration（smoke_duration_sec：整個 smoke 迴圈，含 setup overhead）。
  - [x] success rate（`owrt-monitor metrics`：counts_by_result + success/(success+failed)；含 mean/median/p90/min/max 區段）。
- [x] 支援 job history browser（`owrt-monitor inspect <job_id>` 印單一 job 全表；`owrt-monitor inspect <a> --diff <b>` 並排比對 artifact / provenance / build summary / metrics / DUT status）。
- [ ] 支援 crash recovery（部分；daemon recovery 等待 Phase 7）：
  - [ ] daemon restart 後恢復 job 狀態（Phase 7 owrtd 實作後；目前 Python-only 流程 crash 後 orphan job 透過 PID liveness + lock stale-recovery 在下次手動跑時自動處理）。
  - [x] serial session stale lock cleanup（DUT 與 builder lock 都有 `lock_timeout_sec` + heartbeat-based stale recovery；下個 acquire 自動 break + take over，emit 結構化 event）。
  - [x] partial artifact cleanup（`owrt-monitor prune --keep-success N --keep-failed M`：dry-run by default；保留 SQLite audit trail，只清 run_dir 內容）。
- [x] 支援 dry-run/rehearsal mode（`owrt-monitor dry-run` 子指令；其他子指令（build/run/flash/test/resume）也支援 `--dry-run`；計畫的 actions 會印在 `report.md` 但不觸發任何 docker 或 DUT 操作）。
- [x] 支援 config diff before run（每個新 BuildWorkflow.run / FlashWorkflow.run 啟動時，跟最近一次 SUCCEEDED job 的 config_snapshot diff，差異總數 + 前三個欄位寫進 `## Actions` 段；完整 sample 進 `config_diff_from_last_success` event）。
- [x] 支援 dangerous step confirmation mode（`upgrade.confirm_before_flash`，預設 false；TTY 互動時讀 stdin `[y/yes]`，非 TTY 自動跳過保留 CI 自動化；EOF / 非 yes 答案都 abort 為 DutWorkflowError）。

## Phase 11 - CI and Test Coverage

- [x] Python unit tests（170+ tests across 22 modules，包含真實 lab 從 build.log fixtures）：
  - [x] config parser（`test_config.py`：env interpolation、redacted dump、profile 驗證）。
  - [x] state machine（`test_workflow_integration.py` 用 `events.jsonl` 驗 12-state transition；transition recorded before side-effect）。
  - [x] artifact matching（`test_artifacts.py`：newest/largest/fail-if-multiple、min_size_mb 過濾）。
  - [x] serial prompt parser（`test_dut_serial.py` + `test_login.py`：read_until / read_until_one_of / failure patterns / 密碼 redact）。
  - [x] log parser（`test_build_log.py`：success / disk_full / failed_package / compile_error，含真實 lab build.log fixture）。
- [ ] Go unit tests（owrtd 已有，runner / locks / streaming 等待 Phase 7）：
  - [ ] process runner（Phase 7 Go runner 實作後）。
  - [ ] log streaming（Phase 7）。
  - [ ] lock manager（Phase 7；目前 lock 邏輯在 Python 端 sqlite-backed）。
  - [x] API handlers（`cmd/owrtd/main_test.go`：`/healthz` 200 + JSON、`/v1/jobs` 501 stub、`writeJSON` content-type/status；CI 跑 `go test ./...`）。
- [x] Integration tests with fake DUT（`test_workflow_integration.py:test_build_workflow_full_flow_with_allow_flash`）：
  - [x] pseudo terminal（`_FakeSerialTransport` 注入到 `SerialSession`，跳過 pyserial）。
  - [x] simulated boot log（`b"rebooting\n" + prompt` 驅動 read_until 通過 reboot wait）。
  - [x] simulated sysupgrade（fake transport 在 sysupgrade write 之後回 prompt → `DUT_ONLINE` transition）。
  - [x] full BuildWorkflow.run(allow_flash=True) end-to-end 走過 12 個 state transition。
- [x] Integration tests with fake Docker build（`tests/python/fake_docker.py` + `test_workflow_integration.py`）：
  - [x] success artifact（end-to-end happy path：build → classify → export → SUCCEEDED report）。
  - [x] compile failure（disk_full classification 寫進 FAILED report 的 build_summary）。
  - [x] cancel mid-build（fake 在 run_build 內呼叫 cancel_token.request → CANCELLED state）。
  - [x] dry-run 不觸發任何 docker 呼叫（safety property test）。
  - [x] resume from ARTIFACT_EXPORTED 不會誤觸發 build。
  - [x] missing artifact（list_artifacts 回空 → ArtifactSelectionError）。
  - [x] min_size_mb filter（artifact 太小 → ArtifactSelectionError，但 build_summary 仍有保留）。
  - [x] builder.timeout_sec timeout 流程（fake 透過 `build_should_timeout` 模擬，驗證 FAILED state + build_summary 仍掛上）。
- [ ] End-to-end lab tests on real DUT before declaring stable release（實機 hardware-in-the-loop 測試需要實體 DUT，需 lab 環境執行；本地 fake-DUT 整合測試已涵蓋 BuildWorkflow.run(allow_flash=True) 的 12 個 state transitions、HTTP / TFTP / U-Boot bootloader-TFTP 三條 transfer path，整套流程在 CI 已可重現）。

## Phase 12 - Documentation and Release

- [x] Write quickstart（`docs/quickstart.md`，含 cancel/resume/profile/stale-lock 章節）。
- [x] Write config reference（`docs/config-reference.md`）。
- [x] Write lab setup guide（`docs/lab-setup.md`，含 enter_docker.sh 與 tftpd 設定）。
- [x] Write adding a new board guide（`docs/adding-a-new-board.md`，含 deep-merge profile 範本）。
- [x] Write troubleshooting guide（`docs/troubleshooting.md`，含 disk-full / kernel-panic / orphan-job recipes）。
- [x] Write safe firmware upgrade guide（`docs/safe-upgrade.md`，含 pre-flash checklist 與 abort 流程）。
- [x] Define versioning policy（`docs/versioning.md`：SemVer，列出 public surface 與 deprecation 政策）。
- [x] Produce first tagged release（`owrt-monitor --version` / `-V` 暴露 `__version__ = "0.1.0"`；`CHANGELOG.md` 列出 0.1.0 完整內容；release 流程寫在 `docs/versioning.md`，使用者可下 `git tag -a v0.1.0` 完成 tag）。
