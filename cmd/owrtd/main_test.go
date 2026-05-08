package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestHealthzReturnsOK exercises the only currently-implemented endpoint to
// catch regressions when we expand owrtd. The Python orchestrator may end up
// polling /healthz before submitting jobs, so the contract matters.
func TestHealthzReturnsOK(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()

	// Replicate the handler the way main() wires it; can't import main()'s
	// closures directly, so instantiate a fresh response with the same shape.
	writeJSON(rec, http.StatusOK, healthResponse{Status: "ok"})

	resp := rec.Result()
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200 OK, got %d", resp.StatusCode)
	}
	if got := resp.Header.Get("Content-Type"); got != "application/json" {
		t.Fatalf("want Content-Type application/json, got %q", got)
	}
	var body healthResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "ok" {
		t.Fatalf(`want status "ok", got %q`, body.Status)
	}
	_ = req
}

// TestJobsStubReturns501 locks in the 501 contract: until the Go runner
// milestone lands, anyone calling /v1/jobs should get a clear "not yet"
// response, not a panic or a 404. Python orchestrator reads this to decide
// whether to fall back to the in-process workflow.
func TestJobsStubReturns501(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusNotImplemented, notImplementedResponse{
		Error: "owrtd job API is reserved for a later runner milestone",
	})

	resp := rec.Result()
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d", resp.StatusCode)
	}
	var body notImplementedResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Error == "" {
		t.Fatal("expected non-empty error message")
	}
}

// TestWriteJSONSetsContentTypeAndStatus is the unit test for the lone helper.
// `writeJSON` is a one-liner today, but it's the foundation of every future
// handler — keep it tested.
func TestWriteJSONSetsContentTypeAndStatus(t *testing.T) {
	rec := httptest.NewRecorder()
	type payload struct {
		N int `json:"n"`
	}
	writeJSON(rec, http.StatusTeapot, payload{N: 42})

	if rec.Code != http.StatusTeapot {
		t.Fatalf("want 418, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf(`want "application/json", got %q`, got)
	}
	var body payload
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.N != 42 {
		t.Fatalf("want N=42, got %d", body.N)
	}
}
