// Package docker is the Go port of python/owrt_monitor/docker_build.py: it runs
// the OpenWrt build inside a configured container via `docker exec`, detects
// firmware artifacts with a bash globstar script, gathers git provenance, and
// copies the selected artifact to the host with `docker cp` + SHA256.
//
// The BuildClient interface lets the workflow inject a fake in tests (mirroring
// Python's docker_client= override and FakeDockerBuildClient).
package docker

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
	"github.com/tonyandclaw/owrt_monitor/internal/config"
)

func durationSec(s int) time.Duration { return time.Duration(s) * time.Second }

// BuildError mirrors docker_build.DockerBuildError.
type BuildError struct{ msg string }

func (e *BuildError) Error() string { return e.msg }

func buildErr(format string, args ...any) *BuildError {
	return &BuildError{msg: fmt.Sprintf(format, args...)}
}

// BuildClient is the build/export surface the workflow depends on.
type BuildClient interface {
	BuildCommand(redactEnv bool) []string
	Preflight() error
	RunBuild(ctx context.Context, logPath string) error
	ListArtifacts(patterns []string) ([]artifact.Candidate, error)
	GatherBuildMetadata() map[string]any
	CopyArtifact(c artifact.Candidate, hostPath string) (artifact.ExportedArtifact, error)
}

// Client is the production BuildClient backed by the docker CLI.
type Client struct {
	builder config.BuilderConfig
}

// New returns a docker-backed BuildClient for the given builder config.
func New(builder config.BuilderConfig) *Client { return &Client{builder: builder} }

var _ BuildClient = (*Client)(nil)

// BuildCommand assembles the `docker exec` build command. Env keys are sorted
// for determinism; redactEnv masks values for logging (docker_build.build_command).
func (c *Client) BuildCommand(redactEnv bool) []string {
	cmd := []string{"docker", "exec", "--workdir", c.builder.Workdir}
	keys := make([]string, 0, len(c.builder.Env))
	for k := range c.builder.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		value := c.builder.Env[k]
		if redactEnv {
			value = "<redacted>"
		}
		cmd = append(cmd, "-e", fmt.Sprintf("%s=%s", k, value))
	}
	cmd = append(cmd, c.builder.Container)
	cmd = append(cmd, c.builder.Command...)
	return cmd
}

// Preflight verifies docker, the container is running, the workdir exists, disk
// space, and required paths (docker_build.preflight).
func (c *Client) Preflight() error {
	if _, err := exec.LookPath("docker"); err != nil {
		return buildErr("docker command was not found")
	}
	out, err := run("docker", "inspect", "-f", "{{.State.Running}}", c.builder.Container)
	if err != nil {
		return buildErr("cannot inspect container %s: %s", c.builder.Container, strings.TrimSpace(out))
	}
	if strings.TrimSpace(out) != "true" {
		return buildErr("container %s is not running", c.builder.Container)
	}
	if _, err := run("docker", "exec", c.builder.Container, "test", "-d", c.builder.Workdir); err != nil {
		return buildErr("workdir %s does not exist in %s", c.builder.Workdir, c.builder.Container)
	}
	if err := c.preflightRequiredPaths(); err != nil {
		return err
	}
	return nil
}

func (c *Client) preflightRequiredPaths() error {
	var missing []string
	for _, p := range c.builder.RequiredPaths {
		if _, err := run("docker", "exec", "--workdir", c.builder.Workdir, c.builder.Container, "test", "-e", p); err != nil {
			missing = append(missing, p)
		}
	}
	if len(missing) > 0 {
		return buildErr("required paths missing inside %s:%s: %v. Run feed/setup steps first.",
			c.builder.Container, c.builder.Workdir, missing)
	}
	return nil
}

// RunBuild streams the build to logPath and stdout, honoring ctx for
// cancellation and timeout (docker_build.run_build).
func (c *Client) RunBuild(ctx context.Context, logPath string) error {
	if c.builder.TimeoutSec > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, durationSec(c.builder.TimeoutSec))
		defer cancel()
	}
	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil {
		return err
	}
	logFile, err := os.Create(logPath)
	if err != nil {
		return err
	}
	defer logFile.Close()

	cmd := c.BuildCommand(false)
	proc := exec.CommandContext(ctx, cmd[0], cmd[1:]...)
	stdout, err := proc.StdoutPipe()
	if err != nil {
		return err
	}
	proc.Stderr = proc.Stdout // combine, matching subprocess STDOUT redirect
	if err := proc.Start(); err != nil {
		return buildErr("failed to start build: %v", err)
	}
	writer := io.MultiWriter(logFile, os.Stdout)
	_, _ = io.Copy(writer, stdout)
	err = proc.Wait()
	if ctx.Err() == context.DeadlineExceeded {
		return buildErr("build timed out after %d seconds", c.builder.TimeoutSec)
	}
	if ctx.Err() == context.Canceled {
		return buildErr("build cancelled")
	}
	if err != nil {
		return buildErr("build command failed: %v", err)
	}
	return nil
}

// artifactDetectorScript is the bash globstar detector. Kept byte-identical to
// docker_build.list_artifacts so both engines find the same files.
const artifactDetectorScript = "set -eu\n" +
	"shopt -s globstar nullglob\n" +
	"cd \"$OWRT_WORKDIR\"\n" +
	"for pattern in \"$@\"; do\n" +
	"  for f in $pattern; do\n" +
	"    [ -f \"$f\" ] || continue\n" +
	"    sz=$(stat -c %s -- \"$f\")\n" +
	"    mt=$(stat -c %Y -- \"$f\")\n" +
	"    printf \"%s\\t%s\\t%s\\n\" \"$sz\" \"$mt\" \"$f\"\n" +
	"  done\n" +
	"done\n"

// ListArtifacts runs the detector in the container and parses its output
// (docker_build.list_artifacts).
func (c *Client) ListArtifacts(patterns []string) ([]artifact.Candidate, error) {
	args := []string{
		"exec", "-e", "OWRT_WORKDIR=" + c.builder.Workdir, c.builder.Container,
		"bash", "-c", artifactDetectorScript, "_artifact_detector",
	}
	args = append(args, patterns...)
	out, err := run("docker", args...)
	if err != nil {
		return nil, buildErr("artifact detection failed: %s", strings.TrimSpace(out))
	}
	return parseArtifactLines(out)
}

// parseArtifactLines turns the TAB-separated detector output into candidates.
// Pure function: unit-testable without docker.
func parseArtifactLines(stdout string) ([]artifact.Candidate, error) {
	seen := map[string]artifact.Candidate{}
	order := []string{}
	sc := bufio.NewScanner(strings.NewReader(stdout))
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Text()
		if strings.TrimSpace(line) == "" {
			continue
		}
		parts := strings.SplitN(line, "\t", 3)
		if len(parts) != 3 {
			return nil, buildErr("artifact detector returned malformed line: %q", line)
		}
		size, err := strconv.ParseInt(parts[0], 10, 64)
		if err != nil {
			return nil, buildErr("artifact detector returned non-numeric size/mtime in line: %q", line)
		}
		mtime, err := strconv.ParseFloat(parts[1], 64)
		if err != nil {
			return nil, buildErr("artifact detector returned non-numeric size/mtime in line: %q", line)
		}
		path := parts[2]
		if _, ok := seen[path]; !ok {
			order = append(order, path)
		}
		seen[path] = artifact.Candidate{Path: path, SizeBytes: size, Mtime: mtime}
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	out := make([]artifact.Candidate, 0, len(order))
	for _, p := range order {
		out = append(out, seen[p])
	}
	return out, nil
}

// GatherBuildMetadata captures best-effort git provenance from the workdir
// (docker_build.gather_build_metadata). Failures become nil values.
func (c *Client) GatherBuildMetadata() map[string]any {
	capture := func(args ...string) any {
		full := append([]string{"exec", "--workdir", c.builder.Workdir, c.builder.Container}, args...)
		out, err := run("docker", full...)
		if err != nil {
			return nil
		}
		trimmed := strings.TrimSpace(out)
		if trimmed == "" {
			return nil
		}
		return trimmed
	}
	commit := capture("git", "rev-parse", "HEAD")
	describe := capture("git", "describe", "--tags", "--always", "--dirty")
	status := capture("git", "status", "--porcelain")
	var dirty any
	if status != nil {
		dirty = strings.TrimSpace(status.(string)) != ""
	}
	return map[string]any{
		"git_commit":   commit,
		"git_describe": describe,
		"git_dirty":    dirty,
	}
}

// CopyArtifact docker-cp's the candidate to the host and records SHA256
// (docker_build.copy_artifact).
func (c *Client) CopyArtifact(cand artifact.Candidate, hostPath string) (artifact.ExportedArtifact, error) {
	if err := os.MkdirAll(filepath.Dir(hostPath), 0o755); err != nil {
		return artifact.ExportedArtifact{}, err
	}
	containerPath := strings.TrimRight(c.builder.Workdir, "/") + "/" + cand.Path
	if out, err := run("docker", "cp", c.builder.Container+":"+containerPath, hostPath); err != nil {
		return artifact.ExportedArtifact{}, buildErr("docker cp failed for %s: %s", containerPath, strings.TrimSpace(out))
	}
	info, err := os.Stat(hostPath)
	if err != nil {
		return artifact.ExportedArtifact{}, err
	}
	sum, err := SHA256File(hostPath)
	if err != nil {
		return artifact.ExportedArtifact{}, err
	}
	return artifact.ExportedArtifact{
		ContainerPath: containerPath,
		HostPath:      hostPath,
		Filename:      filepath.Base(hostPath),
		SizeBytes:     info.Size(),
		SHA256:        sum,
	}, nil
}

// SHA256File streams a file through SHA-256 (docker_build.sha256_file).
func SHA256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// run executes a command and returns combined stdout+stderr.
func run(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}
