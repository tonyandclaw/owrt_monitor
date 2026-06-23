package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"
)

func (c *cli) run(args []string) int {
	if err := c.runErr(args); err != nil {
		fmt.Fprintf(c.stderr, "owrtctl: %v\n", err)
		return 1
	}
	return 0
}

func (c *cli) runErr(args []string) error {
	root := flag.NewFlagSet("owrtctl", flag.ContinueOnError)
	root.SetOutput(c.stderr)
	daemonURL := root.String("daemon-url", daemonURLDefault(), "owrtd base URL")
	if err := root.Parse(args); err != nil {
		return err
	}
	if root.NArg() == 0 {
		printUsage(c.stdout)
		return nil
	}

	command := root.Arg(0)
	commandArgs := root.Args()[1:]
	api := client{baseURL: strings.TrimRight(*daemonURL, "/"), http: c.client}

	switch command {
	case "build", "run", "flash", "test", "dry-run":
		req, err := parseSubmitRequest(command, commandArgs, daemonURL, c.stderr)
		if err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.submit(api, req)
	case "health":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.health(api)
	case "jobs":
		fs := commandFlags(command, daemonURL, c.stderr)
		limit := fs.Int("limit", 20, "number of jobs to list")
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.jobs(api, *limit)
	case "status":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.prettyEndpoint(api, "/v1/jobs/"+url.PathEscape(jobID)+"/runner")
	case "report":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.prettyEndpoint(api, "/v1/jobs/"+url.PathEscape(jobID))
	case "analysis":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.prettyEndpoint(api, "/v1/jobs/"+url.PathEscape(jobID)+"/analysis")
	case "events":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.copyEndpoint(api, "/v1/jobs/"+url.PathEscape(jobID)+"/events", nil)
	case "logs":
		jobID, tail, follow, raw, err := parseLogsArgs(commandArgs, daemonURL)
		if err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.logs(api, jobID, tail, follow, raw)
	case "wait":
		jobID, interval, timeout, err := parseWaitArgs(commandArgs, daemonURL)
		if err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.wait(api, jobID, interval, timeout)
	case "file":
		jobID, path, output, err := parseFileArgs(commandArgs, daemonURL)
		if err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.file(api, jobID, path, output)
	case "cancel":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.cancel(api, jobID)
	case "remove", "delete":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		jobID, err := oneJobID(fs.Args())
		if err != nil {
			return err
		}
		return c.remove(api, jobID)
	case "locks":
		fs := commandFlags(command, daemonURL, c.stderr)
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		api.baseURL = strings.TrimRight(*daemonURL, "/")
		return c.prettyEndpoint(api, "/v1/locks")
	case "help", "-h", "--help":
		printUsage(c.stdout)
		return nil
	default:
		return fmt.Errorf("unknown command %q", command)
	}
}

func commandFlags(name string, daemonURL *string, stderr io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(daemonURL, "daemon-url", *daemonURL, "owrtd base URL")
	return fs
}

func daemonURLDefault() string {
	if value := strings.TrimSpace(os.Getenv("OWRTD_URL")); value != "" {
		return value
	}
	return defaultDaemonURL
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, `Usage:
  owrtctl [--daemon-url URL] build [--config PATH] [--profile NAME] [--dry-run] [--working-dir PATH]
  owrtctl [--daemon-url URL] run [--config PATH] [--profile NAME] [--dry-run] [--working-dir PATH]
  owrtctl [--daemon-url URL] flash [--config PATH] [--artifact PATH] [--profile NAME] [--dry-run]
  owrtctl [--daemon-url URL] test [--config PATH] [--profile NAME] [--dry-run] [--working-dir PATH]
  owrtctl [--daemon-url URL] dry-run [--config PATH] [--profile NAME] [--working-dir PATH]
  owrtctl [--daemon-url URL] health
  owrtctl [--daemon-url URL] jobs [--limit N]
  owrtctl [--daemon-url URL] status <job_id>
  owrtctl [--daemon-url URL] wait <job_id> [--interval 2s] [--timeout 0]
  owrtctl [--daemon-url URL] logs <job_id> [--tail N] [--follow] [--raw]
  owrtctl [--daemon-url URL] events <job_id>
  owrtctl [--daemon-url URL] report <job_id>
  owrtctl [--daemon-url URL] analysis <job_id>
  owrtctl [--daemon-url URL] file <job_id> <path> [--output PATH]
  owrtctl [--daemon-url URL] cancel <job_id>
  owrtctl [--daemon-url URL] remove <job_id>
  owrtctl [--daemon-url URL] locks

Environment:
  OWRTD_URL sets the default daemon URL when --daemon-url is omitted.

Notes:
  --config defaults to config/example.yml, config/example.yaml, configs/example.yaml, or configs/example.yml.
  run and flash submit with allow_flash enabled by default; use --dry-run to avoid destructive work.
  flash without --artifact uses the newest successful job with an exported artifact.
  When --profile is set, the default artifact must come from the same profile.`)
}

func oneJobID(args []string) (string, error) {
	if len(args) != 1 {
		return "", errors.New("expected exactly one job_id")
	}
	if strings.TrimSpace(args[0]) == "" {
		return "", errors.New("job_id must not be blank")
	}
	return args[0], nil
}
