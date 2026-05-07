# owrt_monitor

`owrt_monitor` 是一套用來在 macOS 上協調 OpenWrt build、firmware artifact 匯出、DUT firmware upgrade 與自動化測試的長期專案。

核心方向是 **Python + Go hybrid**：

- Python 負責 orchestration、workflow、config、測試腳本、LLM log analysis。
- Go 負責長時間穩定執行的 runner / daemon、process supervision、streaming log、job cancellation 與本機 API。
- iTerm2 或 tmux 只作為觀察與操作輔助，不作為主要自動化控制核心。

主要文件：

- [TODO.md](TODO.md): 長期實作 roadmap 與 checklist。
- [ARCHITECTURE.md](ARCHITECTURE.md): 系統架構、資料流、模組邊界與穩定性設計。
- [docs/quickstart.md](docs/quickstart.md): 目前 MVP 的安裝與操作方式。
- [docs/config-reference.md](docs/config-reference.md): YAML config 欄位說明。

## Current MVP

目前第一版已經可以：

- 驗證 YAML config。
- 執行 dry-run，產出 job directory、JSONL events、SQLite state 與 report。
- 在 Docker builder container 內執行 OpenWrt build command。
- 依 glob pattern 偵測 firmware artifact，支援 newest/largest/fail-if-multiple 選擇策略。
- 用 `docker cp` 匯出 firmware，計算 SHA256，保存 artifact metadata。
- 透過 USB serial 控制 DUT prompt、啟動臨時 HTTP firmware server、執行 `wget` transfer。
- 在顯式 `--allow-flash` 下執行 configured upgrade command，等待 DUT prompt 回來。
- 執行 configured smoke tests，保存 serial transcript 與 test results。

安裝開發環境：

```sh
python3 -m pip install -e ".[dev,serial]"
```

常用指令：

```sh
owrt-monitor validate --config configs/example.yaml
owrt-monitor dry-run --config configs/example.yaml
owrt-monitor build --config configs/example.yaml
owrt-monitor run --config configs/example.yaml --allow-flash
owrt-monitor flash --config configs/example.yaml --artifact artifacts/job_x/firmware/openwrt.bin --allow-flash
owrt-monitor test --config configs/example.yaml
owrt-monitor status --config configs/example.yaml
```

`run --allow-flash` 和 `flash --allow-flash` 會執行破壞性的 DUT upgrade command；先用
`--dry-run` 檢查 report 內容，再對真機執行。
