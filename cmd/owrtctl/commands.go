package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"
)

func (c *cli) submit(api client, req jobSubmitRequest) error {
	if req.Command == "flash" && strings.TrimSpace(req.Artifact) == "" {
		artifact, jobID, err := c.latestArtifact(api, req.Profile)
		if err != nil {
			return err
		}
		req.Artifact = artifact
		fmt.Fprintf(c.stderr, "owrtctl: using latest artifact from %s: %s\n", jobID, artifact)
	}
	var accepted jobSubmitResponse
	if err := api.postJSON("/v1/jobs", req, &accepted); err != nil {
		return err
	}
	return printPrettyJSON(c.stdout, accepted)
}

func (c *cli) latestArtifact(api client, profile string) (artifactPath string, jobID string, err error) {
	values := url.Values{"limit": {"100"}}
	var jobs []jobEntry
	if err := api.getJSON("/v1/jobs", values, &jobs); err != nil {
		return "", "", fmt.Errorf("find latest artifact: %w", err)
	}
	profile = strings.TrimSpace(profile)
	for _, job := range jobs {
		if !job.Success {
			continue
		}
		var report jobReport
		if err := api.getJSON("/v1/jobs/"+url.PathEscape(job.JobID), nil, &report); err != nil {
			return "", "", fmt.Errorf("read report for %s: %w", job.JobID, err)
		}
		if !report.Success {
			continue
		}
		if profile != "" && strings.TrimSpace(report.BuildMetadata.Profile) != profile {
			continue
		}
		artifact := strings.TrimSpace(report.Artifact.HostPath)
		if artifact == "" {
			continue
		}
		if report.JobID != "" {
			return artifact, report.JobID, nil
		}
		return artifact, job.JobID, nil
	}
	if profile != "" {
		return "", "", fmt.Errorf(
			"no successful job with exported artifact found for profile %q; pass --artifact explicitly",
			profile,
		)
	}
	return "", "", errors.New("no successful job with exported artifact found; pass --artifact explicitly")
}

func (c *cli) health(api client) error {
	var payload map[string]any
	if err := api.getJSON("/healthz", nil, &payload); err != nil {
		return err
	}
	return printPrettyJSON(c.stdout, payload)
}

func (c *cli) jobs(api client, limit int) error {
	if limit < 1 || limit > 100 {
		return errors.New("--limit must be in [1, 100]")
	}
	values := url.Values{"limit": {strconv.Itoa(limit)}}
	var jobs []jobEntry
	if err := api.getJSON("/v1/jobs", values, &jobs); err != nil {
		return err
	}

	table := tabwriter.NewWriter(c.stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(table, "JOB ID\tSTATE\tSUCCESS\tDRY\tSTARTED\tRUN DIR")
	for _, job := range jobs {
		fmt.Fprintf(
			table,
			"%s\t%s\t%t\t%t\t%s\t%s\n",
			job.JobID,
			job.State,
			job.Success,
			job.DryRun,
			job.StartedAt,
			job.RunDir,
		)
	}
	return table.Flush()
}

func (c *cli) prettyEndpoint(api client, path string) error {
	var payload any
	if err := api.getJSON(path, nil, &payload); err != nil {
		return err
	}
	return printPrettyJSON(c.stdout, payload)
}

func (c *cli) copyEndpoint(api client, path string, values url.Values) error {
	body, err := api.do(context.Background(), http.MethodGet, path, values, nil)
	if err != nil {
		return err
	}
	defer body.Close()
	_, err = io.Copy(c.stdout, body)
	return err
}

func (c *cli) logs(api client, jobID string, tail int, follow bool, raw bool) error {
	if tail < 1 || tail > 10000 {
		return errors.New("--tail must be in [1, 10000]")
	}
	values := url.Values{"tail": {strconv.Itoa(tail)}}
	if follow {
		values.Set("follow", "true")
	}
	path := "/v1/jobs/" + url.PathEscape(jobID) + "/runner-output"
	if raw {
		return c.copyEndpoint(api, path, values)
	}

	body, err := api.do(context.Background(), http.MethodGet, path, values, nil)
	if err != nil {
		return err
	}
	defer body.Close()

	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		var event runnerOutputEvent
		if err := json.Unmarshal(line, &event); err != nil {
			fmt.Fprintln(c.stdout, string(line))
			continue
		}
		if event.TS == "" && event.Stream == "" {
			fmt.Fprintln(c.stdout, event.Line)
			continue
		}
		fmt.Fprintf(c.stdout, "%s %-6s %s\n", event.TS, event.Stream, event.Line)
	}
	return scanner.Err()
}

func (c *cli) wait(
	api client,
	jobID string,
	interval time.Duration,
	timeout time.Duration,
) error {
	deadline := time.Time{}
	if timeout > 0 {
		deadline = time.Now().Add(timeout)
	}
	previous := ""
	for {
		var status runnerStatus
		if err := api.getJSON(
			"/v1/jobs/"+url.PathEscape(jobID)+"/runner",
			nil,
			&status,
		); err != nil {
			return err
		}
		line := fmt.Sprintf("%s status=%s pid=%d updated=%s", status.JobID, status.Status, status.PID, status.UpdatedAt)
		if status.ExitCode != nil {
			line += fmt.Sprintf(" exit_code=%d", *status.ExitCode)
		}
		if status.Error != "" {
			line += " error=" + status.Error
		}
		if line != previous {
			fmt.Fprintln(c.stdout, line)
			previous = line
		}
		if !runnerActive(status.Status) {
			if status.Status == "exited" && status.ExitCode != nil && *status.ExitCode != 0 {
				return fmt.Errorf("runner exited with code %d", *status.ExitCode)
			}
			if status.Status == "start_failed" || status.Status == "orphaned" {
				if status.Error != "" {
					return fmt.Errorf("runner %s: %s", status.Status, status.Error)
				}
				return fmt.Errorf("runner %s", status.Status)
			}
			return nil
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			return fmt.Errorf("timeout waiting for %s", jobID)
		}
		time.Sleep(interval)
	}
}

func runnerActive(status string) bool {
	return status == "starting" || status == "running" || status == "cancel_requested"
}

func (c *cli) file(api client, jobID string, runPath string, output string) error {
	scopedPath, err := scopedRunPath(runPath)
	if err != nil {
		return err
	}
	path := "/v1/jobs/" + url.PathEscape(jobID) + "/files/" + scopedPath
	body, err := api.do(context.Background(), http.MethodGet, path, nil, nil)
	if err != nil {
		return err
	}
	defer body.Close()
	if output == "-" {
		_, err = io.Copy(c.stdout, body)
		return err
	}
	out, err := os.Create(output)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, body)
	return err
}

func (c *cli) cancel(api client, jobID string) error {
	path := "/v1/jobs/" + url.PathEscape(jobID) + "/cancel"
	body, err := api.do(context.Background(), http.MethodPost, path, nil, nil)
	if err != nil {
		return err
	}
	defer body.Close()
	var payload any
	if err := json.NewDecoder(body).Decode(&payload); err != nil {
		return err
	}
	return printPrettyJSON(c.stdout, payload)
}

func (c *cli) remove(api client, jobID string) error {
	path := "/v1/jobs/" + url.PathEscape(jobID)
	body, err := api.do(context.Background(), http.MethodDelete, path, nil, nil)
	if err != nil {
		return err
	}
	defer body.Close()
	var payload any
	if err := json.NewDecoder(body).Decode(&payload); err != nil {
		return err
	}
	return printPrettyJSON(c.stdout, payload)
}

func printPrettyJSON(w io.Writer, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(w, string(data))
	return err
}
