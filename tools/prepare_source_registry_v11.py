#!/usr/bin/env python3
"""Convert the user's PRIVATE source registry to opt-in V1.1 candidate mappings.

Example:
  python tools/prepare_source_registry_v11.py --input data/private/source_registry_77.json \
      --output data/private/source_registry_77_v11.json
The private data path is excluded by .gitignore. No source becomes approved by conversion.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

CANDIDATES = {
    "mcp-releases": ("modelcontextprotocol", "modelcontextprotocol"),
    "langgraph-releases": ("langchain-ai", "langgraph"),
    "vllm-releases": ("vllm-project", "vllm"),
}

def derive(source: Path, output: Path):
    doc = json.loads(source.read_text(encoding="utf-8"))
    count = 0
    for row in doc["sources"]:
        repo = CANDIDATES.get(row["source_id"])
        if not repo:
            continue
        owner, name = repo
        url = urlsplit(row["endpoint_url"])
        expected_path = f"/{owner}/{name}/releases"
        if (url.scheme, url.hostname, url.path.rstrip("/")) != ("https", "github.com", expected_path):
            raise ValueError(f"Unexpected GitHub URL for {row['source_id']}")
        row.update(
            access_mode="github_releases",
            endpoint_url=f"https://api.github.com/repos/{owner}/{name}/releases?per_page=20",
            endpoint_verification="derived_candidate_requires_api_validation",
            rights="metadata_only",
            review_status="pending",
            enabled=False,
        )
        count += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(doc["sources"]), count

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("--input and --output must be different files")
    total, converted = derive(args.input, args.output)
    print(f"Private registry: {total} sources; {converted} API candidates; output={args.output}")
