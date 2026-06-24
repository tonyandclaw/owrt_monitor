// Command owrt-engine is the standalone Go engine entrypoint. It runs the same
// build/artifact pipeline as the Python `owrt-monitor` CLI against the shared
// on-disk state (SQLite + run directories), so the two engines are
// interchangeable over one artifact directory.
//
// Implemented: validate, dry-run, build, status. The destructive flash/DUT
// path is not yet ported (build --allow-flash is rejected with a clear error).
package main

import (
	"fmt"
	"io"
	"os"
)

func main() {
	c := &cli{stdout: os.Stdout, stderr: os.Stderr}
	os.Exit(c.run(os.Args[1:]))
}

type cli struct {
	stdout io.Writer
	stderr io.Writer
}

const usage = `owrt-engine — standalone Go engine for owrt_monitor

Usage:
  owrt-engine validate  --config FILE [--profile NAME]
  owrt-engine dry-run   --config FILE [--profile NAME]
  owrt-engine build     --config FILE [--profile NAME]
  owrt-engine status    --config FILE [--limit N]
  owrt-engine analyze   --run-dir DIR | (--config FILE --job ID)
  owrt-engine diff      --from FILE --to FILE

All commands share the Python engine's config schema, SQLite store, and run
directories, so jobs are visible to both engines and the owrtd dashboard.
`

func (c *cli) run(args []string) int {
	if len(args) == 0 {
		fmt.Fprint(c.stderr, usage)
		return 2
	}
	switch args[0] {
	case "validate":
		return c.cmdValidate(args[1:])
	case "dry-run":
		return c.cmdRun(args[1:], true)
	case "build":
		return c.cmdRun(args[1:], false)
	case "status":
		return c.cmdStatus(args[1:])
	case "analyze":
		return c.cmdAnalyze(args[1:])
	case "diff":
		return c.cmdDiff(args[1:])
	case "-h", "--help", "help":
		fmt.Fprint(c.stdout, usage)
		return 0
	default:
		fmt.Fprintf(c.stderr, "owrt-engine: unknown command %q\n\n%s", args[0], usage)
		return 2
	}
}
