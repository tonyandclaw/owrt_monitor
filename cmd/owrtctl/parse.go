package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func parseSubmitRequest(
	command string,
	args []string,
	daemonURL *string,
	stderr io.Writer,
) (jobSubmitRequest, error) {
	fs := commandFlags(command, daemonURL, stderr)
	config := fs.String("config", "", "owrt_monitor config path")
	profile := fs.String("profile", "", "config profile")
	dryRun := fs.Bool("dry-run", command == "dry-run", "submit as dry-run")
	allowFlash := fs.Bool(
		"allow-flash",
		command == "run" || command == "flash",
		"permit destructive flash/run; default true for run and flash",
	)
	artifact := fs.String("artifact", "", "firmware artifact path for flash")
	workingDir := fs.String("working-dir", "", "working directory for owrtd-launched process")
	if err := fs.Parse(args); err != nil {
		return jobSubmitRequest{}, err
	}
	if fs.NArg() != 0 {
		return jobSubmitRequest{}, fmt.Errorf("%s does not accept positional arguments", command)
	}

	submitCommand := command
	if command == "dry-run" {
		submitCommand = "build"
		*dryRun = true
	}
	configPath := strings.TrimSpace(*config)
	if configPath == "" {
		var defaultErr error
		configPath, defaultErr = defaultConfigPath()
		if defaultErr != nil {
			return jobSubmitRequest{}, defaultErr
		}
	}
	absConfig, err := filepath.Abs(configPath)
	if err != nil {
		return jobSubmitRequest{}, fmt.Errorf("resolve --config: %w", err)
	}
	absWorkingDir := *workingDir
	if strings.TrimSpace(absWorkingDir) == "" {
		absWorkingDir, err = os.Getwd()
		if err != nil {
			return jobSubmitRequest{}, fmt.Errorf("get working directory: %w", err)
		}
	} else {
		absWorkingDir, err = filepath.Abs(absWorkingDir)
		if err != nil {
			return jobSubmitRequest{}, fmt.Errorf("resolve --working-dir: %w", err)
		}
	}

	req := jobSubmitRequest{
		Command:    submitCommand,
		Config:     absConfig,
		DryRun:     *dryRun,
		AllowFlash: *allowFlash,
		WorkingDir: absWorkingDir,
	}
	if req.AllowFlash && submitCommand != "run" && submitCommand != "flash" {
		return jobSubmitRequest{}, fmt.Errorf("%s does not accept --allow-flash", command)
	}
	if strings.TrimSpace(*profile) != "" {
		req.Profile = *profile
	}
	if submitCommand == "flash" {
		if strings.TrimSpace(*artifact) != "" {
			absArtifact, err := filepath.Abs(*artifact)
			if err != nil {
				return jobSubmitRequest{}, fmt.Errorf("resolve --artifact: %w", err)
			}
			req.Artifact = absArtifact
		}
		if !req.DryRun && !req.AllowFlash {
			return jobSubmitRequest{}, errors.New("flash requires --allow-flash unless --dry-run is set")
		}
	} else if strings.TrimSpace(*artifact) != "" {
		return jobSubmitRequest{}, fmt.Errorf("%s does not accept --artifact", command)
	}
	return req, nil
}

func defaultConfigPath() (string, error) {
	candidates := []string{
		"config/example.yml",
		"config/example.yaml",
		"configs/example.yaml",
		"configs/example.yml",
	}
	for _, candidate := range candidates {
		info, err := os.Stat(candidate)
		if err == nil {
			if info.IsDir() {
				continue
			}
			return candidate, nil
		}
		if !errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("check default config %s: %w", candidate, err)
		}
	}
	return "", fmt.Errorf(
		"--config is required when no default config exists; tried %s",
		strings.Join(candidates, ", "),
	)
}

func parseLogsArgs(args []string, daemonURL *string) (
	jobID string,
	tail int,
	follow bool,
	raw bool,
	err error,
) {
	tail = 80
	var positional []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--follow":
			follow = true
		case arg == "--raw":
			raw = true
		case arg == "--tail":
			i++
			if i >= len(args) {
				return "", 0, false, false, errors.New("--tail requires a value")
			}
			value, parseErr := strconv.Atoi(args[i])
			if parseErr != nil {
				return "", 0, false, false, fmt.Errorf("--tail must be an integer: %w", parseErr)
			}
			tail = value
		case strings.HasPrefix(arg, "--tail="):
			value, parseErr := strconv.Atoi(strings.TrimPrefix(arg, "--tail="))
			if parseErr != nil {
				return "", 0, false, false, fmt.Errorf("--tail must be an integer: %w", parseErr)
			}
			tail = value
		case arg == "--daemon-url":
			i++
			if i >= len(args) {
				return "", 0, false, false, errors.New("--daemon-url requires a value")
			}
			*daemonURL = args[i]
		case strings.HasPrefix(arg, "--daemon-url="):
			*daemonURL = strings.TrimPrefix(arg, "--daemon-url=")
		case strings.HasPrefix(arg, "-"):
			return "", 0, false, false, fmt.Errorf("unknown logs flag %q", arg)
		default:
			positional = append(positional, arg)
		}
	}
	jobID, err = oneJobID(positional)
	if err != nil {
		return "", 0, false, false, err
	}
	return jobID, tail, follow, raw, nil
}

func parseWaitArgs(args []string, daemonURL *string) (
	jobID string,
	interval time.Duration,
	timeout time.Duration,
	err error,
) {
	interval = 2 * time.Second
	timeout = 0
	var positional []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--interval":
			i++
			if i >= len(args) {
				return "", 0, 0, errors.New("--interval requires a duration")
			}
			interval, err = time.ParseDuration(args[i])
			if err != nil {
				return "", 0, 0, fmt.Errorf("parse --interval: %w", err)
			}
		case strings.HasPrefix(arg, "--interval="):
			interval, err = time.ParseDuration(strings.TrimPrefix(arg, "--interval="))
			if err != nil {
				return "", 0, 0, fmt.Errorf("parse --interval: %w", err)
			}
		case arg == "--timeout":
			i++
			if i >= len(args) {
				return "", 0, 0, errors.New("--timeout requires a duration")
			}
			timeout, err = time.ParseDuration(args[i])
			if err != nil {
				return "", 0, 0, fmt.Errorf("parse --timeout: %w", err)
			}
		case strings.HasPrefix(arg, "--timeout="):
			timeout, err = time.ParseDuration(strings.TrimPrefix(arg, "--timeout="))
			if err != nil {
				return "", 0, 0, fmt.Errorf("parse --timeout: %w", err)
			}
		case arg == "--daemon-url":
			i++
			if i >= len(args) {
				return "", 0, 0, errors.New("--daemon-url requires a value")
			}
			*daemonURL = args[i]
		case strings.HasPrefix(arg, "--daemon-url="):
			*daemonURL = strings.TrimPrefix(arg, "--daemon-url=")
		case strings.HasPrefix(arg, "-"):
			return "", 0, 0, fmt.Errorf("unknown wait flag %q", arg)
		default:
			positional = append(positional, arg)
		}
	}
	if interval <= 0 {
		return "", 0, 0, errors.New("--interval must be positive")
	}
	if timeout < 0 {
		return "", 0, 0, errors.New("--timeout must be zero or positive")
	}
	jobID, err = oneJobID(positional)
	return jobID, interval, timeout, err
}

func parseFileArgs(args []string, daemonURL *string) (
	jobID string,
	runPath string,
	output string,
	err error,
) {
	output = "-"
	var positional []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--output":
			i++
			if i >= len(args) {
				return "", "", "", errors.New("--output requires a path or -")
			}
			output = args[i]
		case strings.HasPrefix(arg, "--output="):
			output = strings.TrimPrefix(arg, "--output=")
		case arg == "--daemon-url":
			i++
			if i >= len(args) {
				return "", "", "", errors.New("--daemon-url requires a value")
			}
			*daemonURL = args[i]
		case strings.HasPrefix(arg, "--daemon-url="):
			*daemonURL = strings.TrimPrefix(arg, "--daemon-url=")
		case strings.HasPrefix(arg, "-"):
			return "", "", "", fmt.Errorf("unknown file flag %q", arg)
		default:
			positional = append(positional, arg)
		}
	}
	if len(positional) != 2 {
		return "", "", "", errors.New("file expects <job_id> and <path>")
	}
	jobID = positional[0]
	runPath = positional[1]
	if strings.TrimSpace(jobID) == "" || strings.TrimSpace(runPath) == "" {
		return "", "", "", errors.New("job_id and path must not be blank")
	}
	if strings.TrimSpace(output) == "" {
		return "", "", "", errors.New("--output must not be blank")
	}
	return jobID, runPath, output, nil
}

func scopedRunPath(path string) (string, error) {
	trimmed := strings.TrimLeft(filepath.ToSlash(path), "/")
	if trimmed == "" {
		return "", nil
	}
	parts := strings.Split(trimmed, "/")
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("invalid run directory path %q", path)
		}
	}
	return strings.Join(parts, "/"), nil
}
