package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"github.com/aivoya/acquisition/internal/core"
	"os"
)

func main() {
	path := flag.String("input", "data/source_registry.sample.json", "source registry JSON")
	flag.Parse()
	file, err := os.Open(*path)
	if err != nil {
		panic(err)
	}
	defer file.Close()
	var x struct {
		Sources []core.Source `json:"sources"`
	}
	d := json.NewDecoder(file)
	d.DisallowUnknownFields()
	if err := d.Decode(&x); err != nil {
		panic(err)
	}
	seen := map[string]bool{}
	for _, s := range x.Sources {
		if err := core.CheckSource(s); err != nil {
			fmt.Fprintf(os.Stderr, "source %s: %v\n", s.ID, err)
			os.Exit(1)
		}
		if seen[s.ID] {
			fmt.Fprintf(os.Stderr, "duplicate source: %s\n", s.ID)
			os.Exit(1)
		}
		seen[s.ID] = true
	}
	fmt.Printf("VALID: %d unique sources; policy approval is still required before fetching.\n", len(x.Sources))
}
