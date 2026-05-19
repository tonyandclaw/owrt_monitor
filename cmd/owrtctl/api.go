package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

func (api client) getJSON(path string, values url.Values, target any) error {
	body, err := api.do(context.Background(), http.MethodGet, path, values, nil)
	if err != nil {
		return err
	}
	defer body.Close()
	return json.NewDecoder(body).Decode(target)
}

func (api client) postJSON(path string, payload any, target any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	body, err := api.do(context.Background(), http.MethodPost, path, nil, bytes.NewReader(data))
	if err != nil {
		return err
	}
	defer body.Close()
	return json.NewDecoder(body).Decode(target)
}

func (api client) do(
	ctx context.Context,
	method string,
	path string,
	values url.Values,
	body io.Reader,
) (io.ReadCloser, error) {
	if api.http == nil {
		api.http = http.DefaultClient
	}
	requestURL, err := api.url(path, values)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, method, requestURL, body)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := api.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		defer resp.Body.Close()
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		if len(bytes.TrimSpace(data)) == 0 {
			return nil, fmt.Errorf("%s %s failed: HTTP %d", method, requestURL, resp.StatusCode)
		}
		return nil, fmt.Errorf(
			"%s %s failed: HTTP %d: %s",
			method,
			requestURL,
			resp.StatusCode,
			strings.TrimSpace(string(data)),
		)
	}
	return resp.Body, nil
}

func (api client) url(path string, values url.Values) (string, error) {
	base, err := url.Parse(api.baseURL)
	if err != nil {
		return "", err
	}
	if base.Scheme == "" || base.Host == "" {
		return "", fmt.Errorf("invalid daemon URL %q", api.baseURL)
	}
	base.Path = strings.TrimRight(base.Path, "/") + path
	base.RawQuery = values.Encode()
	return base.String(), nil
}
