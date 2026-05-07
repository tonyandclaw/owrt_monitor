package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
)

type healthResponse struct {
	Status string `json:"status"`
}

type notImplementedResponse struct {
	Error string `json:"error"`
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8765", "HTTP listen address")
	flag.Parse()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, healthResponse{Status: "ok"})
	})
	mux.HandleFunc("/v1/jobs", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusNotImplemented, notImplementedResponse{
			Error: "owrtd job API is reserved for a later runner milestone",
		})
	})

	log.Printf("owrtd listening on http://%s", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("write response: %v", err)
	}
}

