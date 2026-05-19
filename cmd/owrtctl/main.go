package main

import (
	"net/http"
	"os"
	"time"
)

func main() {
	code := (&cli{
		stdout: os.Stdout,
		stderr: os.Stderr,
		client: &http.Client{Timeout: 30 * time.Second},
	}).run(os.Args[1:])
	os.Exit(code)
}
