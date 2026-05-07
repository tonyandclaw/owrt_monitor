# owrt_monitor

`owrt_monitor` 是一套用來在 macOS 上協調 OpenWrt build、firmware artifact 匯出、DUT firmware upgrade 與自動化測試的長期專案。

核心方向是 **Python + Go hybrid**：

- Python 負責 orchestration、workflow、config、測試腳本、LLM log analysis。
- Go 負責長時間穩定執行的 runner / daemon、process supervision、streaming log、job cancellation 與本機 API。
- iTerm2 或 tmux 只作為觀察與操作輔助，不作為主要自動化控制核心。

主要文件：

- [TODO.md](TODO.md): 長期實作 roadmap 與 checklist。
- [ARCHITECTURE.md](ARCHITECTURE.md): 系統架構、資料流、模組邊界與穩定性設計。

