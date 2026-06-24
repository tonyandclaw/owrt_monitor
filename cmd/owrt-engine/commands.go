package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"path/filepath"
	"text/tabwriter"

	"github.com/tonyandclaw/owrt_monitor/internal/analysis"
	"github.com/tonyandclaw/owrt_monitor/internal/config"
	"github.com/tonyandclaw/owrt_monitor/internal/configdiff"
	"github.com/tonyandclaw/owrt_monitor/internal/report"
	"github.com/tonyandclaw/owrt_monitor/internal/store"
	"github.com/tonyandclaw/owrt_monitor/internal/workflow"
)

// profileArg returns a *string: nil when the flag was not set, so the engine
// falls back to project.default_profile (matching the Python CLI).
func profileArg(raw string) *string {
	if raw == "" {
		return nil
	}
	return &raw
}

func (c *cli) cmdValidate(args []string) int {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(c.stderr)
	configPath := fs.String("config", "", "path to the config YAML (required)")
	profile := fs.String("profile", "", "profile overlay to apply (default: project.default_profile)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *configPath == "" {
		fmt.Fprintln(c.stderr, "owrt-engine validate: --config is required")
		return 2
	}
	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(c.stderr, "invalid config: %v\n", err)
		return 1
	}
	eff := cfg.EffectiveProfile(profileArg(*profile))
	if eff != nil {
		if _, err := cfg.WithProfile(*eff); err != nil {
			fmt.Fprintf(c.stderr, "invalid profile %q: %v\n", *eff, err)
			return 1
		}
		fmt.Fprintf(c.stdout, "OK: %s (profile %s) is valid\n", *configPath, *eff)
	} else {
		fmt.Fprintf(c.stdout, "OK: %s is valid\n", *configPath)
	}
	if profiles := cfg.ListProfiles(); len(profiles) > 0 {
		fmt.Fprintf(c.stdout, "profiles: %v\n", profiles)
	}
	return 0
}

func (c *cli) cmdRun(args []string, dryRun bool) int {
	name := "build"
	if dryRun {
		name = "dry-run"
	}
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(c.stderr)
	configPath := fs.String("config", "", "path to the config YAML (required)")
	profile := fs.String("profile", "", "profile overlay to apply (default: project.default_profile)")
	allowFlash := fs.Bool("allow-flash", false, "perform the destructive flash (not yet implemented in the Go engine)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *configPath == "" {
		fmt.Fprintf(c.stderr, "owrt-engine %s: --config is required\n", name)
		return 2
	}
	wf, err := workflow.NewBuildWorkflow(*configPath, profileArg(*profile), nil)
	if err != nil {
		fmt.Fprintf(c.stderr, "%v\n", err)
		return 1
	}
	defer wf.Close()
	rep, err := wf.Run(dryRun, *allowFlash)
	if rep != nil {
		c.printReport(rep)
	}
	if err != nil {
		fmt.Fprintf(c.stderr, "%s failed: %v\n", name, err)
		return 1
	}
	return 0
}

func (c *cli) printReport(rep *report.Report) {
	fmt.Fprintf(c.stdout, "job:      %s\n", rep.JobID)
	fmt.Fprintf(c.stdout, "state:    %s\n", rep.State)
	fmt.Fprintf(c.stdout, "success:  %t\n", rep.Success)
	fmt.Fprintf(c.stdout, "run dir:  %s\n", rep.RunDir)
	if rep.Artifact != nil {
		fmt.Fprintf(c.stdout, "artifact: %s (%d bytes, sha256 %s)\n",
			rep.Artifact.Filename, rep.Artifact.SizeBytes, rep.Artifact.SHA256)
	}
	for _, a := range rep.Actions {
		fmt.Fprintf(c.stdout, "  - %s\n", a)
	}
	for _, w := range rep.Warnings {
		fmt.Fprintf(c.stdout, "  ! %s\n", w)
	}
}

func (c *cli) cmdStatus(args []string) int {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	fs.SetOutput(c.stderr)
	configPath := fs.String("config", "", "path to the config YAML (required)")
	limit := fs.Int("limit", 20, "max number of jobs to list")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *configPath == "" {
		fmt.Fprintln(c.stderr, "owrt-engine status: --config is required")
		return 2
	}
	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(c.stderr, "invalid config: %v\n", err)
		return 1
	}
	st, err := store.Open(cfg.StateDBPath(*configPath))
	if err != nil {
		fmt.Fprintf(c.stderr, "open store: %v\n", err)
		return 1
	}
	defer st.Close()
	jobs, err := st.RecentJobs(*limit)
	if err != nil {
		fmt.Fprintf(c.stderr, "read jobs: %v\n", err)
		return 1
	}
	if len(jobs) == 0 {
		fmt.Fprintln(c.stdout, "no jobs recorded yet")
		return 0
	}
	tw := tabwriter.NewWriter(c.stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "JOB ID\tSTATE\tRESULT\tSTARTED\tRUN DIR")
	for _, j := range jobs {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\n", j.ID, j.State, j.Result, j.StartedAt, j.ArtifactDir)
	}
	if err := tw.Flush(); err != nil {
		fmt.Fprintf(c.stderr, "write: %v\n", err)
		return 1
	}
	return 0
}

func (c *cli) cmdAnalyze(args []string) int {
	fs := flag.NewFlagSet("analyze", flag.ContinueOnError)
	fs.SetOutput(c.stderr)
	runDir := fs.String("run-dir", "", "path to a job run directory")
	configPath := fs.String("config", "", "config file (with --job, to resolve the run dir)")
	job := fs.String("job", "", "job id (with --config)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	dir := *runDir
	if dir == "" {
		if *configPath == "" || *job == "" {
			fmt.Fprintln(c.stderr, "owrt-engine analyze: provide --run-dir, or both --config and --job")
			return 2
		}
		cfg, err := config.Load(*configPath)
		if err != nil {
			fmt.Fprintf(c.stderr, "invalid config: %v\n", err)
			return 1
		}
		dir = filepath.Join(cfg.ArtifactRoot(*configPath), *job)
	}
	result := analysis.Analyze(dir)
	jsonPath, mdPath, err := analysis.WriteFiles(dir, result)
	if err != nil {
		fmt.Fprintf(c.stderr, "write analysis: %v\n", err)
		return 1
	}
	if v, ok := result["verdict"].(map[string]any); ok {
		fmt.Fprintf(c.stdout, "verdict:  %v — %v\n", v["status"], v["summary"])
	}
	fmt.Fprintf(c.stdout, "written:  %s\n", jsonPath)
	fmt.Fprintf(c.stdout, "written:  %s\n", mdPath)
	return 0
}

func (c *cli) cmdDiff(args []string) int {
	fs := flag.NewFlagSet("diff", flag.ContinueOnError)
	fs.SetOutput(c.stderr)
	from := fs.String("from", "", "old config file (required)")
	to := fs.String("to", "", "new config file (required)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *from == "" || *to == "" {
		fmt.Fprintln(c.stderr, "owrt-engine diff: --from and --to are required")
		return 2
	}
	oldDump, err := redactedDump(*from)
	if err != nil {
		fmt.Fprintf(c.stderr, "load %s: %v\n", *from, err)
		return 1
	}
	newDump, err := redactedDump(*to)
	if err != nil {
		fmt.Fprintf(c.stderr, "load %s: %v\n", *to, err)
		return 1
	}
	changes := configdiff.Diff(oldDump, newDump)
	if len(changes) == 0 {
		fmt.Fprintln(c.stdout, "no differences")
		return 0
	}
	fmt.Fprintf(c.stdout, "%d change(s):\n", len(changes))
	for _, ch := range changes {
		fmt.Fprintf(c.stdout, "  %s: %v -> %v\n", ch.Path, ch.Old, ch.New)
	}
	return 0
}

// redactedDump loads a config and returns its redacted dump as a generic map
// (round-tripped through JSON) for diffing.
func redactedDump(path string) (map[string]any, error) {
	cfg, err := config.Load(path)
	if err != nil {
		return nil, err
	}
	dump, err := cfg.RedactedDump()
	if err != nil {
		return nil, err
	}
	// Normalize through JSON so types match configdiff's []any/map[string]any.
	data, err := json.Marshal(dump)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}
