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
- [ ] 支援多 profile：
  - [ ] board profile。
  - [ ] target profile。
  - [ ] DUT profile。
  - [ ] test profile。
- [x] 支援 secret/env interpolation，但避免把 secret 寫進 log。
- [x] 支援 dry-run，列出即將執行的 Docker/artifact actions。

## Phase 2 - Python Orchestrator MVP

- [ ] 建立 Python CLI：
  - [x] `owrt-monitor build`。
  - [ ] `owrt-monitor flash`。
  - [ ] `owrt-monitor test`。
  - [x] `owrt-monitor run`。
  - [x] `owrt-monitor status`。
- [ ] 實作 workflow state machine：
  - [x] preflight。
  - [x] build。
  - [x] artifact detection。
  - [x] artifact export。
  - [ ] DUT prepare。
  - [ ] firmware transfer。
  - [ ] upgrade。
  - [ ] reboot wait。
  - [ ] post-upgrade tests。
  - [x] report。
- [ ] 使用 SQLite 保存 job state，支援 crash recovery。
- [ ] 每個 step 都要有：
  - [x] timeout。
  - [ ] retry policy。
  - [x] structured log。
  - [x] exit status。
  - [ ] resumability note。
- [ ] 支援 job cancel。
- [ ] 支援只重跑失敗 step。

## Phase 3 - Docker/OpenWrt Build Support

- [ ] 支援 Docker container discovery。
- [x] 支援指定 container name/id。
- [ ] 支援 build 前 preflight：
  - [x] container running。
  - [x] workspace path exists。
  - [x] OpenWrt tree exists。
  - [ ] enough disk space。
  - [ ] expected feeds/config present。
- [x] 支援 Docker exec build command。
- [x] 即時收集 stdout/stderr。
- [ ] 解析常見 OpenWrt build result：
  - [ ] success。
  - [ ] failed package。
  - [ ] missing dependency。
  - [ ] disk full。
  - [ ] compile error。
- [ ] 支援 artifact detection：
  - [x] glob。
  - [ ] regex。
  - [x] newest file。
  - [x] size threshold。
  - [x] checksum。
- [x] 支援 `docker cp` 匯出指定 firmware。
- [ ] 建立 artifact metadata：
  - [x] image path。
  - [x] SHA256。
  - [ ] build timestamp。
  - [ ] git commit if available。
  - [ ] OpenWrt target/subtarget/profile。

## Phase 4 - DUT Serial Control

- [ ] 支援 USB serial discovery：
  - [ ] `/dev/cu.usbserial-*`。
  - [ ] `/dev/tty.usbserial-*`。
  - [ ] custom path。
- [ ] 支援 serial config：
  - [ ] baud rate。
  - [ ] data bits/parity/stop bits。
  - [ ] newline mode。
  - [ ] prompt regex。
- [ ] 實作 robust serial session：
  - [ ] connect/disconnect。
  - [ ] read loop。
  - [ ] command write。
  - [ ] prompt wait。
  - [ ] boot log capture。
  - [ ] timeout handling。
- [ ] 支援 login flow：
  - [ ] root shell without password。
  - [ ] username/password。
  - [ ] custom prompt。
- [ ] 支援 reboot detection：
  - [ ] kernel boot marker。
  - [ ] login prompt。
  - [ ] OpenWrt banner。
  - [ ] shell prompt ready。
- [ ] 支援 DUT lock，避免兩個 job 同時 flash 同一台 DUT。

## Phase 5 - Firmware Transfer and Upgrade

- [ ] 支援 firmware transfer methods：
  - [ ] host HTTP server + DUT `wget`/`curl`。
  - [ ] SCP when DUT network is ready。
  - [ ] TFTP for bootloader/recovery flow。
  - [ ] custom command。
- [ ] host 啟動臨時 HTTP server：
  - [ ] bind interface。
  - [ ] random safe port。
  - [ ] checksum endpoint。
  - [ ] transfer log。
- [ ] DUT 下載 firmware 後檢查：
  - [ ] file exists。
  - [ ] file size。
  - [ ] SHA256。
- [ ] 支援 OpenWrt upgrade：
  - [ ] `sysupgrade`。
  - [ ] `mtd` custom flow。
  - [ ] platform-specific script。
- [ ] upgrade 前 safety checks：
  - [ ] artifact target matches DUT target。
  - [ ] minimum battery/power note if applicable。
  - [ ] enough `/tmp` space。
  - [ ] no active conflicting job。
- [ ] upgrade 後等待 reboot：
  - [ ] serial disconnect/reconnect tolerance。
  - [ ] boot timeout。
  - [ ] prompt detection。
  - [ ] fail-fast on kernel panic。
- [ ] 保存完整 upgrade transcript。

## Phase 6 - Post-Upgrade Testing

- [ ] 支援 test runner interface：
  - [ ] serial shell tests。
  - [ ] SSH tests。
  - [ ] pytest tests。
  - [ ] custom scripts。
- [ ] 基本 smoke tests：
  - [ ] OpenWrt version。
  - [ ] kernel version。
  - [ ] uptime。
  - [ ] network interface up。
  - [ ] expected packages。
  - [ ] expected services。
- [ ] 支援 board-specific tests。
- [ ] 支援 test result report：
  - [ ] passed/failed/skipped。
  - [ ] duration。
  - [ ] logs。
  - [ ] firmware metadata。
- [ ] 支援 fail artifact retention，失敗時保存 firmware/log/config snapshot。

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

- [ ] 全域 log format 統一為 JSONL + human readable summary。
- [ ] 支援 log rotation。
- [ ] 支援 metrics：
  - [ ] build duration。
  - [ ] flash duration。
  - [ ] boot duration。
  - [ ] test duration。
  - [ ] success rate。
- [ ] 支援 job history browser。
- [ ] 支援 crash recovery：
  - [ ] daemon restart 後恢復 job 狀態。
  - [ ] serial session stale lock cleanup。
  - [ ] partial artifact cleanup。
- [ ] 支援 dry-run/rehearsal mode。
- [ ] 支援 config diff before run。
- [ ] 支援 dangerous step confirmation mode。

## Phase 11 - CI and Test Coverage

- [ ] Python unit tests：
  - [ ] config parser。
  - [ ] state machine。
  - [ ] artifact matching。
  - [ ] serial prompt parser。
  - [ ] log parser。
- [ ] Go unit tests：
  - [ ] process runner。
  - [ ] log streaming。
  - [ ] lock manager。
  - [ ] API handlers。
- [ ] Integration tests with fake DUT:
  - [ ] pseudo terminal。
  - [ ] simulated boot log。
  - [ ] simulated sysupgrade。
- [ ] Integration tests with fake Docker build:
  - [ ] success artifact。
  - [ ] compile failure。
  - [ ] missing artifact。
  - [ ] timeout。
- [ ] End-to-end lab tests on real DUT before declaring stable release。

## Phase 12 - Documentation and Release

- [ ] Write quickstart。
- [ ] Write config reference。
- [ ] Write lab setup guide。
- [ ] Write adding a new board guide。
- [ ] Write troubleshooting guide。
- [ ] Write safe firmware upgrade guide。
- [ ] Define versioning policy。
- [ ] Produce first tagged release。
